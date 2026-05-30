# Facebook Group Browser

Small Playwright project that opens Facebook in a persistent local browser profile, lets you log in manually, and then navigates to a Facebook group.

This project does not bypass CAPTCHAs, bot detection, or Facebook security checks. Use it with your own account and follow Facebook's terms.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## Run

Create or edit `.env`:

```powershell
FACEBOOK_GROUP_URL=https://www.facebook.com/groups/964040666405959
BROWSER_PROFILE_DIR=browser-profile
KEEP_OPEN=true
FACEBOOK_EMAIL=your-email-or-phone
```

Then run:

```powershell
python facebook_group_browser.py
```

On the first run, the script can prefill your email. Enter your password and any security checks manually in the browser window. After login, press Enter in the terminal and the script will navigate to the group.

Your session is saved in `browser-profile/`, so future runs should already be logged in unless Facebook expires the session.

## Options

```powershell
python facebook_group_browser.py --group-url "https://www.facebook.com/groups/YOUR_GROUP_ID" --profile-dir ".\browser-profile" --keep-open
```

- `--group-url`: Full Facebook group URL to open.
- `--profile-dir`: Local folder used for cookies and session data.
- `--keep-open` / `--no-keep-open`: Keep the browser open until you press Enter again.
- `--facebook-email`: Optional email/phone to prefill on the login page.

Do not store your Facebook password in `.env`. This project does not automate Facebook password submission, CAPTCHA, 2FA, or checkpoint screens. The persistent browser profile is the safer way to keep your login available.

## Export Group Posts

After you have logged in once, export visible posts and comments:

```powershell
python fetch_group_data.py
```

Optional `.env` settings:

```powershell
MAX_POSTS=25
MAX_COMMENTS_PER_POST=100
COMMENT_EXPAND_ROUNDS=6
SCROLLS=12
OUTPUT_JSON=fb_group_posts.json
OUTPUT_CSV=fb_group_posts.csv
HEADLESS=false
```

The exporter saves JSON and CSV files. The JSON also includes `group.member_count_text` when Facebook exposes the member count in the loaded page. It can only collect content visible to your logged-in account and loaded in the browser page.
Each JSON post includes `reactions_text`, numeric `reaction_count`, `comment_count_text`, numeric `total_comment_count`, `comments_found`, and the extracted `comments` array. Set `MAX_COMMENTS_PER_POST` or API `max_comments` high, for example `1000`, or use `0`/`null` through the API for no scraper-side comment limit. Use API `comment_expand_rounds: 0` or `null` to keep expanding comments/replies until no more controls are found. Facebook must still load/expand the comments and replies in the browser.

## API

After you have logged in once with the browser profile, start the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn server.app:app --host 0.0.0.0 --port 8000
```

Or use the server helper script:

```powershell
.\server\start.ps1
```

Open the interactive docs:

```text
http://localhost:8000/docs
```

Start a scrape job:

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/scrape" `
  -ContentType "application/json" `
  -Body '{"group_url":"https://www.facebook.com/groups/964040666405959","max_posts":25,"max_comments":1000,"headless":false}'
```

Useful endpoints:

- `GET /health`: API health check.
- `POST /scrape`: Start a background export job.
- `POST /scrape-json`: Run an export and return the scraped JSON in the same response.
- `GET /jobs`: List jobs started since this API process began.
- `GET /jobs/{job_id}`: Check job status and recent logs.
- `GET /jobs/{job_id}/result`: Get exported JSON after the job completes.
- `GET /jobs/{job_id}/posts.csv`: Download post-level CSV.
- `GET /jobs/{job_id}/comments.csv`: Download comment-level CSV.

The API uses the same saved Playwright profile as the command-line exporter. If Facebook asks for login, CAPTCHA, 2FA, or a checkpoint, run `python facebook_group_browser.py` first and finish login manually in the browser.
