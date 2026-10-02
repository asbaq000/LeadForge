"""One-time interactive login. Opens a real browser window; you log in by hand.

Your session lives in a PERSISTENT Chrome profile (auth/profile/) that every
later headless run reuses. That matters for account safety: a stable device
fingerprint across sessions looks like one person on one machine, which is
exactly what it is. Regenerating a fresh browser identity each run is a tell.

Nothing here ever sees or stores your password - you type it into the real
Threads login page, and only the resulting session cookies are cached locally.

    python scripts/login.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from threads_leads.collector import USER_AGENT, _STEALTH_JS  # noqa: E402
from threads_leads.config import load_settings, proxy_config  # noqa: E402

LOGIN_URL = "https://www.threads.com/login"
CHECK_URL = "https://www.threads.com/search?q=automation"


def main() -> int:
    settings = load_settings()
    profile_dir = Path(settings.user_data_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    backup = Path(settings.storage_state)
    backup.parent.mkdir(parents=True, exist_ok=True)

    print("Opening a browser window.")
    print("  1. Log in to Threads (your Instagram credentials).")
    print("  2. Complete any 2FA prompt.")
    print("  3. Wait until your Threads feed is visible.")
    print("  4. Come back here and press ENTER.\n")
    print("Use a SECONDARY account dedicated to prospecting, never your main one.")
    print("Your password is typed into Threads directly - this script never sees it.\n")

    launch_kwargs: dict = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "user_agent": USER_AGENT,
        "viewport": {"width": 1440, "height": 900},
        "locale": settings.locale,
        "timezone_id": settings.timezone_id,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    proxy = proxy_config(settings)
    if proxy:
        # Log in through the same egress the scraper will use, so the account
        # never sees a sudden change of network.
        launch_kwargs["proxy"] = proxy
        print(f"Using proxy {proxy['server']}\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(**launch_kwargs)
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        input("Press ENTER once you are logged in and can see your feed... ")

        # Confirm search actually works before saving - that is what the scraper needs.
        ok = True
        try:
            page.goto(CHECK_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(3000)
            if "/login" in page.url.lower():
                print("\nStill on the login page. Session NOT usable. Try again.")
                ok = False
        except Exception as exc:
            print(f"\nCould not verify search access: {exc}")

        if ok:
            try:
                ctx.storage_state(path=str(backup))
            except Exception:
                pass
        ctx.close()

    if not ok:
        return 1

    print(f"\nProfile saved to {profile_dir}")
    print(f"Cookie backup   {backup}")
    print("\nNext:  python run.py check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
