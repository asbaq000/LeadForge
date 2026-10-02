"""Driving the browser and harvesting SearchTimeline responses.

Why it reads X's own API responses instead of scraping the DOM:

* The DOM truncates long posts behind a "Show more" link, and long posts are
  exactly the ones worth having — a real brief lists budget, format and volume.
* The DOM has no follower count, no bio, and no `professional.category`, all of
  which the classifier leans on to tell a hiring creator from a freelance
  editor pitching for work.
* Reading one JSON body is far cheaper than 20 locator round-trips per screen.

Replaying those API calls directly was tried and does not work: X signs every
GraphQL request with a single-use `x-client-transaction-id`, and a replayed URL
gets 404 (from an API context) or 403 (from page `fetch`), with or without the
original headers. So the browser stays in the loop and does the signing; we
just listen. Scrolling is the pagination mechanism.

The run is bounded by *age*, not by a scroll count. Latest is strictly
reverse-chronological, so the moment a response contains a tweet older than the
cutoff there is provably nothing newer further down, and the query is done.
That is what stops the scraper pulling thousands of stale posts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from x_leads.errors import SearchBlocked
from x_leads.models import Tweet
from x_leads.search.parser import parse_timeline
from x_leads.search.query import search_url

LOGGER = logging.getLogger(__name__)

TIMELINE_MARKER = "SearchTimeline"
TWEET_SELECTOR = 'article[data-testid="tweet"]'

# Everything that costs bandwidth and renders nothing we read. Stylesheets go
# too: the timeline still lays out and scrolls without them (measured), and
# they are a large share of the bytes.
BLOCKED_RESOURCES = frozenset({"image", "media", "font", "stylesheet"})

# X's "no results" state. Distinct from a failure — a precise query legitimately
# returning nothing is the normal case for a narrow phrase on a quiet day.
EMPTY_MARKERS = (
    "no results for",
    "try searching for something else",
)
BLOCK_MARKERS = (
    "rate limit exceeded",
    "something went wrong. try reloading",
    "try again later",
)


@dataclass
class CollectStats:
    queries_run: int = 0
    responses_seen: int = 0
    tweets_seen: int = 0
    unique_tweets: int = 0
    scrolls: int = 0
    stopped_on_age: int = 0
    empty_queries: int = 0
    blocked_queries: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        blocked = (
            f", {self.blocked_queries} BLOCKED" if self.blocked_queries else ""
        )
        return (
            f"{self.queries_run} queries, {self.scrolls} scrolls, "
            f"{self.unique_tweets} unique tweets "
            f"({self.stopped_on_age} ended at the age cutoff{blocked})"
        )


class Collector:
    """Runs searches against one signed-in browser context.

    Use as an async context manager:

        async with Collector(storage_state) as c:
            tweets = await c.run_queries(queries)
    """

    def __init__(
        self,
        storage_state: dict,
        max_age_hours: float = 48.0,
        max_scrolls: int = 12,
        patience: int = 3,
        max_tweets_per_query: int = 400,
        concurrency: int = 2,
        headless: bool = True,
        scroll_pause_s: float = 1.1,
        nav_timeout_ms: int = 45_000,
        first_result_timeout_ms: int = 25_000,
        block_retries: int = 2,
        block_backoff_s: float = 45.0,
    ):
        self.storage_state = storage_state
        self.max_age_hours = max_age_hours
        self.max_scrolls = max_scrolls
        self.patience = patience
        self.max_tweets_per_query = max_tweets_per_query
        # X rate-limits search hard. Two pages in flight is a measured
        # compromise: it halves wall-clock against serial, and unlike the old
        # scraper's four simultaneous browsers it does not trip the limiter.
        self.concurrency = max(1, concurrency)
        self.headless = headless
        self.scroll_pause_s = scroll_pause_s
        self.nav_timeout_ms = nav_timeout_ms
        self.first_result_timeout_ms = first_result_timeout_ms
        self.block_retries = block_retries
        self.block_backoff_s = block_backoff_s

        self.stats = CollectStats()
        self._playwright = None
        self._browser = None
        self._context = None

    @property
    def cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(hours=self.max_age_hours)

    # ------------------------------------------------------------------ setup
    async def start(self) -> "Collector":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            storage_state=self.storage_state,
            viewport={"width": 1280, "height": 900},
        )
        self._context.set_default_timeout(self.nav_timeout_ms)
        await self._context.route("**/*", self._route)
        return self

    @staticmethod
    async def _route(route):
        try:
            if route.request.resource_type in BLOCKED_RESOURCES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            # The page can go away mid-flight; a dead route is not an error.
            pass

    async def close(self):
        for obj, method in (
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ):
            try:
                if obj is not None:
                    await getattr(obj, method)()
            except Exception:
                pass
        self._context = self._browser = self._playwright = None

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *exc):
        await self.close()
        return False

    # -------------------------------------------------------------- one query
    async def run_query(self, query: str) -> list[Tweet]:
        """One search, retried if X rate-limits it.

        The retry matters more than it looks. A blocked query returns an empty
        timeline that is indistinguishable from a quiet one, so without this a
        rate limit reads as "nobody is hiring" — and it lands on whichever
        query happens to be running, which on a live run was the single most
        valuable one ("hiring a video editor"). Backing off and retrying is the
        difference between a partial run you can trust and one you can't.
        """
        for attempt in range(1, self.block_retries + 2):
            try:
                return await self._attempt_query(query)
            except SearchBlocked:
                if attempt > self.block_retries:
                    msg = f"rate limited (gave up after {attempt} tries): {_short(query, 50)}"
                    LOGGER.error("  %s", msg)
                    self.stats.errors.append(msg)
                    self.stats.blocked_queries += 1
                    return []
                wait = self.block_backoff_s * attempt
                LOGGER.warning(
                    "  rate limited — waiting %gs then retrying (%d/%d): %s",
                    wait, attempt, self.block_retries, _short(query, 50),
                )
                await asyncio.sleep(wait)
        return []

    async def _attempt_query(self, query: str) -> list[Tweet]:
        """Scroll one search until the age cutoff, exhaustion, or the caps."""
        if self._context is None:
            raise RuntimeError("Collector not started")

        found: dict[str, Tweet] = {}
        pending: set[asyncio.Task] = set()
        reached_cutoff = False
        cutoff = self.cutoff
        page = await self._context.new_page()

        async def handle(response):
            nonlocal reached_cutoff
            if TIMELINE_MARKER not in response.url:
                return
            try:
                payload = await response.json()
            except Exception:
                # A 429 body isn't JSON; the status check below reports it.
                if response.status in (429, 403):
                    self.stats.errors.append(f"HTTP {response.status} on search")
                return

            self.stats.responses_seen += 1
            tweets, _cursor = parse_timeline(payload)
            self.stats.tweets_seen += len(tweets)

            for tweet in tweets:
                if tweet.created_at and tweet.created_at < cutoff:
                    # Reverse-chronological: everything below this is older.
                    reached_cutoff = True
                    continue
                if tweet.tweet_id not in found:
                    tweet.matched_queries.append(query)
                    found[tweet.tweet_id] = tweet

        def on_response(response):
            task = asyncio.create_task(handle(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        page.on("response", on_response)

        try:
            await page.goto(
                search_url(query), wait_until="domcontentloaded",
                timeout=self.nav_timeout_ms,
            )

            if not await self._await_first_results(page, query):
                return []

            stagnant = 0
            for scroll in range(self.max_scrolls):
                before = len(found)
                await page.keyboard.press("End")
                await asyncio.sleep(self.scroll_pause_s)
                self.stats.scrolls += 1

                if reached_cutoff:
                    self.stats.stopped_on_age += 1
                    LOGGER.debug("Age cutoff reached after %d scrolls", scroll + 1)
                    break
                if len(found) >= self.max_tweets_per_query:
                    LOGGER.debug("Per-query cap hit (%d)", self.max_tweets_per_query)
                    break

                if len(found) == before:
                    stagnant += 1
                    if stagnant >= self.patience:
                        LOGGER.debug("Timeline exhausted after %d scrolls", scroll + 1)
                        break
                else:
                    stagnant = 0

        except SearchBlocked:
            # Handled by run_query's backoff. Swallowing it here is what made a
            # rate limit look like an empty result set.
            raise
        except Exception as e:
            msg = f"{type(e).__name__} on {_short(query, 50)}"
            LOGGER.warning("Search failed: %s", msg)
            self.stats.errors.append(msg)
        finally:
            # Responses already in flight still carry tweets worth keeping.
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)
            try:
                page.remove_listener("response", on_response)
                await page.close()
            except Exception:
                pass

        self.stats.queries_run += 1
        LOGGER.info("  %-3d tweets | %s", len(found), _short(query))
        return list(found.values())

    async def _await_first_results(self, page, query: str) -> bool:
        """True when the timeline rendered; False for a legitimately empty one.

        An empty result and a block look identical from the outside — both are
        "no articles appeared" — so the page text is read to tell them apart.
        Mistaking a rate limit for an empty search would quietly report zero
        leads on a day full of them.
        """
        try:
            await page.wait_for_selector(
                TWEET_SELECTOR, timeout=self.first_result_timeout_ms
            )
            return True
        except Exception:
            pass

        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            body = ""

        if any(m in body for m in BLOCK_MARKERS):
            raise SearchBlocked(
                "X is rate limiting search. Wait a few minutes and re-run; "
                "lower --concurrency if it keeps happening."
            )
        if any(m in body for m in EMPTY_MARKERS) or body:
            self.stats.empty_queries += 1
            LOGGER.info("  0   tweets | %s", _short(query))
            self.stats.queries_run += 1
            return False

        self.stats.errors.append(f"blank page for {_short(query)}")
        return False

    # ------------------------------------------------------------ many queries
    async def run_queries(self, queries: list[str]) -> list[Tweet]:
        """Every query, merged and deduplicated by tweet id.

        A tweet matched by several queries keeps all of them in
        `matched_queries`, which is how you find out that a phrase is earning
        its place or only ever duplicating another.
        """
        semaphore = asyncio.Semaphore(self.concurrency)

        async def guarded(index: int, q: str) -> list[Tweet]:
            async with semaphore:
                # Stagger the starts. Firing every permitted search in the same
                # instant is what a rate limiter is looking for.
                if index:
                    await asyncio.sleep(min(index, self.concurrency) * 0.7)
                return await self.run_query(q)

        results = await asyncio.gather(
            *(guarded(i, q) for i, q in enumerate(queries)), return_exceptions=True
        )

        merged: dict[str, Tweet] = {}
        for result in results:
            if isinstance(result, BaseException):
                self.stats.errors.append(f"{type(result).__name__}: {result}")
                continue
            for tweet in result:
                existing = merged.get(tweet.tweet_id)
                if existing is None:
                    merged[tweet.tweet_id] = tweet
                else:
                    for q in tweet.matched_queries:
                        if q not in existing.matched_queries:
                            existing.matched_queries.append(q)

        self.stats.unique_tweets = len(merged)
        return list(merged.values())


def _short(query: str, width: int = 70) -> str:
    return query if len(query) <= width else query[: width - 1] + "…"
