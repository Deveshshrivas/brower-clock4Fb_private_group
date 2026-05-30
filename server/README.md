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
  -Body '{"group_url":"https://www.facebook.com/groups/964040666405959","max_posts":25,"max_comments":1000,"headless":false}'
```

Scrape output is written under `server/api-runs/`.
JSON post rows include `reaction_count`, `total_comment_count`, and `comments_found` in addition to the full `comments` array. Set `max_comments` high, or use `0`/`null` for no scraper-side comment/reply limit. Set `comment_expand_rounds` to `0`/`null` to keep expanding until no more comment/reply controls are found.
