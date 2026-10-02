"""Drive a real Chromium against maps.google.com and pull result cards.

Design notes, because the fragile parts are not obvious:

* We read listings straight out of the results feed rather than clicking into
  each one. The feed card already tells us whether a business has a website
  (there is an outbound link in the card only when it does), which is the one
  field this whole tool hangs on. Skipping the detail panel makes the scrape
  roughly 20x faster.
* Selectors are chosen to survive Google reshuffling their markup: we anchor on
  `a[href*="/maps/place/"]` and on the shape of the card text, not on obfuscated
  class names, which change every few weeks.
* We only open a detail panel when a no-website lead is missing a phone, since
  phone is the contact channel that actually matters here.
"""

from __future__ import annotations

import asyncio
import random
import re
import urllib.parse
from dataclasses import dataclass

from playwright.async_api import Browser, Page, async_playwright

from .models import Lead, parse_card_text, parse_rating_label

# Stable-ish hex id inside a place URL, e.g. !1s0x3eb33e06...:0x9a1b...
_FEATURE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.I)
_PLACE_SLUG_RE = re.compile(r"/maps/place/([^/@?]+)")

_BLOCK_MARKERS = (
    "unusual traffic",
    "not a robot",
    "detected unusual",
    "systems have detected",
)

# Extracts every result card in the feed in one page evaluation.
_EXTRACT_JS = """
() => {
  const feed = document.querySelector('div[role="feed"]');
  const root = feed || document.body;
  const links = Array.from(root.querySelectorAll('a[href*="/maps/place/"]'));
  const out = [];
  const seen = new Set();

  const isExternal = (href) => {
    try {
      const u = new URL(href);
      if (!u.protocol.startsWith('http')) return false;
      const h = u.hostname.toLowerCase();
      if (h.endsWith('google.com') || h.includes('.google.')) return false;
      if (h.endsWith('gstatic.com') || h.endsWith('ggpht.com')) return false;
      return true;
    } catch (e) { return false; }
  };

  for (const link of links) {
    const href = link.href || '';
    if (!href || seen.has(href)) continue;
    seen.add(href);

    // Walk up to the card: the smallest ancestor carrying multi-line text.
    let card = link.closest('div.Nv2PK');
    if (!card) {
      card = link.parentElement;
      let hops = 0;
      while (card && hops < 5) {
        const t = (card.innerText || '').trim();
        if (t.split('\\n').filter(Boolean).length >= 2) break;
        card = card.parentElement;
        hops++;
      }
    }
    if (!card) continue;

    // A website is present if Google renders its Website action, or if the
    // card contains any outbound non-Google link at all.
    let siteEl = card.querySelector('a[data-value="Website"], a[aria-label^="Visit"]');
    if (!siteEl) {
      siteEl = Array.from(card.querySelectorAll('a[href]')).find(a => isExternal(a.href));
    }

    // The star element's aria-label carries the review count, which the card
    // text often omits entirely.
    const starEl = card.querySelector('span[role="img"][aria-label*="star" i]');

    out.push({
      name: link.getAttribute('aria-label') || '',
      maps_url: href,
      website: siteEl ? siteEl.href : '',
      has_website: !!siteEl,
      rating_label: starEl ? (starEl.getAttribute('aria-label') || '') : '',
      text: (card.innerText || '').trim(),
    });
  }
  return out;
}
"""

# Reads the open detail panel. data-item-id attributes are far more stable than
# class names and carry the phone number directly.
_DETAIL_JS = """
() => {
  const q = (s) => document.querySelector(s);
  const phoneBtn = q('button[data-item-id^="phone:tel:"]');
  const siteEl = q('a[data-item-id="authority"]');
  const addrBtn = q('button[data-item-id="address"]');
  const h1 = q('h1');
  const strip = (el, prefix) => {
    if (!el) return '';
    const v = el.getAttribute('aria-label') || el.innerText || '';
    return v.replace(prefix, '').trim();
  };
  return {
    name: h1 ? h1.innerText.trim() : '',
    phone: phoneBtn
      ? (phoneBtn.getAttribute('data-item-id') || '').replace('phone:tel:', '')
      : '',
    website: siteEl ? siteEl.href : '',
    address: strip(addrBtn, /^Address:\\s*/),
  };
}
"""


class BlockedError(RuntimeError):
    """Google served a CAPTCHA or rate-limit interstitial."""


@dataclass
class ScrapeStats:
    searches: int = 0
    cards_seen: int = 0
    no_website: int = 0
    details_opened: int = 0
    blocks: int = 0


class MapsScraper:
    """One browser, many searches. Reuse the instance across a whole run."""

    def __init__(
        self,
        headless: bool = True,
        min_delay: float = 1.8,
        max_delay: float = 4.0,
        scroll_pause_ms: int = 1200,
        max_scrolls: int = 40,
        locale: str = "en-US",
        on_block=None,
    ):
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.scroll_pause_ms = scroll_pause_ms
        self.max_scrolls = max_scrolls
        self.locale = locale
        self.on_block = on_block
        self.stats = ScrapeStats()
        self._pw = None
        self._browser: Browser | None = None
        self._page: Page | None = None

    # --- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "MapsScraper":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        context = await self._browser.new_context(
            locale=self.locale,
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        # Hide the most obvious automation tell.
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = await context.new_page()
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # --- helpers -----------------------------------------------------------

    async def _sleep_jitter(self) -> None:
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))

    async def _dismiss_consent(self) -> None:
        """Clear the EU cookie wall, declining non-essential cookies."""
        page = self._page
        assert page
        candidates = [
            'button[aria-label*="Reject all" i]',
            'button:has-text("Reject all")',
            'button:has-text("Reject All")',
            'form[action*="consent"] button:has-text("Reject")',
        ]
        for sel in candidates:
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.click(timeout=4000)
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    async def _check_blocked(self) -> bool:
        page = self._page
        assert page
        if "/sorry/" in page.url:
            return True
        try:
            body = (await page.inner_text("body", timeout=5000)).lower()[:3000]
        except Exception:
            return False
        return any(marker in body for marker in _BLOCK_MARKERS)

    async def _handle_block(self) -> None:
        """Pause on a CAPTCHA. We never solve it; a human has to."""
        self.stats.blocks += 1
        if self.on_block:
            # Caller decides: prompt the user, back off, or abort.
            await self.on_block(self)
        else:
            raise BlockedError(
                "Google served a CAPTCHA. Re-run with --no-headless and solve it "
                "in the browser window, or wait a few hours before retrying."
            )

    async def _scroll_feed(self) -> None:
        page = self._page
        assert page
        last_count = 0
        stagnant = 0
        for _ in range(self.max_scrolls):
            await page.evaluate(
                "() => { const f = document.querySelector('div[role=\"feed\"]');"
                " if (f) f.scrollTop = f.scrollHeight; }"
            )
            await page.wait_for_timeout(self.scroll_pause_ms)

            reached_end = await page.evaluate(
                "() => /reached the end of the list/i.test(document.body.innerText)"
            )
            if reached_end:
                return

            count = await page.evaluate(
                "() => document.querySelectorAll('a[href*=\"/maps/place/\"]').length"
            )
            if count == last_count:
                stagnant += 1
                if stagnant >= 3:
                    return
            else:
                stagnant = 0
                last_count = count

    # --- extraction --------------------------------------------------------

    @staticmethod
    def place_id_from_url(url: str) -> str:
        """A stable dedupe key. Prefer the hex feature id, fall back to the slug."""
        m = _FEATURE_ID_RE.search(url)
        if m:
            return m.group(1).lower()
        m = _PLACE_SLUG_RE.search(url)
        if m:
            return "slug:" + urllib.parse.unquote(m.group(1)).lower()
        return "url:" + url[:180]

    async def _fetch_detail(self, lead: Lead) -> bool:
        """Open a listing to recover a missing phone number.

        Returns whether the panel was actually read. A caller that treats this
        page as the authority on whether the business has a website needs to
        know the difference between "no website" and "never got there".
        """
        page = self._page
        assert page
        try:
            await page.goto(lead.maps_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_selector("h1", timeout=15000)
            await page.wait_for_timeout(900)
            data = await page.evaluate(_DETAIL_JS)
        except Exception:
            return False

        self.stats.details_opened += 1
        if data.get("phone") and not lead.phone:
            lead.phone = data["phone"].strip()
        if data.get("address") and not lead.address:
            lead.address = data["address"].strip()
        # The detail panel is authoritative: if a website turns up here, the
        # lead does not belong in our list after all.
        if data.get("website"):
            lead.website = data["website"]
        lead.verified = True
        return True

    async def search_tile(
        self,
        query: str,
        lat: float,
        lng: float,
        zoom: int,
        city: str,
        only_without_website: bool = True,
    ) -> list[Lead]:
        """Run one search centred on one grid point and return matching leads."""
        page = self._page
        assert page

        url = (
            f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"
            f"/@{lat},{lng},{zoom}z?hl=en"
        )
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            return []

        self.stats.searches += 1
        await page.wait_for_timeout(2000)
        await self._dismiss_consent()

        if await self._check_blocked():
            await self._handle_block()
            return []

        try:
            await page.wait_for_selector('div[role="feed"]', timeout=12000)
        except Exception:
            # A very specific query can land straight on a single place page.
            if "/maps/place/" not in page.url:
                return []

        await self._scroll_feed()

        try:
            raw = await page.evaluate(_EXTRACT_JS)
        except Exception:
            return []

        # Google serves two card layouts. The rich one carries a Website
        # button; the compact one carries no action row at all, so every card
        # in it looks website-free. If not one card in this feed showed a
        # website, we cannot read anything into their absence, and the leads
        # are marked unverified for the detail-page pass to settle.
        feed_shows_websites = any(item.get("has_website") for item in raw)

        leads: list[Lead] = []
        for item in raw:
            name = (item.get("name") or "").strip()
            # Placeholder listings really are called "." or "-"; they are not
            # businesses anyone can sell to.
            if not name or not any(c.isalnum() for c in name):
                continue
            self.stats.cards_seen += 1

            if only_without_website and item.get("has_website"):
                continue

            parsed = parse_card_text(item.get("text", ""), name_hint=name)

            # The aria-label is the more reliable source for both numbers.
            rating, reviews = parse_rating_label(item.get("rating_label", ""))
            if rating is None:
                rating = parsed["rating"]
            if reviews is None:
                reviews = parsed["reviews"]

            leads.append(
                Lead(
                    place_id=self.place_id_from_url(item["maps_url"]),
                    name=name,
                    category=parsed["category"],
                    address=parsed["address"],
                    phone=parsed["phone"],
                    website=item.get("website", ""),
                    rating=rating,
                    reviews=reviews,
                    maps_url=item["maps_url"],
                    city=city,
                    query=query,
                    lat=lat,
                    lng=lng,
                    verified=feed_shows_websites,
                )
            )

        self.stats.no_website += len(leads)
        await self._sleep_jitter()
        return leads
