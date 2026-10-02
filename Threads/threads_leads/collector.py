"""Playwright driver for Threads.

Strategy: let the real page load, but harvest the JSON it renders from rather
than the DOM it renders to. Threads ships post data in two places we can read:

  * inline `<script type="application/json">` blobs on first paint
  * `/graphql/query` XHR responses fired by infinite scroll

Both are intercepted. DOM scraping runs as a third, last-resort net.

Account safety notes baked into this module:
  * a PERSISTENT browser profile, so device/session fingerprint is stable across
    runs - a fresh fingerprint on every login is itself a strong bot signal
  * read-only: this driver never likes, follows, posts or messages. Write actions
    are what trigger action-blocks; we do none of them.
  * optional proxy, so the scraper egresses from the same network the account
    normally logs in from
  * challenge/rate-limit detection that raises instead of retrying
"""

from __future__ import annotations

import json
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .config import Settings, proxy_config
from .parser import normalize_dom_post, normalize_graphql_post, walk_posts

BASE = "https://www.threads.com"
POST_CARD = 'div[data-pressable-container="true"]'

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Phrases Meta shows when it is throttling or challenging a session.
_CHALLENGE_MARKERS = (
    "try again later",
    "please wait a few minutes",
    "we limit how often",
    "suspicious activity",
    "confirm it's you",
    "confirm its you",
    "your account has been temporarily",
    "action blocked",
)

# Pulled out of the DOM fallback text: engagement counters and chrome.
_COUNTER_LINE = re.compile(r"^\s*[\d.,]+[KkMm]?\s*$")
_CHROME_LINE = re.compile(
    r"^\s*(translate|see more|more|follow|following|reply|repost|share|like|"
    r"\d+[smhdw]|pinned|verified)\s*$",
    re.IGNORECASE,
)

# Keep this in sync with what a real Chrome on Windows reports.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
window.chrome = window.chrome || { runtime: {} };
try {
  const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
  window.navigator.permissions.query = (p) =>
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(p);
} catch (e) { /* permissions API unavailable; nothing to mask */ }
"""

_JS_EXTRACT_CARDS = """
() => {
  const out = [];
  document.querySelectorAll('div[data-pressable-container="true"]').forEach(c => {
    const hrefs = Array.from(c.querySelectorAll('a[href]'))
      .map(a => a.getAttribute('href')).filter(Boolean);
    const postHref = hrefs.find(h => h.includes('/post/'));
    const userHref = hrefs.find(h => /^\\/@[^\\/]+$/.test(h));
    let username = '';
    if (userHref) username = userHref.slice(2);
    else if (postHref) {
      const m = postHref.match(/\\/@([^\\/]+)\\/post\\//);
      if (m) username = m[1];
    }
    let code = '';
    if (postHref) {
      const m = postHref.match(/\\/post\\/([^\\/?#]+)/);
      if (m) code = m[1];
    }
    let taken_at = 0;
    const t = c.querySelector('time');
    if (t) {
      const dt = t.getAttribute('datetime');
      if (dt) taken_at = Math.floor(new Date(dt).getTime() / 1000);
    }
    const text = (c.innerText || '').trim();
    if (username && text) {
      out.push({
        username, code, text, taken_at,
        permalink: postHref ? new URL(postHref, location.origin).href : ''
      });
    }
  });
  return out;
}
"""

_JS_INLINE_JSON = """
() => Array.from(document.querySelectorAll('script[type="application/json"]'))
        .map(s => s.textContent || '')
        .filter(t => t.length > 200 && t.includes('username'))
"""


class ThreadsBlocked(RuntimeError):
    """Threads bounced us to login, or showed a challenge / rate-limit notice."""


class TransientNetworkError(RuntimeError):
    """A page load failed for network reasons, not because Threads blocked us.

    Kept distinct from ThreadsBlocked on purpose: a dropped connection should
    cost one query and a short retry, whereas a block must stop the whole run.
    """


def _clean_dom_text(raw: str, username: str) -> str:
    """innerText of a card includes the handle, timestamp and counters. Drop them."""
    lines = []
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lstrip("@").lower() == username.lower():
            continue
        if _COUNTER_LINE.match(stripped) or _CHROME_LINE.match(stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


class ThreadsCollector:
    """Owns the browser. Use as a context manager."""

    def __init__(self, settings: Settings, log=print):
        self.s = settings
        self.log = log
        self._pw = None
        self._ctx = None
        self._page = None
        self._captured: list[Any] = []

    # ---------- lifecycle ----------

    def __enter__(self) -> "ThreadsCollector":
        profile_dir = Path(self.s.user_data_dir)
        state_file = Path(self.s.storage_state)

        if not profile_dir.exists() or not any(profile_dir.iterdir()):
            if not state_file.exists():
                raise FileNotFoundError(
                    f"No saved Threads session (looked in {profile_dir} and {state_file}).\n"
                    "Run:  python scripts/login.py   (log in once, session is cached)"
                )

        self._pw = sync_playwright().start()

        launch_kwargs: dict = {
            "user_data_dir": str(profile_dir),
            "headless": self.s.headless,
            "user_agent": USER_AGENT,
            "viewport": {"width": 1440, "height": 900},
            "locale": self.s.locale,
            "timezone_id": self.s.timezone_id,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        }
        proxy = proxy_config(self.s)
        if proxy:
            launch_kwargs["proxy"] = proxy
            self.log(f"  [safety] egressing via proxy {proxy['server']}")

        # A persistent context keeps cookies, localStorage and the device profile
        # stable between runs - a brand-new fingerprint each session is a tell.
        self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        self._ctx.add_init_script(_STEALTH_JS)

        self._bootstrap_cookies_if_needed(profile_dir, state_file)

        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.on("response", self._on_response)
        return self

    def _bootstrap_cookies_if_needed(self, profile_dir: Path, state_file: Path) -> None:
        """Migrate a legacy storage_state.json session into the persistent profile."""
        try:
            has_cookies = bool(self._ctx.cookies(BASE))
        except PlaywrightError:
            has_cookies = False
        if has_cookies or not state_file.exists():
            return
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            cookies = state.get("cookies") or []
            if cookies:
                self._ctx.add_cookies(cookies)
                self.log(f"  imported {len(cookies)} cookies from {state_file.name}")
        except (json.JSONDecodeError, OSError, PlaywrightError) as exc:
            self.log(f"  ! could not import legacy session: {exc}")

    def __exit__(self, *exc) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ---------- interception ----------

    def _on_response(self, response) -> None:
        url = response.url
        if "/graphql" not in url and "/api/v1/" not in url:
            return
        try:
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                return
            self._captured.append(response.json())
        except Exception:
            # Bodies get dropped on redirects/aborts all the time. Not fatal.
            pass

    def _drain_captured(self) -> list[Any]:
        payloads, self._captured = self._captured, []
        return payloads

    def _harvest_inline_json(self) -> list[Any]:
        blobs = []
        try:
            for raw in self._page.evaluate(_JS_INLINE_JSON) or []:
                try:
                    blobs.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    continue
        except PlaywrightError:
            pass
        return blobs

    # ---------- pacing ----------

    def _pause(self, lo: int | None = None, hi: int | None = None) -> None:
        lo = lo if lo is not None else self.s.min_delay_ms
        hi = hi if hi is not None else self.s.max_delay_ms
        time.sleep(random.uniform(lo, hi) / 1000.0)

    def _human_scroll(self) -> None:
        """Wheel events in a few uneven nudges, not one teleporting jump."""
        try:
            for _ in range(random.randint(2, 4)):
                self._page.mouse.wheel(0, random.randint(280, 620))
                time.sleep(random.uniform(0.15, 0.45))
            # People overshoot and scroll back sometimes.
            if random.random() < 0.15:
                self._page.mouse.wheel(0, -random.randint(120, 300))
                time.sleep(random.uniform(0.2, 0.6))
        except PlaywrightError:
            try:
                self._page.evaluate("window.scrollBy(0, window.innerHeight * 0.9)")
            except PlaywrightError:
                pass

    # ---------- guards ----------

    def _assert_healthy(self) -> None:
        url = (self._page.url or "").lower()
        if "/login" in url or "accounts/login" in url or "challenge" in url:
            raise ThreadsBlocked(
                "Threads redirected to login/challenge - the saved session expired "
                "or the account was flagged. Re-run: python scripts/login.py"
            )
        try:
            body = (self._page.inner_text("body") or "")[:2500].lower()
        except PlaywrightError:
            return
        for marker in _CHALLENGE_MARKERS:
            if marker in body:
                raise ThreadsBlocked(f"Threads is throttling this session ({marker!r})")

    # ---------- human-looking browsing ----------

    def warmup(self) -> None:
        """Read the home feed for a moment before touching search.

        A session that opens straight into a burst of keyword searches looks
        nothing like a person opening an app.
        """
        if random.random() > self.s.warmup_probability:
            return
        try:
            self.log("  [safety] warming up on the home feed")
            self._page.goto(BASE, wait_until="domcontentloaded", timeout=30_000)
            self._pause(2000, 4500)
            for _ in range(max(1, self.s.warmup_feed_scrolls)):
                self._human_scroll()
                self._pause(1500, 3800)
            self._drain_captured()
        except (PlaywrightError, PlaywrightTimeout):
            pass

    def browse_feed_briefly(self) -> None:
        """Occasional drift back to the feed between searches."""
        if random.random() > self.s.interleave_feed_probability:
            return
        try:
            self._page.goto(BASE, wait_until="domcontentloaded", timeout=30_000)
            self._pause(1500, 3200)
            self._human_scroll()
            self._pause(1200, 2600)
            self._drain_captured()
        except (PlaywrightError, PlaywrightTimeout):
            pass

    # ---------- collection ----------

    def _collect_from_page(self, matched_query: str) -> list[dict]:
        """Merge GraphQL + inline JSON + DOM into one de-duplicated batch."""
        posts: dict[str, dict] = {}

        for payload in self._drain_captured() + self._harvest_inline_json():
            for node in walk_posts(payload):
                rec = normalize_graphql_post(node, matched_query)
                if rec and rec["text"].strip():
                    posts.setdefault(rec["post_id"], rec)

        # DOM fallback fills gaps the JSON paths missed.
        try:
            for card in self._page.evaluate(_JS_EXTRACT_CARDS) or []:
                card["text"] = _clean_dom_text(card.get("text", ""), card.get("username", ""))
                rec = normalize_dom_post(card, matched_query)
                if not rec or not rec["text"]:
                    continue
                key = rec["code"] or rec["post_id"]
                if not any(key and key == p.get("code") for p in posts.values()):
                    posts.setdefault(rec["post_id"], rec)
        except PlaywrightError:
            pass

        return list(posts.values())

    def _open_and_scroll(self, url: str, matched_query: str, scrolls: int) -> list[dict]:
        self._drain_captured()
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeout:
            self.log(f"    ! timeout loading {url}")
            return []
        except PlaywrightError as exc:
            # ERR_INTERNET_DISCONNECTED, DNS failures, proxy hiccups. The caller
            # retries this one query rather than losing the whole run.
            raise TransientNetworkError(str(exc).split("\n")[0]) from exc

        self._assert_healthy()

        try:
            self._page.wait_for_selector(POST_CARD, timeout=15_000)
        except PlaywrightTimeout:
            self.log("    ! no post cards rendered (empty result or layout change)")

        self._pause()
        collected: dict[str, dict] = {}
        for rec in self._collect_from_page(matched_query):
            collected[rec["post_id"]] = rec

        for i in range(scrolls):
            self._human_scroll()
            self._pause(1200, 2600)
            before = len(collected)
            for rec in self._collect_from_page(matched_query):
                collected.setdefault(rec["post_id"], rec)
            if len(collected) == before and i >= 2:
                break  # feed exhausted; stop burning requests

        return list(collected.values())

    def search(self, query: str, scrolls: int | None = None, recent: bool = True) -> list[dict]:
        scrolls = self.s.scrolls_per_query if scrolls is None else scrolls
        url = f"{BASE}/search?q={quote_plus(query)}&serp_type=default"
        if recent:
            url += "&filter=recent"
        self.log(f"  search: {query!r}")
        return self._open_and_scroll(url, query, scrolls)

    def hashtag(self, tag: str, scrolls: int | None = None) -> list[dict]:
        scrolls = self.s.scrolls_per_query if scrolls is None else scrolls
        tag = tag.lstrip("#")
        url = f"{BASE}/search?q={quote_plus('#' + tag)}&serp_type=tags"
        self.log(f"  hashtag: #{tag}")
        return self._open_and_scroll(url, f"#{tag}", scrolls)

    # ---------- profile enrichment ----------

    def profile(self, username: str) -> dict:
        """Fetch bio + follower count for a single lead. Best-effort, never raises."""
        out = {"followers": 0, "bio": "", "external_url": ""}
        try:
            self._drain_captured()
            self._page.goto(
                f"{BASE}/@{username}", wait_until="domcontentloaded", timeout=30_000
            )
            self._pause(1200, 2400)

            for payload in self._drain_captured() + self._harvest_inline_json():
                found = _find_user_object(payload, username)
                if found:
                    out["followers"] = found.get("followers", 0) or out["followers"]
                    out["bio"] = found.get("bio", "") or out["bio"]
                    out["external_url"] = found.get("external_url", "") or out["external_url"]
                    if out["followers"] and out["bio"]:
                        break

            if not out["followers"]:
                text = self._page.inner_text("body")[:4000]
                m = re.search(r"([\d.,]+\s*[KkMm]?)\s+followers", text)
                if m:
                    out["followers"] = _parse_count(m.group(1))
        except (PlaywrightError, PlaywrightTimeout):
            pass
        return out


def _parse_count(raw: str) -> int:
    raw = (raw or "").strip().replace(",", "").replace(" ", "")
    mult = 1
    if raw[-1:].lower() == "k":
        mult, raw = 1_000, raw[:-1]
    elif raw[-1:].lower() == "m":
        mult, raw = 1_000_000, raw[:-1]
    try:
        return int(float(raw) * mult)
    except ValueError:
        return 0


def _find_user_object(node: Any, username: str, _depth: int = 0) -> dict | None:
    """Locate the profile owner's user dict inside an arbitrary payload."""
    if _depth > 20:
        return None
    if isinstance(node, dict):
        if str(node.get("username", "")).lower() == username.lower():
            followers = node.get("follower_count")
            if followers is None:
                fc = node.get("friendship_status") or {}
                followers = fc.get("follower_count") if isinstance(fc, dict) else None
            if followers is not None or node.get("biography") is not None:
                return {
                    "followers": int(followers or 0),
                    "bio": str(node.get("biography") or ""),
                    "external_url": str(
                        node.get("external_url") or node.get("bio_links_url") or ""
                    ),
                }
        for v in node.values():
            found = _find_user_object(v, username, _depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_user_object(v, username, _depth + 1)
            if found:
                return found
    return None


@contextmanager
def open_collector(settings: Settings, log=print):
    with ThreadsCollector(settings, log=log) as c:
        yield c
