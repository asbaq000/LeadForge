"""Keeping an X session alive across runs, and getting one when there is none.

The contract the rest of the package relies on:

    storage state exists and works  ->  use it, headless, no window
    it does not, or has expired     ->  open a real Chromium window at the X
                                        login page, wait for a person to sign
                                        in, save the session, carry on

Nothing here ever asks for a password, and no credentials are stored — the
sign-in happens in the browser window, and only the resulting cookies are kept.

Two things learned the hard way and easy to "fix" back into breakage:

1. `auth_token` in the cookie jar does NOT mean the session works. Cookies
   outlive the sessions they belong to, so `verify()` asks X rather than
   trusting the jar. A logged-out request to /home redirects to the splash
   page, which is the reliable signal.
2. The login window must be a *persistent context*. X sets part of its session
   during an interstitial (2FA, "unusual activity" checks), and a throwaway
   context loses whatever was written before the final redirect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from x_leads.errors import BrowserMissing, LoginTimeout, NotSignedIn

LOGGER = logging.getLogger(__name__)

# Everything session-related lives together so "sign me out" is one rmtree.
STATE_DIR = Path(os.getenv("X_LEADS_HOME") or (Path.home() / ".x_lead_scraper"))
SESSION_PATH = STATE_DIR / "session.json"
LOGIN_PROFILE_DIR = STATE_DIR / "login_profile"

LOGIN_URL = "https://x.com/i/flow/login"
HOME_URL = "https://x.com/home"

# The cookie X issues to a signed-in account. `ct0` (the CSRF token) is handed
# to logged-out visitors too, so it is not evidence of anything on its own.
AUTH_COOKIE = "auth_token"

# Sessions last months in practice, but a stale file that *looks* fine wastes a
# run before failing. Anything older than this is re-verified rather than
# trusted outright.
STALE_AFTER_DAYS = 25

INSTALL_HELP = """
Playwright and its Chromium build are required:

    python -m pip install playwright
    python -m playwright install chromium
""".strip()


def _load_playwright():
    try:
        from playwright.async_api import async_playwright
        return async_playwright
    except ImportError as e:
        raise BrowserMissing(INSTALL_HELP) from e


def _is_x_domain(domain: str) -> bool:
    d = (domain or "").lstrip(".")
    return d in ("x.com", "twitter.com") or d.endswith((".x.com", ".twitter.com"))


class SessionManager:
    """Owns the saved storage state: reading it, checking it, replacing it."""

    def __init__(self, session_path: Path | None = None):
        self.session_path = Path(session_path or SESSION_PATH)

    # ------------------------------------------------------------- inspection
    def load(self) -> dict | None:
        """The saved storage state, or None when there isn't a usable one."""
        if not self.session_path.exists():
            return None
        try:
            state = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            LOGGER.warning("Saved session is unreadable (%s)", type(e).__name__)
            return None

        if not isinstance(state, dict) or not state.get("cookies"):
            LOGGER.warning("Saved session has no cookies in it")
            return None

        if not self._has_auth_cookie(state["cookies"]):
            LOGGER.warning("Saved session has no %s cookie", AUTH_COOKIE)
            return None
        return state

    @staticmethod
    def _has_auth_cookie(cookies: list[dict]) -> bool:
        now = time.time()
        for c in cookies:
            if c.get("name") != AUTH_COOKIE or not _is_x_domain(c.get("domain", "")):
                continue
            # -1 means a session cookie: no stated expiry, still usable here
            # because Playwright replays it into the new context.
            expires = c.get("expires", -1)
            if expires in (-1, 0, None) or expires > now:
                return True
        return False

    def exists(self) -> bool:
        return self.load() is not None

    def age_days(self) -> float | None:
        try:
            return (time.time() - self.session_path.stat().st_mtime) / 86400
        except OSError:
            return None

    def looks_stale(self) -> bool:
        age = self.age_days()
        return age is not None and age > STALE_AFTER_DAYS

    def forget(self) -> bool:
        """Sign out: drop the saved state and the login profile with it."""
        import shutil

        removed = False
        try:
            self.session_path.unlink()
            removed = True
        except FileNotFoundError:
            pass
        if LOGIN_PROFILE_DIR.exists():
            shutil.rmtree(LOGIN_PROFILE_DIR, ignore_errors=True)
            removed = True
        return removed

    # ---------------------------------------------------------------- writing
    async def save_async(self, context) -> int:
        """Persist an authenticated context's cookies and local storage.

        Returns how many X cookies were captured; 0 means the sign-in did not
        actually take, and the caller should say so rather than claim success.
        """
        state = await context.storage_state()
        cookies = [
            c for c in state.get("cookies", []) if _is_x_domain(c.get("domain", ""))
        ]
        if not self._has_auth_cookie(cookies):
            return 0

        state["cookies"] = cookies
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        try:  # best effort; POSIX only
            self.session_path.chmod(0o600)
        except OSError:
            pass

        LOGGER.info("Session saved (%d cookies) to %s", len(cookies), self.session_path)
        return len(cookies)


async def verify(context, timeout_ms: int = 30_000) -> bool:
    """Ask X whether this context is actually signed in.

    Loads /home, which only a signed-in account is served. A logged-out request
    is redirected to the splash page, and that redirect is what gets checked —
    it is the one signal a stale cookie cannot fake.
    """
    page = await context.new_page()
    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        # Give the SPA a moment to decide whether to bounce us.
        await page.wait_for_timeout(2500)
        url = page.url
        if "/i/flow/login" in url or "/login" in url or url.rstrip("/") == "https://x.com":
            return False
        try:
            await page.wait_for_selector(
                '[data-testid="SideNav_NewTweet_Button"], '
                '[data-testid="tweetTextarea_0"], '
                '[data-testid="primaryColumn"]',
                timeout=8000,
            )
            return True
        except Exception:
            return "/home" in page.url
    except Exception as e:
        LOGGER.warning("Session check failed: %s", type(e).__name__)
        return False
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _has_live_auth(context) -> bool:
    try:
        cookies = await context.cookies()
    except Exception:
        return False
    return any(
        c.get("name") == AUTH_COOKIE and _is_x_domain(c.get("domain", ""))
        for c in cookies
    )


async def interactive_login(
    manager: SessionManager,
    timeout_s: int = 300,
    poll_s: float = 2.0,
) -> int:
    """Open a real browser window, wait for the sign-in, save the session.

    Returns the number of cookies saved. Raises LoginTimeout if nobody signed
    in before the deadline.
    """
    async_playwright = _load_playwright()

    LOGIN_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print()
    print("=" * 70)
    print("  No usable X session — opening a browser window to sign in.")
    print()
    print("  Sign in normally in that window, including any 2FA step.")
    print("  This notices when you're done by itself and closes the window.")
    print(f"  It gives up after {timeout_s // 60} minutes.")
    print()
    print("  Only the resulting cookies are saved, to:")
    print(f"    {manager.session_path}")
    print("  That file is as good as a password — keep it off shared drives.")
    print("=" * 70)
    print(flush=True)

    async with async_playwright() as p:
        # Persistent, headful, and deliberately plain: X flags automation
        # tells, and every extra launch arg is one more thing to fingerprint.
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(LOGIN_PROFILE_DIR),
            headless=False,
            viewport=None,
            args=["--window-position=80,60", "--window-size=1200,900"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

            waited = 0.0
            while waited < timeout_s:
                if await _has_live_auth(context):
                    # Cookies can land mid-redirect during SSO/2FA, before the
                    # account is really in. Confirm with X before saving.
                    if await verify(context):
                        saved = await manager.save_async(context)
                        if saved:
                            print(f"  Signed in — session saved ({saved} cookies).\n")
                            return saved
                await asyncio.sleep(poll_s)
                waited += poll_s

            raise LoginTimeout(
                f"Nobody completed the sign-in within {timeout_s}s. Nothing was saved."
            )
        finally:
            try:
                await context.close()
            except Exception:
                pass


async def _state_works(state: dict) -> bool:
    """Spin up a throwaway headless context and check the saved state in it."""
    async_playwright = _load_playwright()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                storage_state=state,
                viewport={"width": 1280, "height": 900},
            )
            return await verify(context)
        except Exception as e:
            LOGGER.warning("Could not validate saved session: %s", type(e).__name__)
            return False
        finally:
            await browser.close()


async def ensure_session(
    manager: SessionManager | None = None,
    allow_login: bool = True,
    login_timeout_s: int = 300,
    force_login: bool = False,
    verify_saved: bool = True,
) -> dict:
    """The storage state to run with, signing in first if that's needed.

    This is the single entry point the scraper uses. `verify_saved` costs one
    extra browser launch (~4s); it is worth it on a scheduled run, where a
    dead session would otherwise look like "X returned no results", and
    skippable when a person is watching the output.
    """
    manager = manager or SessionManager()

    if not force_login:
        state = manager.load()
        if state is not None:
            if not verify_saved and not manager.looks_stale():
                LOGGER.info("Using saved X session (unverified)")
                return state
            if await _state_works(state):
                LOGGER.info("Using saved X session (%s)", manager.session_path)
                return state
            LOGGER.warning("Saved session is no longer valid — signing in again.")

    if not allow_login:
        raise NotSignedIn(
            "No valid X session, and sign-in is disabled (--no-login). "
            "Run `x-leads --login` once from a desktop session."
        )

    await interactive_login(manager, timeout_s=login_timeout_s)
    state = manager.load()
    if state is None:
        raise NotSignedIn("Sign-in reported success but no session was saved.")
    return state
