"""
A resilient anonymous HTTP client for Instagram.

This is the part Apify-style scrapers get right, minus the one piece that
costs money. Their reliability comes from four things:

    1. guest sessions      — real browser cookies, not bare headers
    2. fingerprint rotation — consistent, plausible header sets
    3. adaptive throttling  — back off on the first warning, not the tenth
    4. proxy rotation       — spread load across many IPs

We do 1-3 for free. We can't do 4 without proxies, and 4 is the only one that
raises the ceiling — the anonymous quota is enforced per IP, so no amount of
header craft creates extra allowance. What 1-3 buy is *not tripping the limit
as fast*, and recovering gracefully when we do.

The practical consequence: this client is deliberately slow. A block costs
30+ minutes of total downtime, so a request that waits two seconds is always
cheaper than one that gets us walled. State persists to disk so a block is
still remembered after the process exits — otherwise every run would
rediscover it the hard way.
"""

import json
import os
import random
import time

import requests

HOME = "https://www.instagram.com/"

# Real Chrome/Safari builds. The sec-ch-ua header has to agree with the UA
# string — a Chrome 131 UA sending Chrome 119 hints is a cheap tell.
FINGERPRINTS = [
    {
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "ch_ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "platform": '"Windows"',
    },
    {
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
        "ch_ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "platform": '"Windows"',
    },
    {
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "ch_ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
        "platform": '"macOS"',
    },
    {
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/17.1 Safari/605.1.15"),
        "ch_ua": None,  # Safari doesn't send client hints
        "platform": None,
    },
]

# Instagram's own "slow down" wording. Seeing this means stop, not retry.
BLOCK_MARKERS = (
    "please wait a few minutes",
    "rate limited",
    "try again later",
)


class RateLimitState:
    """
    Remembers, across runs, when Instagram last told us to back off.

    Without this every fresh process would burn another request to rediscover
    an active block — and each of those probes plausibly extends it.
    """

    def __init__(self, path):
        self.path = path
        self.blocked_until = 0.0
        self.consecutive_blocks = 0
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.blocked_until = float(data.get("blocked_until", 0))
            self.consecutive_blocks = int(data.get("consecutive_blocks", 0))
        except (ValueError, OSError):
            pass

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"blocked_until": self.blocked_until,
                       "consecutive_blocks": self.consecutive_blocks}, f)

    @property
    def seconds_remaining(self):
        return max(0.0, self.blocked_until - time.time())

    def record_block(self):
        """
        Exponential cooldown: 15m, 30m, 60m, capped at 2h. Repeated blocks
        mean the IP is in deeper trouble, so backing off harder each time is
        the only lever we have left.
        """
        self.consecutive_blocks += 1
        minutes = min(15 * (2 ** (self.consecutive_blocks - 1)), 120)
        self.blocked_until = time.time() + minutes * 60
        self._save()
        return minutes

    def record_success(self):
        if self.consecutive_blocks or self.blocked_until:
            self.consecutive_blocks = 0
            self.blocked_until = 0.0
            self._save()


class Blocked(Exception):
    """Instagram is refusing anonymous traffic from this IP for now."""

    def __init__(self, message, seconds_remaining=0):
        super().__init__(message)
        self.seconds_remaining = seconds_remaining


class InstagramClient:
    """
    A polite anonymous client: guest cookies, rotating fingerprints, jittered
    pacing, and a persistent cooldown after a block.
    """

    def __init__(self, settings=None, state_path="data/run/ratelimit.json"):
        settings = settings or {}
        self.settings = settings
        self.timeout = settings.get("timeout", 20)
        self.min_delay = settings.get("min_delay", 2.5)
        self.max_delay = settings.get("max_delay", 5.0)
        self.rotate_after = settings.get("rotate_after", 25)
        self.proxies = settings.get("proxies") or {}
        self.state = RateLimitState(state_path)

        self.session = None
        self.fingerprint = None
        self.requests_made = 0
        self._last_request = 0.0

    # -- session management -------------------------------------------------

    def _new_session(self):
        """
        Start a fresh guest session the way a browser would: load the
        homepage first so Instagram issues us csrftoken / mid / ig_did /
        datr, then reuse those on the API calls.
        """
        fp = random.choice(FINGERPRINTS)
        session = requests.Session()
        if self.proxies:
            session.proxies.update(self.proxies)

        headers = {
            "user-agent": fp["ua"],
            "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"),
            "accept-language": "en-US,en;q=0.9",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "upgrade-insecure-requests": "1",
        }
        if fp["ch_ua"]:
            headers.update({
                "sec-ch-ua": fp["ch_ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": fp["platform"],
            })
        session.headers.update(headers)

        try:
            session.get(HOME, timeout=self.timeout)
        except requests.RequestException:
            # A failed bootstrap isn't fatal — the API calls may still work,
            # just without guest cookies.
            pass

        self.session = session
        self.fingerprint = fp
        self.requests_made = 0
        return session

    def _api_headers(self, app_id, referer):
        csrf = self.session.cookies.get("csrftoken", "") if self.session else ""
        headers = {
            "x-ig-app-id": app_id,
            "x-requested-with": "XMLHttpRequest",
            "x-ig-www-claim": "0",
            "accept": "*/*",
            "referer": referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if csrf:
            headers["x-csrftoken"] = csrf
        return headers

    # -- pacing -------------------------------------------------------------

    def _respect_cooldown(self):
        remaining = self.state.seconds_remaining
        if remaining > 0:
            raise Blocked(
                f"still cooling down from a previous block — "
                f"{int(remaining // 60)}m {int(remaining % 60)}s left",
                seconds_remaining=remaining,
            )

    def _pace(self):
        """Jittered gap between requests. Uniform timing is itself a tell."""
        elapsed = time.time() - self._last_request
        wait = random.uniform(self.min_delay, self.max_delay) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    # -- the one entry point ------------------------------------------------

    def get(self, url, params=None, app_id=None, referer=HOME, allow_status=()):
        """
        Perform a paced, fingerprinted GET.

        Raises Blocked as soon as Instagram signals a limit — callers should
        stop entirely rather than retry, because retrying is what turns a
        15-minute cooldown into an hour.
        """
        self._respect_cooldown()

        if self.session is None or self.requests_made >= self.rotate_after:
            self._new_session()

        self._pace()

        headers = self._api_headers(app_id, referer) if app_id else {}
        try:
            response = self.session.get(url, params=params, headers=headers,
                                        timeout=self.timeout)
        except requests.RequestException as exc:
            raise Blocked(f"network error: {exc}") from exc

        self.requests_made += 1

        if response.status_code in (401, 429) or (
            response.status_code == 403 and "login" in response.text.lower()
        ):
            minutes = self.state.record_block()
            raise Blocked(
                f"Instagram returned {response.status_code} "
                f"('{_short_message(response)}'). Backing off {minutes} minutes.",
                seconds_remaining=minutes * 60,
            )

        body_lower = response.text[:400].lower()
        if any(marker in body_lower for marker in BLOCK_MARKERS):
            minutes = self.state.record_block()
            raise Blocked(f"soft rate-limit in the response body. "
                          f"Backing off {minutes} minutes.",
                          seconds_remaining=minutes * 60)

        if response.status_code == 200 or response.status_code in allow_status:
            self.state.record_success()
        return response


def _short_message(response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get("message", ""))[:80]
    except ValueError:
        pass
    return response.text[:80].replace("\n", " ")
