"""Best-effort email discovery for businesses that have no website.

Set expectations before reading further: these leads were selected precisely
because they have no web presence, so for many of them no email exists anywhere
to be found. Phone is the reliable contact channel; email is a bonus.

Where an email does exist it is usually on a Facebook page or a local business
directory. Finding it means running a web search, and every free search API and
HTML endpoint is now behind bot protection:

  * html.duckduckgo.com and lite.duckduckgo.com return HTTP 202 challenge pages.
  * Bing over plain `requests` returns junk results (wrong country, ignored
    quotes) because it fingerprints the client as a bot.

So enrichment drives a real browser, same as the scraper. It searches Bing
rather than Google, deliberately: the Google CAPTCHA budget is spent on the
scrape itself, and burning it here would cost us leads.
"""

from __future__ import annotations

import asyncio
import base64
import random
import re
import urllib.parse
from dataclasses import dataclass

from playwright.async_api import async_playwright

from .models import Lead

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Local-parts that are never a business's own contact address.
_JUNK_LOCAL = re.compile(
    r"^(noreply|no-reply|donotreply|postmaster|abuse|webmaster|sentry|"
    r"privacy|dmca|legal|press|jobs|careers|example|test|user|name|email|"
    r"error-lite|support@duckduckgo)",
    re.I,
)
_JUNK_DOMAIN = re.compile(
    r"(wixpress|godaddy|sentry\.io|example\.(com|org)|domain\.com|"
    r"email\.com|yourdomain|sitename|wordpress\.org|squarespace|"
    r"schema\.org|w3\.org|gstatic|duckduckgo\.com|bing\.com|microsoft\.com|"
    r"sentry-next|\.(png|jpg|jpeg|gif|webp|svg|css|js)$)",
    re.I,
)

# Hosts worth opening: social pages and directories carry contact details.
_WORTH_OPENING = re.compile(
    r"(facebook\.com|instagram\.com|yelp\.|justdial\.|yellowpages|"
    r"tripadvisor\.|zomato\.|foursquare\.|bbb\.org|chamber|directory|"
    r"clinic|hospital|listing|local|business)",
    re.I,
)
_SKIP_HOSTS = re.compile(
    r"(google\.|youtube\.|wikipedia\.|linkedin\.com|twitter\.com|x\.com|"
    r"pinterest\.|amazon\.|bing\.com|microsoft\.com|duckduckgo\.)",
    re.I,
)


# Aggregators and booking portals. An email on one of these domains belongs to
# the directory, not to the business listed on it. Mailing care@oladoc.com
# instead of the dentist is worse than having no email at all, so these are
# rejected outright.
_AGGREGATOR_DOMAINS = re.compile(
    r"(^|\.)(oladoc|marham|healthwire|practo|justdial|sulekha|yelp|zomato|"
    r"tripadvisor|foursquare|yellowpages|yell|thomsonlocal|hotfrog|cylex|"
    r"bark|checkatrade|trustpilot|glassdoor|indeed|facebook|instagram|"
    r"linkedin|booksy|fresha|treatwell|opentable|doctolib|zocdoc|"
    r"healthgrades|vitals|webmd|nextdoor|angi|thumbtack|houzz)\.",
    re.I,
)

_WORD_RE = re.compile(r"[a-z]{4,}")


@dataclass
class EnrichStats:
    attempted: int = 0
    found: int = 0
    errors: int = 0
    rejected_aggregator: int = 0

    @property
    def rate(self) -> float:
        return (self.found / self.attempted * 100) if self.attempted else 0.0


def _registrable(host: str) -> str:
    """Rough registrable domain: 'www.foo.co.uk' -> 'foo'."""
    parts = [p for p in host.lower().split(".") if p and p != "www"]
    if not parts:
        return ""
    # Skip common second-level suffixes so 'foo.co.uk' yields 'foo'.
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "gov", "ac"}:
        return parts[-3]
    return parts[0] if len(parts) == 1 else parts[-2]


def belongs_to_business(email: str, business_name: str) -> bool:
    """Does this email's domain plausibly belong to this business?

    Used to tell a clinic's own address apart from the directory page it was
    found on. Free mail domains pass, since a small business with no website
    very often uses gmail.
    """
    domain = email.partition("@")[2].lower()
    if not domain:
        return False
    if _AGGREGATOR_DOMAINS.search(domain + "."):
        return False

    free_mail = {
        "gmail", "yahoo", "hotmail", "outlook", "live", "icloud", "aol",
        "protonmail", "proton", "gmx", "mail", "yandex", "zoho", "rediffmail",
        "msn", "me", "qq", "163",
    }
    root = _registrable(domain)
    if root in free_mail:
        return True

    # A custom domain should echo some part of the business name.
    tokens = [t for t in _WORD_RE.findall(business_name.lower())]
    generic = {"dental", "clinic", "care", "practice", "center", "centre",
               "medical", "hospital", "the", "and", "shop", "store", "salon"}
    distinctive = [t for t in tokens if t not in generic] or tokens
    return any(t[:5] in root or root[:5] in t for t in distinctive if len(t) >= 4)


def clean_emails(text: str, business_name: str = "") -> list[str]:
    """Pull plausible business emails out of arbitrary text.

    When business_name is given, emails that clearly belong to a directory
    rather than the business are dropped.
    """
    out: list[str] = []
    for raw in _EMAIL_RE.findall(text or ""):
        email = raw.strip(".,;:)(<>\"'").lower()
        if len(email) > 96:
            continue
        local, _, domain = email.partition("@")
        if not domain or "." not in domain:
            continue
        if _JUNK_LOCAL.match(local) or _JUNK_DOMAIN.search(email):
            continue
        # Long consonant-only local parts are tracking addresses, not people.
        if len(local) > 30 and not re.search(r"[aeiou]", local):
            continue
        if business_name and not belongs_to_business(email, business_name):
            continue
        if email not in out:
            out.append(email)
    return out


def unwrap_bing_link(url: str) -> str:
    """Bing wraps outbound links in /ck/a redirectors with the target base64'd."""
    if "bing.com/ck/a" not in url:
        return url
    m = re.search(r"[?&]u=a1([^&]+)", url)
    if not m:
        return url
    payload = m.group(1)
    payload += "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload).decode("utf-8", "ignore")
    except Exception:
        return url


# Pulls result links and snippet text off a Bing SERP.
_SERP_JS = """
() => {
  const items = Array.from(document.querySelectorAll('li.b_algo')).slice(0, 8);
  return {
    text: document.body.innerText || '',
    results: items.map(li => {
      const a = li.querySelector('h2 a');
      return { url: a ? a.href : '', snippet: (li.innerText || '').slice(0, 400) };
    }).filter(r => r.url),
  };
}
"""

# Reads a landed page: mailto links are the highest-confidence signal.
_PAGE_JS = """
() => ({
  text: (document.body ? document.body.innerText : '').slice(0, 200000),
  mailtos: Array.from(document.querySelectorAll('a[href^="mailto:"]'))
    .map(a => a.getAttribute('href').replace('mailto:', '').split('?')[0]),
})
"""


class BrowserEnricher:
    """Searches for business emails using a real browser."""

    def __init__(self, headless: bool = True, pages_per_lead: int = 2):
        self.headless = headless
        self.pages_per_lead = pages_per_lead
        self._pw = None
        self._browser = None
        self._page = None

    async def __aenter__(self) -> "BrowserEnricher":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = await self._browser.new_context(
            locale="en-US", user_agent=_UA, viewport={"width": 1400, "height": 900}
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self._page = await ctx.new_page()
        # Images and fonts are pure cost here.
        await self._page.route(
            re.compile(r"\.(png|jpg|jpeg|gif|webp|svg|woff2?|ttf|mp4)$"),
            lambda route: asyncio.ensure_future(route.abort()),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _goto(self, url: str, timeout: int = 30000) -> bool:
        """Navigate, tolerating the aborts Bing throws when pushed too fast."""
        for attempt in range(2):
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                return True
            except Exception:
                if attempt == 0:
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    continue
                return False
        return False

    async def _search(self, query: str) -> tuple[str, list[dict]]:
        url = "https://www.bing.com/search?q=" + urllib.parse.quote_plus(query)
        if not await self._goto(url):
            return "", []
        await self._page.wait_for_timeout(random.randint(1200, 2200))
        try:
            data = await self._page.evaluate(_SERP_JS)
        except Exception:
            return "", []
        results = [
            {"url": unwrap_bing_link(r["url"]), "snippet": r["snippet"]}
            for r in data.get("results", [])
        ]
        return data.get("text", ""), results

    async def find_email(self, lead: Lead) -> tuple[str, str]:
        """Return (email, where_we_found_it), or ("", "")."""
        locality = lead.city or lead.address
        queries = [
            f'"{lead.name}" {locality} email contact',
            f'"{lead.name}" {locality} facebook',
        ]

        for query in queries:
            serp_text, results = await self._search(query)

            # Cheapest win: the email is printed in a search snippet.
            snippet_blob = " ".join(r["snippet"] for r in results)
            hits = clean_emails(snippet_blob, lead.name) or \
                clean_emails(serp_text, lead.name)
            if hits:
                return hits[0], "search snippet"

            # Otherwise open the most promising results.
            candidates = [
                r["url"] for r in results
                if r["url"].startswith("http") and not _SKIP_HOSTS.search(r["url"])
            ]
            candidates.sort(key=lambda u: 0 if _WORTH_OPENING.search(u) else 1)

            for url in candidates[: self.pages_per_lead]:
                if not await self._goto(url, timeout=20000):
                    continue
                await self._page.wait_for_timeout(random.randint(800, 1500))
                try:
                    page_data = await self._page.evaluate(_PAGE_JS)
                except Exception:
                    continue
                hits = clean_emails(
                    " ".join(page_data.get("mailtos", [])), lead.name
                ) or clean_emails(page_data.get("text", ""), lead.name)
                if hits:
                    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
                    return hits[0], host

            await asyncio.sleep(random.uniform(1.5, 3.0))

        return "", ""


async def enrich_leads(
    leads: list[Lead],
    headless: bool = True,
    on_result=None,
) -> EnrichStats:
    """Look up emails for a batch of leads.

    Sequential on purpose. Bing aborts connections under parallel load, and a
    blocked enricher finds nothing at all.
    """
    stats = EnrichStats()
    if not leads:
        return stats

    async with BrowserEnricher(headless=headless) as enricher:
        for lead in leads:
            stats.attempted += 1
            try:
                email, source = await enricher.find_email(lead)
            except Exception:
                stats.errors += 1
                email, source = "", ""
            if email:
                lead.email = email
                lead.email_source = source
                stats.found += 1
            if on_result:
                on_result(lead, email, source)
            await asyncio.sleep(random.uniform(1.0, 2.5))

    return stats
