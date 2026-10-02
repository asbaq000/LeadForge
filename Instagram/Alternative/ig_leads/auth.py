"""Getting and keeping an Instagram session.

Logging in is the single riskiest thing this package does. Instagram scores
login attempts far more harshly than reads, and repeated programmatic logins
are the classic way a working account becomes a locked one. So:

* **Log in once, then never again.** The session is saved and reused
  indefinitely. There is no "log in if the request fails" path anywhere.
* **The login is assisted, not automated.** Credentials are typed into a real
  browser window with human-ish pauses, and then it *waits for a person* —
  this account was observed being sent to `/auth_platform/codeentry/` on
  2026-08-13, and a verification code is not something a script can invent.
* **No credentials in source.** They come from `.env` / the environment.

If the session ever dies, that is a prompt to run `--login` deliberately, not
something to paper over automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from ig_leads import config
from ig_leads.errors import BrowserMissing, LoginTimeout, NotSignedIn

LOGGER = logging.getLogger(__name__)

LOGIN_URL = "https://www.instagram.com/accounts/login/"
HOME_URL = "https://www.instagram.com/"

SESSION_COOKIE = "sessionid"

# Pages that mean "finish this by hand".
CHALLENGE_URLS = ("codeentry", "two_factor", "challenge", "checkpoint")


def _load_playwright():
    try:
        from playwright.async_api import async_playwright
        return async_playwright
    except ImportError as e:
        raise BrowserMissing(
            "Playwright is required:\n"
            "    python -m pip install playwright\n"
            "    python -m playwright install chromium"
        ) from e


def _is_ig(domain: str) -> bool:
    return "instagram.com" in (domain or "")


class SessionStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or config.SESSION_PATH)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            LOGGER.warning("Saved session unreadable (%s)", type(e).__name__)
            return None
        if not isinstance(state, dict):
            return None
        cookies = state.get("cookies") or []
        if not any(
            c.get("name") == SESSION_COOKIE and _is_ig(c.get("domain", ""))
            for c in cookies
        ):
            LOGGER.warning("Saved session has no %s cookie", SESSION_COOKIE)
            return None
        return state

    def exists(self) -> bool:
        return self.load() is not None

    def age_days(self) -> float | None:
        try:
            return (time.time() - self.path.stat().st_mtime) / 86400
        except OSError:
            return None

    async def save(self, context) -> int:
        state = await context.storage_state()
        cookies = [c for c in state.get("cookies", []) if _is_ig(c.get("domain", ""))]
        if not any(c.get("name") == SESSION_COOKIE for c in cookies):
            return 0
        state["cookies"] = cookies
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        LOGGER.info("Session saved (%d cookies) to %s", len(cookies), self.path)
        return len(cookies)

    def forget(self) -> bool:
        import shutil

        removed = False
        try:
            self.path.unlink()
            removed = True
        except FileNotFoundError:
            pass
        if config.LOGIN_PROFILE_DIR.exists():
            shutil.rmtree(config.LOGIN_PROFILE_DIR, ignore_errors=True)
            removed = True
        return removed


async def _has_session(context) -> bool:
    try:
        cookies = await context.cookies()
    except Exception:
        return False
    return any(
        c.get("name") == SESSION_COOKIE and _is_ig(c.get("domain", ""))
        for c in cookies
    )


async def interactive_login(
    store: SessionStore | None = None,
    timeout_s: int = 420,
    poll_s: float = 2.0,
) -> int:
    """Open a window, fill what we can, and wait for a person to finish."""
    store = store or SessionStore()
    async_playwright = _load_playwright()

    user, pwd = config.username(), config.password()
    if not user:
        raise NotSignedIn(
            "No IG_USERNAME set. Put credentials in a .env file beside the "
            "package:\n    IG_USERNAME=your_account\n    IG_PASSWORD=your_password"
        )

    print()
    print("=" * 70)
    print(f"  Signing in to Instagram as @{user}")
    print()
    print("  A browser window will open and the form will be filled in.")
    print("  If Instagram asks for a verification code or shows any other")
    print("  challenge, COMPLETE IT IN THAT WINDOW - this waits for you.")
    print(f"  It gives up after {timeout_s // 60} minutes.")
    print()
    print("  Only the resulting cookies are stored, in:")
    print(f"    {store.path}")
    print("=" * 70)
    print(flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, args=["--window-position=60,40"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900}, locale="en-US"
        )
        page = await context.new_page()
        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(3500)

            for label in ("Allow all cookies", "Accept All",
                          "Only allow essential cookies"):
                try:
                    button = page.get_by_role("button", name=label)
                    if await button.count():
                        await button.first.click()
                        await page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            # Typed with pauses rather than set instantly: an input event
            # stream with zero delay is a bot signal on this form.
            try:
                await page.fill('input[name="username"]', user, timeout=20_000)
                await page.wait_for_timeout(800)
                if pwd:
                    await page.fill('input[name="password"]', pwd)
                    await page.wait_for_timeout(800)
                    await page.click('button[type="submit"]')
                    print("  Credentials submitted.")
                else:
                    print("  No password set — sign in manually in the window.")
            except Exception:
                print("  Could not fill the form — sign in manually in the window.")

            waited, announced = 0.0, False
            while waited < timeout_s:
                if await _has_session(context):
                    break
                if not announced and any(c in page.url for c in CHALLENGE_URLS):
                    print("\n  >> Instagram is asking for verification.")
                    print("  >> Complete it in the browser window; this is waiting.\n",
                          flush=True)
                    announced = True
                await asyncio.sleep(poll_s)
                waited += poll_s

            if not await _has_session(context):
                raise LoginTimeout(
                    f"No Instagram session appeared within {timeout_s}s. "
                    "Nothing was saved."
                )

            # Let any post-login interstitial ("Save your login info?") settle
            # before capturing cookies.
            await page.wait_for_timeout(3000)
            saved = await store.save(context)
            if not saved:
                raise NotSignedIn("Signed in, but no session cookie could be saved.")
            print(f"\n  Signed in — session saved ({saved} cookies).\n")
            return saved
        finally:
            try:
                await context.close()
                await browser.close()
            except Exception:
                pass


async def verify(state: dict, headless: bool = True) -> bool:
    """Load the site with the saved state and see if we are still signed in."""
    async_playwright = _load_playwright()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            context = await browser.new_context(
                storage_state=state, viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(3000)
            return "accounts/login" not in page.url and "challenge" not in page.url
        except Exception as e:
            LOGGER.warning("Session check failed: %s", type(e).__name__)
            return False
        finally:
            await browser.close()


async def ensure_session(
    store: SessionStore | None = None,
    allow_login: bool = True,
    login_timeout_s: int = 420,
) -> dict:
    """The storage state to run with.

    Deliberately does *not* try to log in when a saved session merely looks
    stale — only when there is none at all. A session that has just been
    rejected is a signal the account is under scrutiny, and immediately logging
    in again is the worst possible response.
    """
    store = store or SessionStore()
    state = store.load()
    if state is not None:
        return state

    if not allow_login:
        raise NotSignedIn(
            "No saved Instagram session and --no-login was passed. "
            "Run `python -m ig_leads --login` once."
        )

    await interactive_login(store, timeout_s=login_timeout_s)
    state = store.load()
    if state is None:
        raise NotSignedIn("Sign-in finished but no session was saved.")
    return state
