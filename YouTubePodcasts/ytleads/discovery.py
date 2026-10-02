"""Candidate channel discovery.

Two sources, deliberately ordered by cost:

  1. Keyword search over videos published in the last N days (100 units/page).
     Searching videos rather than channels means every hit already proves the
     channel uploaded recently -- the cadence filter then confirms it properly.

  2. Snowballing off qualified leads via their featured channels (1 unit each).
     Creators feature peers in the same niche and size bracket, so this is by
     far the cheapest source of on-target candidates once a run gets going.

Everything already in the DB is dropped before a single unit is spent on it.
"""

from __future__ import annotations

from typing import Callable, Iterator

from .filters import published_after_iso
from .store import Store
from .youtube_api import QuotaExhausted, YouTubeClient

Logger = Callable[[str], None]


def from_seeds(
    client: YouTubeClient,
    store: Store,
    seeds: list[str],
    pages_per_seed: int = 1,
    published_within_days: int = 21,
    order: str = "relevance",
    region_code: str = "",
    relevance_language: str = "",
    log: Logger = print,
) -> Iterator[tuple[str, str]]:
    """Yield (channel_id, seed) for channels never seen before."""
    published_after = published_after_iso(published_within_days)
    emitted: set[str] = set()
    known = store.known_ids()

    for seed in seeds:
        page_token: str | None = None
        for page in range(pages_per_seed):
            if store.seed_done_today(seed, page):
                continue
            if not client.can_afford(100):
                log(f"[!] Out of quota before seed '{seed}' (page {page + 1}).")
                return
            try:
                resp = client.search_videos(
                    query=seed,
                    published_after=published_after,
                    page_token=page_token,
                    order=order,
                    region_code=region_code,
                    relevance_language=relevance_language,
                )
            except QuotaExhausted:
                log(f"[!] Quota exhausted during seed '{seed}'.")
                return

            store.mark_seed_done(seed, page)
            items = resp.get("items", [])
            new_here = 0
            for item in items:
                cid = (item.get("snippet") or {}).get("channelId")
                if cid and cid not in known and cid not in emitted:
                    emitted.add(cid)
                    new_here += 1
                    yield cid, f"search:{seed}"

            log(f"    '{seed}' p{page + 1}: {len(items)} results -> {new_here} new "
                f"(quota {client.used}/{client.daily_limit})")

            page_token = resp.get("nextPageToken")
            if not page_token:
                break


def snowball(
    client: YouTubeClient,
    store: Store,
    source_channel_ids: list[str],
    max_per_channel: int = 25,
    log: Logger = print,
) -> Iterator[tuple[str, str]]:
    """Yield (channel_id, source) from the featured channels of qualified leads."""
    emitted: set[str] = set()
    known = store.known_ids()

    for src in source_channel_ids:
        if not client.can_afford(1):
            log("[!] Out of quota during snowball.")
            return
        try:
            featured = client.featured_channels(src, limit=max_per_channel)
        except QuotaExhausted:
            log("[!] Quota exhausted during snowball.")
            return
        for cid in featured:
            if cid and cid not in known and cid not in emitted:
                emitted.add(cid)
                yield cid, f"featured_by:{src}"
