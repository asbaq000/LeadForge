"""Separate stage: client and job-activity details, from the public job page.

This never touches the job search. Jobs are scraped first and completely; only
afterwards does this walk the results. A failure skips that job and the run
continues.

The blocker here is Cloudflare, not login. Job pages are public — a browser
sees the client's country, city, local time, member-since date and the proposal
counts while logged out — but every HTTP client is refused with a 403, at every
TLS impersonation target tried (chrome99..146, safari, firefox, edge, warmed
sessions included). So `html_fetcher` is pluggable: supply something driving a
real browser and this stage works. See BROWSER_REQUIRED_HELP.
"""

import json
import logging
import re

from curl_cffi import requests

from upwork_scraper.enrich.throttle import CircuitBreaker, HumanDelay
from upwork_scraper.models.client_models import ClientInfo
from upwork_scraper.models.job_models import Job
from upwork_scraper.proxies.proxy_manager import ProxyManager, proxy_dict_for

LOGGER = logging.getLogger(__name__)

BROWSER_REQUIRED_HELP = """
Job pages are public, but Cloudflare refuses plain HTTP clients with a 403 —
curl_cffi cannot get through at any impersonation setting. Fetching them needs
a real browser engine.

Supply one via `html_fetcher`: any callable taking a URL and returning the
page's HTML, e.g. a Playwright or Selenium session.

    enricher = ClientEnricher(html_fetcher=my_browser_fetch)

Without it every request returns 403 and every job is skipped.
""".strip()

PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.upwork.com/nx/search/jobs/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\xa0]+")


def _text(html: str) -> str:
    """HTML to something close to the browser's innerText."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = _TAGS.sub("\n", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    return _WS.sub(" ", text)


def _block(html: str, data_qa: str, length: int = 600) -> str:
    """Text of the element carrying a given data-qa hook.

    Upwork's markup churns, but these hooks have been stable, so anchoring on
    them beats depending on class names or nesting.
    """
    match = re.search(rf'data-qa="{re.escape(data_qa)}"', html)
    if not match:
        return ""
    # Skip to the end of the opening tag, or the leftover ">" becomes a "line".
    tag_end = html.find(">", match.end())
    start = match.end() if tag_end < 0 else tag_end + 1
    return _text(html[start: start + length])


# Interface furniture that sits between a label and its value in the markup.
UI_NOISE = (
    "close the tooltip", "tooltip", "learn more", "opens in a new window",
    "about the client", "activity on this job",
)


def _lines(text: str) -> list[str]:
    """Visible lines only — no markup fragments, no UI furniture.

    A fixed-length window can cut mid-tag, and the real pages carry a
    "Close the tooltip" button between some labels and their values.
    """
    clean = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "<" in line or ">" in line or "class=" in line:
            continue
        if line.lower() in UI_NOISE:
            continue
        clean.append(line)
    return clean


def _region(text: str, start_marker: str, end_marker: str, fallback: int = 900) -> str:
    """The slice of page text between two headings.

    Returns the whole text when the start heading is absent, so a page with
    different wording degrades to the old whole-page behaviour instead of
    silently parsing nothing.
    """
    start = text.find(start_marker)
    if start < 0:
        return text
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        end = start + fallback
    return text[start:end]


def _value_after(
    text: str, label: str, pattern: str, window: int = 600
) -> str | None:
    """First value matching `pattern` after any occurrence of `label`.

    Two reasons this is fussier than reading the next line:

    - The label appears in unrelated marketing copy earlier in the page
      ("Proposals that win funding"), so only the occurrence that is actually
      followed by a value counts — hence trying every occurrence.
    - A tooltip button and a paragraph of explanation often sit between the
      label and its value, so the value is matched by its own shape rather
      than by position.
    """
    for anchor in re.finditer(re.escape(label), text, re.I):
        window_text = text[anchor.end(): anchor.end() + window]
        match = re.search(pattern, window_text, re.I)
        if match:
            return match.group(1).strip()
    return None


def _int_or_none(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.search(r"\d+", value)
    return int(digits.group()) if digits else None


_DECODER = json.JSONDecoder()

# Client stats live in the page's Nuxt payload, which carries more than the
# card renders — the star rating and review count are there but never shown
# logged out. Field name -> ClientInfo attribute.
_PAYLOAD_STATS = {
    "totalAssignments": "total_hires",
    "activeAssignmentsCount": "active_hires",
    "hoursCount": "total_hours",
    "feedbackCount": "total_reviews",
    "score": "rating",
    "totalJobsWithHires": "total_jobs_with_hires",
    "totalCharges": "total_spent",
}
_PAYLOAD_COUNTS = {
    "postedCount": "total_posted_jobs",
    "openCount": "open_jobs",
}


def _extract_payload_array(html: str) -> list | None:
    """The flat array Nuxt serialises the page state into."""
    for script in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S):
        body = script.group(1)
        if "totalAssignments" not in body:
            continue
        for opening in re.finditer(r"\[", body):
            try:
                data, _ = _DECODER.raw_decode(body[opening.start():])
            except ValueError:
                continue
            if isinstance(data, list) and len(data) > 100:
                return data
    return None


def _resolve_one_level(obj: dict, flat: list) -> dict:
    """Swap each index for `flat[index]` — exactly one hop.

    The resolved values are themselves small integers, so recursing would read
    a real value (say 9 active contracts) as an index and return nonsense.
    """
    out = {}
    for key, value in obj.items():
        if not isinstance(value, int) or not 0 <= value < len(flat):
            out[key] = value
            continue

        resolved = flat[value]
        # Money arrives as {"amount": <index>} and needs one more hop.
        if isinstance(resolved, dict) and set(resolved) == {"amount"}:
            inner = resolved["amount"]
            resolved = (
                flat[inner] if isinstance(inner, int) and 0 <= inner < len(flat) else inner
            )
        out[key] = resolved
    return out


def parse_payload_stats(html: str) -> dict:
    """Client numbers from the embedded payload, keyed by ClientInfo field.

    Preferred over the rendered text: exact figures rather than the card's
    rounded "$326K", plus rating and review count which the card omits.
    Returns {} when the payload is missing or shaped differently.
    """
    flat = _extract_payload_array(html)
    if not flat:
        return {}

    found: dict = {}
    for item in flat:
        if not isinstance(item, dict):
            continue
        if "totalAssignments" in item and not found.get("_stats"):
            found["_stats"] = _resolve_one_level(item, flat)
        if "postedCount" in item and not found.get("_counts"):
            found["_counts"] = _resolve_one_level(item, flat)

    values: dict = {}
    for source, mapping in (
        (found.get("_stats", {}), _PAYLOAD_STATS),
        (found.get("_counts", {}), _PAYLOAD_COUNTS),
    ):
        for raw_key, field in mapping.items():
            value = source.get(raw_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                values[field] = value

    return values


def parse_client_info(html: str, cipher: str) -> ClientInfo:
    """Pull client and activity details out of a public job page.

    Written against the real logged-out markup (2026-08-12):

        <div data-qa="client-contract-date"><small>Member since Aug 1, 2025</small></div>
        <li data-qa="client-location">
          <strong>Australia</strong><div><span>Sydney</span><span>4:00 PM</span></div>
        </li>

    Anything Upwork renames comes back None rather than breaking the record.
    """
    info = ClientInfo(cipher=cipher)
    text = _text(html)

    # Scope each group of fields to its own region. The whole-page approach
    # picked up marketing copy — "Proposals that win funding" in the site nav
    # was being read as the proposal count.
    client_region = _region(text, "About the client", "Explore similar jobs")
    activity_region = _region(text, "Activity on this job", "About the client")

    # Member since — from the hook, falling back to the page text.
    date_block = _block(html, "client-contract-date", 200)
    match = re.search(r"Member since\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})",
                      date_block or text)
    if match:
        info.member_since = match.group(1)

    # Location: <strong>Country</strong> then optional city, then local time.
    location_lines = _lines(_block(html, "client-location", 400))
    if location_lines:
        info.country = location_lines[0]
        for line in location_lines[1:]:
            if re.fullmatch(r"\d{1,2}:\d{2}\s*(AM|PM)?", line, re.I):
                info.local_time = line
            elif info.city is None and len(line) < 60:
                info.city = line

    # Competition on this job. Upwork buckets the proposal count rather than
    # giving an exact number, e.g. "Less than 5", "5 to 10", "50+".
    info.proposals = _value_after(
        activity_region, "Proposals", r"(Less than \d+|\d+\s*to\s*\d+|\d+\s*\+)"
    )
    info.last_viewed = _value_after(
        activity_region,
        "Last viewed by client",
        r"(\d+\s+(?:second|minute|hour|day|week|month)s?\s+ago|just now|yesterday)",
    )
    info.interviewing = _int_or_none(
        _value_after(activity_region, "Interviewing", r"(\d+)")
    )
    info.invites_sent = _int_or_none(
        _value_after(activity_region, "Invites sent", r"(\d+)")
    )
    info.unanswered_invites = _int_or_none(
        _value_after(activity_region, "Unanswered invites", r"(\d+)")
    )

    # Where the job wants the freelancer.
    match = re.search(r"\n(Worldwide|Only freelancers located in [^\n]{1,60})\n", text)
    if match:
        info.job_location = match.group(1).strip()

    # Client history. Present only once a client has some — a brand-new client
    # shows nothing but member-since and location.
    match = re.search(
        r"\$([\d,]+(?:\.\d+)?)\s*([KM])?\+?\s*total spent", client_region, re.I
    )
    if match:
        amount = float(match.group(1).replace(",", ""))
        multiplier = {"K": 1_000, "M": 1_000_000}.get((match.group(2) or "").upper(), 1)
        info.total_spent = amount * multiplier

    match = re.search(r"([\d,]+)\s+hires?", client_region, re.I)
    if match:
        info.total_hires = int(match.group(1).replace(",", ""))
    match = re.search(r"([\d,]+)\s+active", client_region, re.I)
    if match:
        info.active_hires = int(match.group(1).replace(",", ""))
    match = re.search(r"([\d,]+(?:\.\d+)?)\s+hours\b", client_region, re.I)
    if match:
        info.total_hours = float(match.group(1).replace(",", ""))

    match = re.search(
        r"((?:Small|Mid-sized|Large|Enterprise) company \([^)]{1,40}\)"
        r"|Individual client|Freelancer)",
        client_region, re.I,
    )
    if match:
        info.company_size = match.group(1).strip()

    info.industry = _industry(client_region, info)

    # The embedded payload is more precise than the rendered card ($325,900.10
    # rather than "$326K") and carries the rating and review count the card
    # never shows. Applied last so it wins over the text-scraped values.
    for field, value in parse_payload_stats(html).items():
        if field in ("total_hires", "active_hires", "total_reviews",
                     "total_jobs_with_hires", "total_posted_jobs", "open_jobs"):
            setattr(info, field, int(value))
        else:
            setattr(info, field, float(value))

    # Login-only fields. Not on public pages; parsed if they ever appear.
    match = re.search(r"(\d{1,3})%\s*hire rate", text, re.I)
    if match:
        info.hire_rate = int(match.group(1))
    match = re.search(r"([\d.]+)\s*of\s*5", text)
    if match:
        info.rating = float(match.group(1))
    if re.search(r"payment (method )?verified", text, re.I):
        info.payment_verified = True

    return info


def _industry(client_region: str, info: ClientInfo) -> str | None:
    """The category line, which sits between the hires line and company size.

    It has no label and no stable marker, so it is identified by position:
    a short line that is none of the values already extracted.
    """
    known = {
        (info.country or "").lower(), (info.city or "").lower(),
        (info.company_size or "").lower(),
    }
    # Label fragments that survive tag-stripping as their own lines.
    labels = re.compile(
        r"^(about the client|total spent|hires?|active|member since\b|"
        r"payment method verified|phone number verified)$|hires|active",
        re.I,
    )

    for line in _lines(client_region):
        low = line.lower()
        if low in known or low.startswith("member since") or labels.match(low):
            continue
        if re.search(r"\d", line) or len(line) > 40:
            continue  # money, hires, times and prose are not categories
        return line
    return None


def _curl_cffi_fetcher(proxy_manager: ProxyManager | None, cookie: str | None, timeout: int):
    """Default transport. Gets 403 from Cloudflare on job pages — see module docstring."""

    def fetch(url: str) -> tuple[int, str]:
        headers = dict(PAGE_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        proxy_dict = proxy_dict_for(
            proxy_manager.get_proxy() if proxy_manager else None
        )
        resp = requests.get(
            url, headers=headers, proxies=proxy_dict,
            impersonate="chrome", timeout=timeout,
        )
        return resp.status_code, resp.text

    return fetch


class ClientEnricher:
    """Fetches client details for jobs, one at a time, politely.

    `html_fetcher` is any callable taking a URL and returning either the HTML
    or an `(status_code, html)` pair — plug in a browser to get past Cloudflare.
    Failures skip the job; a run of consecutive failures trips the circuit
    breaker and stops the stage.
    """

    def __init__(
        self,
        proxy_manager: ProxyManager | None = None,
        cookie: str | None = None,
        delay: HumanDelay | None = None,
        breaker: CircuitBreaker | None = None,
        timeout: int = 30,
        html_fetcher=None,
    ):
        self.proxy_manager = proxy_manager
        self.cookie = (cookie or "").strip() or None
        self.delay = delay or HumanDelay()
        self.breaker = breaker or CircuitBreaker()
        self.timeout = timeout
        self.html_fetcher = html_fetcher or _curl_cffi_fetcher(
            proxy_manager, self.cookie, timeout
        )
        self.uses_browser = html_fetcher is not None

    def enrich(self, job: Job) -> ClientInfo:
        """Fetch one job's client details. Never raises — failures are reported."""
        cipher = job.cipher or ""

        self.delay.wait()

        try:
            result = self.html_fetcher(job.link)
        except Exception as e:
            self.breaker.record_failure()
            return ClientInfo(
                cipher=cipher, fetch_status="failed",
                fetch_error=f"{type(e).__name__}: {e}"[:200],
            )

        status, html = result if isinstance(result, tuple) else (200, result)

        if status == 429:
            # Upwork's own soft block. Trip the breaker immediately: continuing
            # extends the cool-off rather than getting through it.
            self.breaker.consecutive_failures = self.breaker.trip_after
            self.breaker.total_failures += 1
            return ClientInfo(
                cipher=cipher, fetch_status="rate_limited",
                fetch_error=(
                    "Upwork is serving its error page — this session is rate "
                    "limited. Wait before running again, and slow the pacing."
                ),
            )

        if status == 403:
            self.breaker.record_failure()
            return ClientInfo(
                cipher=cipher, fetch_status="blocked",
                fetch_error="HTTP 403 — Cloudflare challenge.",
            )

        if status == 503:
            self.breaker.record_failure()
            return ClientInfo(
                cipher=cipher, fetch_status="failed",
                fetch_error="Page loaded but never rendered the client card.",
            )
        if status != 200:
            self.breaker.record_failure()
            return ClientInfo(
                cipher=cipher, fetch_status="failed", fetch_error=f"HTTP {status}",
            )

        info = parse_client_info(html or "", cipher)
        if not info.is_usable:
            self.breaker.record_failure()
            info.fetch_status = "failed"
            info.fetch_error = "No client fields found in page"
            return info

        self.breaker.record_success()
        return info

    def enrich_all(self, jobs: list[Job], on_result=None) -> list[ClientInfo]:
        """Enrich a batch, skipping failures and stopping if the breaker trips."""
        results: list[ClientInfo] = []
        LOGGER.info(
            "Enriching %d jobs (%.0f-%.0fs between requests, ~%.0f min)",
            len(jobs), self.delay.min_seconds, self.delay.max_seconds,
            len(jobs) * (self.delay.min_seconds + self.delay.max_seconds) / 2 / 60,
        )
        if not self.uses_browser:
            LOGGER.warning(
                "No browser fetcher supplied — Cloudflare will 403 every request.\n%s",
                BROWSER_REQUIRED_HELP,
            )

        for index, job in enumerate(jobs, start=1):
            if self.breaker.is_open:
                LOGGER.error(
                    "Stopping enrichment at %d/%d — too many consecutive failures",
                    index, len(jobs),
                )
                break

            info = self.enrich(job)
            results.append(info)
            if on_result:
                on_result(info)

            if info.fetch_status != "ok":
                LOGGER.warning("Skipped %s: %s", job.cipher, info.fetch_error)
            elif index % 10 == 0:
                LOGGER.info("Enriched %d/%d", index, len(jobs))

        ok = sum(1 for r in results if r.fetch_status == "ok")
        LOGGER.info(
            "Enrichment done: %d succeeded, %d skipped, %d not attempted",
            ok, len(results) - ok, len(jobs) - len(results),
        )
        return results
