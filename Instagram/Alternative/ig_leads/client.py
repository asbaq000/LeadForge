"""The only thing that talks to Instagram.

Every request in this package goes through `IGClient.get_json`, which is what
makes the safety rules unavoidable rather than advisory: pacing, the persisted
budget, and the circuit breaker cannot be bypassed by a caller that forgets.

Three decisions worth understanding before changing anything:

**Requests are made by the real browser, from inside the page.** Not
`requests`, and not Playwright's `APIRequestContext` — both of those have their
own TLS stack and header order, and Instagram fingerprints exactly that. An
in-page `fetch()` carries the genuine Chrome fingerprint, the genuine cookie
jar, and the genuine `sec-*` headers, because it *is* Chrome making the
request. This costs a browser process and is worth it.

**Nothing here ever writes.** No follow, like, comment, DM, or view. Those are
the actions Instagram polices hardest and the ones that get accounts
restricted; this only reads public profile data. Keep it that way.

**Pushback stops the run, permanently.** A 401, 429, checkpoint or
`feedback_required` raises `AccountAtRisk` and the run ends. There is no retry
and no backoff-then-continue, because by the time Instagram says any of those
it has already decided something is wrong, and the next request is what turns
a soft throttle into a hard block.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

from ig_leads.budget import RequestBudget
from ig_leads.config import Pacing
from ig_leads.errors import AccountAtRisk, BrowserMissing, NotSignedIn

LOGGER = logging.getLogger(__name__)

# The public web app id every instagram.com page sends. Not a secret, and
# omitting it is what makes these endpoints return 401 to a plain client.
WEB_APP_ID = "936619743392459"

# Runs inside the page, so this is Chrome's own fetch with Chrome's own
# fingerprint. Returns the parsed body where possible to avoid shipping
# megabyte payloads across the CDP bridge.
_FETCH_JS = """
async ([url]) => {
  let r;
  try {
    r = await fetch(url, {
      headers: {
        'X-IG-App-ID': '%s',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': '*/*',
      },
      credentials: 'include',
    });
  } catch (e) {
    return {status: 0, error: String(e)};
  }
  const text = await r.text();
  let body = null, parseError = null;
  try { body = JSON.parse(text); } catch (e) { parseError = text.slice(0, 300); }
  return {status: r.status, url: r.url, body: body, parseError: parseError};
}
""" % WEB_APP_ID

# Instagram's ways of saying "we have noticed you".
CHALLENGE_MARKERS = ("checkpoint", "challenge", "accounts/suspended", "login")
FEEDBACK_MARKERS = ("feedback_required", "please wait a few minutes",
                    "try again later", "rate limit")

# A 400 that is Instagram's bug, not our problem. Observed on 2026-08-13 for
# perfectly healthy business accounts:
#
#   {"message":"Asset asset://laser.provider/ig_business_category_subvertical
#    has been deleted. You cannot use this schema","status":"fail"}
#
# The account exists and its page loads fine; the profile API just cannot
# serialise that account's business category. It hit 2 of 6 accounts in one
# run, so it must not (a) count towards the "account at risk" streak, or the
# run aborts on a false alarm, and (b) silently lose the candidate.
BENIGN_400_MARKERS = (
    "has been deleted. you cannot use this schema",
    "laser.provider",
)


class ProfileUnavailable(Exception):
    """Instagram cannot serve this profile's API record, but the account is fine."""


class IGClient:
    """Paced, budgeted, read-only access to Instagram's web JSON endpoints.

    Use as an async context manager:

        async with IGClient(state, budget, pacing) as ig:
            data = await ig.get_json(url, label="profile:someone")
    """

    def __init__(
        self,
        storage_state: dict,
        budget: RequestBudget,
        pacing: Pacing | None = None,
        headless: bool = True,
        rng: random.Random | None = None,
    ):
        self.storage_state = storage_state
        self.budget = budget
        self.pacing = pacing or Pacing()
        self.headless = headless
        self._rng = rng or random.Random()

        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

        self.requests_made = 0
        self.failure_streak = 0
        self._first_request = True

    # ------------------------------------------------------------------ setup
    async def start(self) -> "IGClient":
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise BrowserMissing(
                "Playwright is required:\n"
                "    python -m pip install playwright\n"
                "    python -m playwright install chromium"
            ) from e

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            storage_state=self.storage_state,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        self._page = await self._context.new_page()

        # Land on the site first. The endpoints check the Referer and the page
        # origin, and a cold context that jumps straight to /api/v1/ looks
        # nothing like a browsing session.
        await self._page.goto(
            "https://www.instagram.com/", wait_until="domcontentloaded", timeout=60_000
        )
        await self._page.wait_for_timeout(self._rng.randint(2500, 5000))

        if "login" in self._page.url or "accounts/login" in self._page.url:
            raise NotSignedIn(
                "The saved Instagram session is no longer valid. "
                "Run `python -m ig_leads --login` to sign in again."
            )
        LOGGER.info("Instagram session is live")
        return self

    async def close(self):
        for obj, method in (
            (self._context, "close"), (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            try:
                if obj is not None:
                    await getattr(obj, method)()
            except Exception:
                pass
        self._context = self._browser = self._playwright = self._page = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()
        return False

    # ------------------------------------------------------------------ pacing
    async def _pause(self):
        """Wait like a person reading, not like a loop."""
        if self._first_request:
            self._first_request = False
            return

        if (
            self.pacing.long_break_every
            and self.requests_made % self.pacing.long_break_every == 0
        ):
            gap = self._rng.uniform(
                self.pacing.long_break_min_s, self.pacing.long_break_max_s
            )
            LOGGER.info("  taking a longer break (%.0fs)", gap)
        else:
            gap = self._rng.uniform(self.pacing.min_gap_s, self.pacing.max_gap_s)
            LOGGER.debug("  waiting %.1fs", gap)
        await asyncio.sleep(gap)

    # ----------------------------------------------------------------- request
    async def get_json(self, url: str, label: str = "") -> dict[str, Any] | None:
        """One paced, budgeted GET. Returns parsed JSON, or None for a 404.

        Raises `AccountAtRisk` on anything that suggests Instagram has noticed,
        and `BudgetExhausted` when the allowance is gone. Both end the run.
        """
        if self._page is None:
            raise RuntimeError("IGClient not started")

        self.budget.check(1)
        await self._pause()

        result = await self._page.evaluate(_FETCH_JS, [url])
        self.budget.spend(1)
        self.requests_made += 1

        status = result.get("status", 0)

        if status == 200:
            body = result.get("body")
            if body is None:
                # 200 with an unparseable body is usually a login/challenge
                # page served in place of the API response.
                snippet = (result.get("parseError") or "").lower()
                self.failure_streak += 1
                if any(m in snippet for m in FEEDBACK_MARKERS):
                    raise AccountAtRisk(
                        f"Instagram returned a throttle page for {label or url}. "
                        "Stopping so the account is not pushed further."
                    )
                LOGGER.warning("  %s: 200 but body was not JSON", label or url)
                self._check_streak()
                return None
            self.failure_streak = 0
            self._warn_if_feedback(body, label)
            return body

        if status in (401, 403):
            raise AccountAtRisk(
                f"Instagram returned {status} for {label or url}. The session is "
                "rejected or the account is flagged. Stop, leave it alone for a "
                "day, then run `--login` and try a smaller run."
            )
        if status == 429:
            raise AccountAtRisk(
                f"Instagram rate limited the account ({label or url}). Stopping. "
                "Leave it several hours; retrying now is what causes a ban."
            )
        if status == 404:
            LOGGER.debug("  %s: not found", label or url)
            self.failure_streak = 0
            return None

        if status == 400:
            detail = json.dumps(result.get("body") or result.get("parseError") or "").lower()
            if any(m in detail for m in BENIGN_400_MARKERS):
                # Instagram's own serialisation bug. Not a signal about us.
                self.failure_streak = 0
                raise ProfileUnavailable(label or url)

        self.failure_streak += 1
        LOGGER.warning("  %s: HTTP %s", label or url, status)
        self._check_streak()
        return None

    def _warn_if_feedback(self, body: Any, label: str):
        """A 200 can still carry `feedback_required` in the payload."""
        if not isinstance(body, dict):
            return
        message = str(body.get("message", "")).lower()
        status = str(body.get("status", "")).lower()
        if "feedback_required" in message or (status == "fail" and message):
            raise AccountAtRisk(
                f"Instagram replied '{body.get('message')}' for {label}. "
                "This is a soft block — stopping."
            )

    def _check_streak(self):
        if self.failure_streak >= self.pacing.failure_streak_limit:
            raise AccountAtRisk(
                f"{self.failure_streak} failed requests in a row. Instagram "
                "serves soft errors before hard ones, so this is treated as a "
                "block rather than bad luck."
            )

    # Convenience wrappers so callers never build URLs by hand.
    async def profile(self, username: str) -> dict | None:
        return await self.get_json(
            "https://www.instagram.com/api/v1/users/web_profile_info/"
            f"?username={username}",
            label=f"profile:{username}",
        )

    async def recent_posts(self, username: str, count: int = 12) -> dict | None:
        return await self.get_json(
            f"https://www.instagram.com/api/v1/feed/user/{username}/username/"
            f"?count={count}",
            label=f"posts:{username}",
        )

    async def hashtag(self, tag: str) -> dict | None:
        return await self.get_json(
            f"https://www.instagram.com/api/v1/tags/web_info/?tag_name={tag}",
            label=f"#{tag}",
        )

    async def profile_via_page(self, username: str) -> dict | None:
        """Read a profile from its public page instead of the API.

        The fallback for `ProfileUnavailable`. The page's Open Graph tags carry
        follower/following/post counts and the display name, and the bio is in
        the header, so an account the API refuses to serialise is still usable.

        Counts here are ROUNDED by Instagram ("14K Followers"), so the caller
        must treat them as approximate — see `parse_count`.
        """
        if self._page is None:
            raise RuntimeError("IGClient not started")

        self.budget.check(1)
        await self._pause()

        try:
            await self._page.goto(
                f"https://www.instagram.com/{username}/",
                wait_until="domcontentloaded", timeout=45_000,
            )
            await self._page.wait_for_timeout(self._rng.randint(2000, 3500))
            data = await self._page.evaluate(
                """() => {
                    const meta = n => {
                        const el = document.querySelector(`meta[property="${n}"]`);
                        return el ? el.content : null;
                    };
                    const header = document.querySelector('header');
                    return {
                        title: meta('og:title'),
                        description: meta('og:description'),
                        header: header ? header.innerText : '',
                        url: location.href,
                    };
                }"""
            )
        except Exception as e:
            LOGGER.debug("  page fallback failed for %s: %s", username, type(e).__name__)
            data = None
        finally:
            self.budget.spend(1)
            self.requests_made += 1

        if data and "accounts/login" in (data.get("url") or ""):
            raise AccountAtRisk(
                "Instagram redirected a profile page to the login screen — "
                "the session has been rejected mid-run."
            )
        return data
