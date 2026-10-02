"""
Reuse an Instagram session you're already logged into in your browser.

Why this exists: creating a fresh account and logging in through instaloader
tends to trip Instagram's checkpoint/bot detection immediately. An account
you already use normally in a browser has none of that friction — it's
already authenticated, already trusted. This borrows that session's cookies
so the scraper can ride on it. No password is involved at any point.

Two ways in, in order of preference:

  1. Automatic — read them out of the browser's own cookie store.
     Chrome 127+ encrypts cookies with App-Bound Encryption, so on Windows
     this needs an elevated terminal:

         python src/discovery/import_browser_session.py --browser chrome

  2. Manual — if you'd rather not run anything as admin, copy the two
     cookie values out of DevTools yourself (instructions printed by
     --help-manual) into a small JSON file, then:

         python src/discovery/import_browser_session.py --from-file mycookies.json

Either way this writes an instaloader-compatible session file, after which
everything else works through the existing flag:

    python src/find_creators.py -k data/keywords.txt --login <username>

Security notes, because a sessionid is as powerful as a password:
  * The cookie value is never printed — output is always masked.
  * The session file lands wherever instaloader keeps them, readable only
    by your user account. Treat it like a password: don't commit it, don't
    paste it into a chat window, and log the account out of Instagram
    ("Log out of all sessions") if you think it leaked.
"""

import argparse
import json
import os
import pickle
import sys

REQUIRED = ("sessionid", "csrftoken")
USEFUL = ("sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "datr", "rur")

MANUAL_HELP = """
How to copy the cookies by hand (no admin needed):

  1. Open Chrome and go to https://www.instagram.com/ (logged in).
  2. Press F12 to open DevTools.
  3. Go to the "Application" tab (you may need the >> arrow to find it).
  4. In the left sidebar: Storage -> Cookies -> https://www.instagram.com
  5. Find these rows and copy each "Value":
         sessionid
         csrftoken
         ds_user_id     (optional but useful)
  6. Make a file called mycookies.json next to this project, containing:

     {
       "sessionid": "PASTE_HERE",
       "csrftoken": "PASTE_HERE",
       "ds_user_id": "PASTE_HERE"
     }

  7. Run:
         python src/discovery/import_browser_session.py --from-file mycookies.json

  8. Delete mycookies.json afterwards — the session file is what matters now.

Do not paste these values into a chat window. A sessionid grants access to
the account exactly like a password does.
"""


def mask(value):
    if not value:
        return "(empty)"
    return f"{value[:4]}...{value[-3:]} (len {len(value)})"


def from_browser(browser):
    try:
        import browser_cookie3
    except ImportError:
        sys.exit("browser_cookie3 is not installed:  pip install browser_cookie3")

    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        sys.exit(f"unknown browser {browser!r} — try chrome, edge, firefox, brave")

    try:
        jar = loader(domain_name="instagram.com")
    except Exception as exc:  # noqa: BLE001 — surface the real cause plainly
        name = type(exc).__name__
        if "Admin" in name or "admin" in str(exc):
            sys.exit(
                f"{browser} cookies are encrypted and need an elevated terminal.\n\n"
                f"  Either: right-click PowerShell -> 'Run as administrator', cd back\n"
                f"  to this folder, and re-run this same command.\n\n"
                f"  Or:     python src/discovery/import_browser_session.py --help-manual\n"
                f"          to copy the two values by hand instead (no admin needed)."
            )
        sys.exit(f"could not read {browser} cookies: {name}: {exc}")

    return {c.name: c.value for c in jar}


def from_file(path):
    if not os.path.exists(path):
        sys.exit(f"no such file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            sys.exit(f"{path} isn't valid JSON: {exc}")
    if not isinstance(data, dict):
        sys.exit(f"{path} should contain a JSON object of cookie name -> value")
    return {k: str(v) for k, v in data.items()}


def resolve_username(cookies, given):
    """Ask Instagram who these cookies belong to, so we name the file right."""
    if given:
        return given

    user_id = cookies.get("ds_user_id")
    if not user_id:
        return None

    import requests
    try:
        r = requests.get(
            f"https://i.instagram.com/api/v1/users/{user_id}/info/",
            headers={
                "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/131.0.0.0 Safari/537.36"),
                "x-ig-app-id": "936619743392459",
            },
            cookies={k: v for k, v in cookies.items() if k in USEFUL},
            timeout=20,
        )
        if r.status_code == 200:
            return (r.json().get("user") or {}).get("username")
    except Exception:  # noqa: BLE001 — this is a convenience, not a requirement
        pass
    return None


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Import an Instagram session from your browser.")
    p.add_argument("--browser", default=None,
                   help="chrome, edge, firefox, brave — read cookies automatically")
    p.add_argument("--from-file", dest="from_file",
                   help="JSON file of cookie name -> value, copied from DevTools")
    p.add_argument("--username", help="the account these cookies belong to "
                    "(auto-detected if omitted)")
    p.add_argument("--out", help="explicit path for the session file")
    p.add_argument("--help-manual", action="store_true",
                   help="print the manual DevTools copy instructions and exit")
    args = p.parse_args(argv)

    if args.help_manual:
        print(MANUAL_HELP)
        return 0
    if not args.browser and not args.from_file:
        p.error("pick one: --browser chrome   or   --from-file mycookies.json "
                "(or --help-manual)")

    cookies = from_file(args.from_file) if args.from_file else from_browser(args.browser)

    if not cookies:
        sys.exit("No instagram.com cookies found. Make sure you're logged in "
                 "to Instagram in that browser, then try again.")

    print(f"Found {len(cookies)} instagram.com cookie(s): {sorted(cookies)}")
    missing = [k for k in REQUIRED if not cookies.get(k)]
    if missing:
        sys.exit(
            f"\nMissing {', '.join(missing)} — that means this browser isn't "
            f"logged in to Instagram (or only has a guest session).\n"
            f"Log in at https://www.instagram.com/ in that browser first."
        )

    for key in USEFUL:
        if key in cookies:
            print(f"  {key:<12} {mask(cookies[key])}")

    username = resolve_username(cookies, args.username)
    if not username:
        sys.exit("\nCouldn't work out the username for these cookies. "
                 "Re-run with --username <your_account_name>.")
    print(f"\nSession belongs to: {username}")

    keep = {k: v for k, v in cookies.items() if k in USEFUL}
    out = args.out
    if not out:
        from instaloader.instaloader import get_default_session_filename
        out = get_default_session_filename(username)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        pickle.dump(keep, f)

    print(f"Saved session to {out}")
    print(f"\nNow run:\n"
          f"    python src/find_creators.py -k data/keywords.txt --login {username}")
    if args.from_file:
        print(f"\nYou can delete {args.from_file} now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
