from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright


FACEBOOK_HOME = "https://www.facebook.com/"
DEFAULT_ENV_FILE = ".env"
EMAIL_SELECTOR = "input[name='email'], input#email"


def load_env_file(path: str = DEFAULT_ENV_FILE) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    load_env_file()
    parser = argparse.ArgumentParser(
        description="Open Facebook, reuse a local login session, and navigate to a group."
    )
    parser.add_argument(
        "--group-url",
        default=os.getenv("FACEBOOK_GROUP_URL"),
        help="Full Facebook group URL, for example https://www.facebook.com/groups/123456789",
    )
    parser.add_argument(
        "--profile-dir",
        default=os.getenv("BROWSER_PROFILE_DIR", "browser-profile"),
        help="Directory where Playwright stores the persistent browser profile.",
    )
    parser.add_argument(
        "--keep-open",
        action=argparse.BooleanOptionalAction,
        default=os.getenv("KEEP_OPEN", "false").lower() in {"1", "true", "yes", "on"},
        help="Keep the browser open after navigating to the group.",
    )
    parser.add_argument(
        "--facebook-email",
        default=os.getenv("FACEBOOK_EMAIL"),
        help="Optional email/phone to prefill on Facebook's login page.",
    )
    return parser.parse_args()


def validate_facebook_group_url(group_url: str) -> str:
    parsed = urlparse(group_url)
    host = parsed.netloc.lower()

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Group URL must start with http:// or https://.")

    if host not in {"facebook.com", "www.facebook.com", "m.facebook.com"}:
        raise ValueError("Group URL must be on facebook.com.")

    if not parsed.path.startswith("/groups/"):
        raise ValueError("Group URL path must start with /groups/.")

    return group_url


def is_login_or_checkpoint(page: Page) -> bool:
    url = page.url.lower()
    return "/login" in url or "/checkpoint" in url


def prefill_login_email(page: Page, email: str | None) -> None:
    if not email:
        return

    email_input = page.locator(EMAIL_SELECTOR).first
    try:
        email_input.wait_for(state="visible", timeout=5_000)
        email_input.fill(email)
        print("Prefilled Facebook email. Enter your password/2FA manually in the browser.")
    except Exception:
        print("Could not find a visible Facebook email field to prefill.")


def main() -> None:
    args = parse_args()
    if not args.group_url:
        raise SystemExit(
            "Missing group URL. Add FACEBOOK_GROUP_URL to .env or pass --group-url."
        )

    group_url = validate_facebook_group_url(args.group_url)
    profile_dir = Path(args.profile_dir).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = context.pages[0] if context.pages else context.new_page()

        print("Opening Facebook. If prompted, log in manually in the browser window.")
        page.goto(FACEBOOK_HOME, wait_until="domcontentloaded")

        if is_login_or_checkpoint(page):
            prefill_login_email(page, args.facebook_email)
            input("After you finish login/security checks in the browser, press Enter here...")

        print(f"Opening group: {group_url}")
        page.goto(group_url, wait_until="domcontentloaded")

        if args.keep_open:
            input("Browser is open. Press Enter here to close it...")

        context.close()


if __name__ == "__main__":
    main()
