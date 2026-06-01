from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


SERVER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SERVER_DIR.parent
RUNS_DIR = SERVER_DIR / "api-runs"
FETCH_SCRIPT = PROJECT_DIR / "fetch_group_data.py"

app = FastAPI(title="Facebook Group Browser API")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class ScrapeRequest(BaseModel):
    group_url: str | None = Field(default=None, description="Facebook group URL.")
    profile_dir: str = Field(default="browser-profile")
    facebook_email: str | None = None
    max_posts: int = Field(default=25, ge=1, le=2000)
    max_comments: int | None = Field(
        default=20,
        ge=0,
        description="Maximum comments/replies per post. Use 0 or null for no scraper-side limit.",
    )
    comment_expand_rounds: int | None = Field(
        default=1,
        ge=0,
        description="Comment/reply expansion rounds. Use 0 for no expansion beyond currently visible comments.",
    )
    comment_sort: Literal["relevant", "all"] = Field(
        default="relevant",
        description="Use Facebook's relevant comments for speed, or all comments for exhaustive scraping.",
    )
    scrolls: int = Field(default=12, ge=0, le=200)
    headless: bool = False
    extra_post_urls: str | None = None
    parallel_workers: int = Field(
        default=1,
        ge=1,
        le=8,
        description="Parallel workers for scraping post permalink pages. Requires separate logged-in profiles.",
    )
    parallel_profile_dirs: str | None = Field(
        default=None,
        description="Comma/newline-separated browser profile directories. Each must already be logged into Facebook.",
    )
    today_only: bool = Field(
        default=True,
        description="Stop scanning New posts once a Yesterday/older post is reached.",
    )
    recover_urls: bool = Field(
        default=False,
        description="Enable slower click/HTML recovery for feed posts that do not expose permalinks.",
    )


class JobCreated(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    status_url: str
    result_url: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_job(job_id: str, job: dict) -> dict:
    return {
        "job_id": job_id,
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "return_code": job.get("return_code"),
        "error": job.get("error"),
        "logs": job.get("logs", [])[-200:],
        "result_url": f"/jobs/{job_id}/result",
        "posts_csv_url": f"/jobs/{job_id}/posts.csv",
        "comments_csv_url": f"/jobs/{job_id}/comments.csv",
    }


def run_scrape_job(job_id: str, request: ScrapeRequest) -> None:
    run_dir = RUNS_DIR / job_id
    run_dir.mkdir(parents=True, exist_ok=True)

    output_json = run_dir / "fb_group_posts.json"
    output_csv = run_dir / "fb_group_posts.csv"
    debug_dir = run_dir / "debug"
    profile_dir = Path(request.profile_dir)
    if not profile_dir.is_absolute():
        profile_dir = PROJECT_DIR / profile_dir

    command = [
        sys.executable,
        str(FETCH_SCRIPT),
        "--profile-dir",
        str(profile_dir),
        "--output-json",
        str(output_json),
        "--output-csv",
        str(output_csv),
        "--max-posts",
        str(request.max_posts),
        "--max-comments",
        str(request.max_comments or 0),
        "--comment-expand-rounds",
        str(request.comment_expand_rounds or 0),
        "--comment-sort",
        request.comment_sort,
        "--scrolls",
        str(request.scrolls),
        "--debug-dir",
        str(debug_dir),
        "--parallel-workers",
        str(request.parallel_workers),
        "--today-only" if request.today_only else "--no-today-only",
        "--recover-urls" if request.recover_urls else "--no-recover-urls",
        "--headless" if request.headless else "--no-headless",
    ]

    if request.group_url:
        command.extend(["--group-url", request.group_url])
    if request.facebook_email:
        command.extend(["--facebook-email", request.facebook_email])
    if request.extra_post_urls:
        command.extend(["--extra-post-urls", request.extra_post_urls])
    if request.parallel_profile_dirs:
        command.extend(["--parallel-profile-dirs", request.parallel_profile_dirs])

    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["started_at"] = utc_now()

    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        assert process.stdout is not None
        for line in process.stdout:
            with jobs_lock:
                jobs[job_id]["logs"].append(line.rstrip())

        return_code = process.wait()
        with jobs_lock:
            jobs[job_id]["return_code"] = return_code
            jobs[job_id]["finished_at"] = utc_now()
            jobs[job_id]["status"] = "completed" if return_code == 0 else "failed"
            if return_code != 0:
                jobs[job_id]["error"] = f"Exporter exited with code {return_code}."
    except Exception as exc:
        with jobs_lock:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["finished_at"] = utc_now()
            jobs[job_id]["error"] = str(exc)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape", response_model=JobCreated, status_code=202)
def start_scrape(request: ScrapeRequest) -> JobCreated:
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "created_at": utc_now(),
            "logs": [],
            "error": None,
        }

    thread = threading.Thread(target=run_scrape_job, args=(job_id, request), daemon=True)
    thread.start()

    return JobCreated(
        job_id=job_id,
        status="queued",
        status_url=f"/jobs/{job_id}",
        result_url=f"/jobs/{job_id}/result",
    )


@app.post("/scrape-json")
def scrape_json(request: ScrapeRequest) -> dict:
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "created_at": utc_now(),
            "logs": [],
            "error": None,
        }

    run_scrape_job(job_id, request)

    with jobs_lock:
        job = jobs[job_id]
        response = public_job(job_id, job)

    if job["status"] != "completed":
        raise HTTPException(status_code=500, detail=response)

    output_json = RUNS_DIR / job_id / "fb_group_posts.json"
    if not output_json.exists():
        raise HTTPException(status_code=500, detail={**response, "error": "Result JSON was not created."})

    response["data"] = json.loads(output_json.read_text(encoding="utf-8"))
    return response


@app.get("/jobs")
def list_jobs() -> list[dict]:
    with jobs_lock:
        return [public_job(job_id, job) for job_id, job in jobs.items()]


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return public_job(job_id, job)


@app.get("/jobs/{job_id}/result")
def get_result(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        if job["status"] != "completed":
            raise HTTPException(status_code=409, detail=f"Job is {job['status']}.")

    output_json = RUNS_DIR / job_id / "fb_group_posts.json"
    if not output_json.exists():
        raise HTTPException(status_code=404, detail="Result JSON was not created.")

    return json.loads(output_json.read_text(encoding="utf-8"))


@app.get("/jobs/{job_id}/posts.csv")
def get_posts_csv(job_id: str) -> FileResponse:
    path = RUNS_DIR / job_id / "fb_group_posts.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Posts CSV not found.")
    return FileResponse(path, media_type="text/csv", filename="fb_group_posts.csv")


@app.get("/jobs/{job_id}/comments.csv")
def get_comments_csv(job_id: str) -> FileResponse:
    path = RUNS_DIR / job_id / "fb_group_posts_comments.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Comments CSV not found.")
    return FileResponse(path, media_type="text/csv", filename="fb_group_posts_comments.csv")
