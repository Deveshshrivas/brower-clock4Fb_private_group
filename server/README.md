# Server

FastAPI server for running the Facebook group exporter over HTTP.

## Run

From the project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn server.app:app --host 0.0.0.0 --port 8000
```

Interactive docs:

```text
http://localhost:8000/docs
```

## Endpoints

- `GET /health`
- `POST /scrape`
- `POST /scrape-json`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/result`
- `GET /jobs/{job_id}/posts.csv`
- `GET /jobs/{job_id}/comments.csv`

## Example

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/scrape" `
  -ContentType "application/json" `
  -Body '{"group_url":"https://www.facebook.com/groups/964040666405959","max_posts":25,"max_comments":20,"comment_expand_rounds":1,"comment_sort":"relevant","headless":true,"today_only":true,"recover_urls":false}'
```

Scrape output is written under `server/api-runs/`.
JSON post rows include `reaction_count`, `total_comment_count`, and `comments_found` in addition to the full `comments` array. The default API mode fetches up to 20 `relevant` comments per post with one light expansion round. Set `max_comments` to `0`/`null` for no scraper-side comment/reply limit.
Use `parallel_workers` with `parallel_profile_dirs` to scrape post permalink pages in parallel. Each profile directory must already be logged into Facebook, for example `browser-profile-1,browser-profile-2,browser-profile-3`.
Keep `recover_urls` false for faster feed-only exports. Set it true only when you need slower click/HTML recovery for feed cards where Facebook hides the post permalink.
