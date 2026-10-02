"""High-level entry point: `UpworkScraper`.

Wires the token manager, proxy manager and job fetcher together so callers only
need `UpworkScraper().scrape()`. Nothing here touches a database — results come
back as `Job` objects for the caller to do whatever they want with.
"""

import logging
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime, timezone

from upwork_scraper import config
from upwork_scraper.auth.token_manager import TokenManager
from upwork_scraper.errors import TokenExpired
from upwork_scraper.models.job_models import Job
from upwork_scraper.proxies.proxy_manager import (
    NoProxyManager,
    ProxyManager,
    build_proxy_manager,
    proxy_dict_for,
)
from upwork_scraper.scrapers.job_fetcher import CONCURRENT_WORKERS, fetch_all_jobs

LOGGER = logging.getLogger(__name__)

# Without proxies every request leaves from the same IP, so stay gentle.
DIRECT_WORKERS = 2

# Ciphers remembered by scrape_loop before the oldest are forgotten. At the
# observed ~3 new jobs/min site-wide this is several days of history.
SEEN_LIMIT = 20_000


class UpworkScraper:

    def __init__(
        self,
        proxy_manager: ProxyManager | None = None,
        token_manager: TokenManager | None = None,
        workers: int | None = None,
    ):
        self.proxy_manager = proxy_manager or build_proxy_manager()
        self.token_manager = token_manager or TokenManager()

        if workers is not None:
            self.workers = workers
        elif isinstance(self.proxy_manager, NoProxyManager):
            self.workers = DIRECT_WORKERS
        else:
            self.workers = CONCURRENT_WORKERS

    def scrape(
        self,
        max_pages: int = config.MAX_PAGES,
        query: str | Sequence[str] | None = None,
        sort: str = "recency",
        pin_proxy: bool = False,
        dedupe: bool = True,
        max_age_minutes: float | None = None,
        job_filter: Callable[[Job], bool] | None = None,
    ) -> list[Job]:
        """Run one scrape cycle and return the jobs found, newest first.

        `query` may be a single keyword or a list of them — each is searched
        separately and the results merged, with `Job.matched_query` recording
        which keyword(s) surfaced each job. `max_age_minutes` keeps only jobs
        published within that window, and `job_filter` (e.g. a `Niche`'s
        relevance check) drops anything that returns False.

        On a token-expired response the token is refreshed and the cycle is
        retried once.
        """
        queries: list[str | None] = _normalize_queries(query)

        jobs: list[Job] = []
        for keyword in queries:
            batch = self._scrape_one(max_pages, keyword, sort, pin_proxy)
            if keyword:
                for job in batch:
                    job.matched_query = keyword
            jobs.extend(batch)

        if dedupe:
            jobs = _dedupe(jobs)
        if job_filter is not None:
            before = len(jobs)
            jobs = [j for j in jobs if job_filter(j)]
            if before != len(jobs):
                LOGGER.info(
                    "Relevance filter kept %d of %d jobs", len(jobs), before
                )
        if max_age_minutes is not None:
            jobs = _filter_by_age(jobs, max_age_minutes)

        jobs.sort(key=lambda j: j.published_date, reverse=True)
        return jobs

    def _scrape_one(
        self, max_pages: int, query: str | None, sort: str, pin_proxy: bool
    ) -> list[Job]:
        for attempt in (1, 2):
            proxy_dict = proxy_dict_for(self.proxy_manager.get_proxy())
            token = self.token_manager.get_token(proxy_dict=proxy_dict)

            try:
                return fetch_all_jobs(
                    token,
                    self.proxy_manager,
                    max_pages=max_pages,
                    query=query,
                    sort=sort,
                    workers=self.workers,
                    pinned_proxy_dict=proxy_dict if pin_proxy else None,
                )
            except TokenExpired:
                self.token_manager.invalidate()
                if attempt == 2:
                    raise
                LOGGER.warning("Token expired, refreshing and retrying once")

        return []  # unreachable — the loop either returns or raises

    def scrape_loop(
        self,
        interval: int = config.SCRAPE_INTERVAL,
        max_pages: int = config.MAX_PAGES,
        query: str | Sequence[str] | None = None,
        sort: str = "recency",
        pin_proxy: bool = False,
        only_new: bool = True,
        max_age_minutes: float | None = None,
        skip_backlog: bool = False,
        job_filter: Callable[[Job], bool] | None = None,
        initial_seen: Iterable[str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> Iterator[list[Job]]:
        """Yield newly posted jobs every `interval` seconds until stopped.

        With `only_new`, ciphers already yielded are filtered out — the
        in-memory stand-in for the original project's `ON CONFLICT DO NOTHING`.
        `skip_backlog` swallows the first cycle (the jobs that already existed
        when you started) so you only see jobs posted from now on. Pass
        `initial_seen` (e.g. ciphers from a backfill) to suppress jobs you
        already have.

        Warns when a cycle comes back entirely new, which means jobs were
        probably posted and missed between polls — shorten `interval` or raise
        `max_pages`.
        """
        stop = stop_event or threading.Event()
        seen: dict[str, None] = dict.fromkeys(initial_seen or [])
        first_cycle = True

        while not stop.is_set():
            try:
                jobs = self.scrape(
                    max_pages=max_pages,
                    query=query,
                    sort=sort,
                    pin_proxy=pin_proxy,
                    max_age_minutes=max_age_minutes,
                    job_filter=job_filter,
                )
                fetched = len(jobs)

                if only_new:
                    jobs = [j for j in jobs if j.cipher not in seen]
                    for job in jobs:
                        if job.cipher:
                            seen[job.cipher] = None
                    _trim(seen)

                if first_cycle and skip_backlog:
                    LOGGER.info("Skipping %d pre-existing jobs (backlog)", len(jobs))
                    jobs = []
                elif not first_cycle and fetched and len(jobs) == fetched:
                    LOGGER.warning(
                        "Every one of the %d jobs fetched was new — some may have "
                        "been missed. Lower --interval or raise --pages.",
                        fetched,
                    )

                first_cycle = False
                yield jobs
            except Exception:
                LOGGER.exception("Error during scrape cycle")
                stop.wait(30)
                continue

            stop.wait(interval)


def _normalize_queries(query: str | Sequence[str] | None) -> list[str | None]:
    """Accept None, one keyword, or many; always return a list to iterate."""
    if query is None:
        return [None]
    if isinstance(query, str):
        return [query]

    keywords = [k.strip() for k in query if k and k.strip()]
    return keywords or [None]


def _dedupe(jobs: list[Job]) -> list[Job]:
    """Drop repeated ciphers, merging the keywords that matched each job."""
    by_cipher: dict[str, Job] = {}
    unique: list[Job] = []

    for job in jobs:
        if not job.cipher:
            unique.append(job)
            continue

        seen = by_cipher.get(job.cipher)
        if seen is None:
            by_cipher[job.cipher] = job
            unique.append(job)
        elif job.matched_query and job.matched_query not in (seen.matched_query or ""):
            seen.matched_query = f"{seen.matched_query}, {job.matched_query}"

    return unique


def _filter_by_age(jobs: list[Job], max_age_minutes: float) -> list[Job]:
    now = datetime.now(timezone.utc)
    cutoff = max_age_minutes * 60
    return [j for j in jobs if j.age_seconds(now) <= cutoff]


def _trim(seen: dict[str, None], limit: int = SEEN_LIMIT):
    """Forget the oldest ciphers so a long-running loop stays bounded."""
    excess = len(seen) - limit
    for cipher in list(seen)[:excess]:
        del seen[cipher]
