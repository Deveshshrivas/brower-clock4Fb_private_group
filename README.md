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
