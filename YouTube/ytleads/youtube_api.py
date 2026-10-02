"""Quota-aware YouTube Data API v3 wrapper.

Quota is the real constraint on this whole project: a default project gets
10,000 units/day, and search.list costs 100 of them per call. Everything here
is shaped around spending as few units as possible per qualified lead:

    search.list          100 units   (<=50 candidates)
    channels.list          1 unit    (up to 50 channels per call -- always batch)
    playlistItems.list     1 unit    (up to 50 uploads)
    channelSections.list   1 unit    (featured channels, for snowballing)

Every call is metered into the DB before it goes out, so a crashed run never
loses track of what it already spent today.
"""

from __future__ import annotations

import random
import time
from typing import Any, Iterator, Sequence

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .store import Store

COST_SEARCH = 100
COST_LIST = 1


class QuotaExhausted(RuntimeError):
    """Raised when the daily allowance is gone -- the run stops cleanly."""


class YouTubeClient:
    def __init__(
        self,
        api_key: str,
        store: Store,
        daily_limit: int = 10_000,
        reserve: int = 200,
        max_retries: int = 4,
    ):
        self.yt = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
        self.store = store
        self.daily_limit = daily_limit
        self.reserve = reserve
        self.max_retries = max_retries

    # -- quota -------------------------------------------------------------
    @property
    def used(self) -> int:
        return self.store.quota_used()

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self.reserve - self.used)

    def can_afford(self, units: int) -> bool:
        return self.remaining >= units

    def _spend(self, units: int) -> None:
        if not self.can_afford(units):
            raise QuotaExhausted(
                f"Daily quota spent: {self.used}/{self.daily_limit} units "
                f"(reserve {self.reserve}). Resets at midnight Pacific."
            )
        self.store.add_quota(units)

    # -- transport ---------------------------------------------------------
    def _execute(self, request: Any, cost: int) -> dict[str, Any]:
        self._spend(cost)
        delay = 1.0
        for attempt in range(self.max_retries):
            try:
                return request.execute()
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                reason = _http_reason(exc)
                if reason in ("quotaExceeded", "dailyLimitExceeded"):
                    # Our local counter drifted from Google's. Trust Google.
                    self.store.add_quota(self.remaining)
                    raise QuotaExhausted(f"API reports quota exceeded: {reason}") from exc
                if status in (403, 404) and reason in (
                    "channelNotFound", "playlistNotFound", "forbidden", "notFound",
                ):
                    return {}
                if status and status >= 500 or status == 429:
                    if attempt == self.max_retries - 1:
                        raise
                    time.sleep(delay + random.uniform(0, 0.5))
                    delay *= 2
                    continue
                raise
        return {}

    # -- endpoints ---------------------------------------------------------
    def search_videos(
        self,
        query: str,
        published_after: str,
        page_token: str | None = None,
        order: str = "relevance",
        region_code: str = "",
        relevance_language: str = "",
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Search recent VIDEOS -- a hit proves the channel uploaded lately."""
        params: dict[str, Any] = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": order,
            "maxResults": max_results,
            "publishedAfter": published_after,
        }
        if page_token:
            params["pageToken"] = page_token
        if region_code:
            params["regionCode"] = region_code
        if relevance_language:
            params["relevanceLanguage"] = relevance_language
        return self._execute(self.yt.search().list(**params), COST_SEARCH)

    def channels(self, channel_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Batched channel lookup. 50 ids for 1 unit -- never call this singly."""
        out: list[dict[str, Any]] = []
        for chunk in _chunks(list(channel_ids), 50):
            resp = self._execute(
                self.yt.channels().list(
                    part="snippet,statistics,contentDetails,brandingSettings,topicDetails",
                    id=",".join(chunk),
                    maxResults=50,
                ),
                COST_LIST,
            )
            out.extend(resp.get("items", []))
        return out

    def uploads(self, uploads_playlist_id: str, limit: int = 15) -> list[dict[str, Any]]:
        """Most recent uploads (newest first) with publish dates and titles."""
        resp = self._execute(
            self.yt.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=uploads_playlist_id,
                maxResults=min(limit, 50),
            ),
            COST_LIST,
        )
        return resp.get("items", [])

    def featured_channels(self, channel_id: str, limit: int = 25) -> list[str]:
        """Channels this one features on its homepage -- cheap graph expansion."""
        resp = self._execute(
            self.yt.channelSections().list(part="contentDetails", channelId=channel_id),
            COST_LIST,
        )
        found: list[str] = []
        for item in resp.get("items", []):
            for cid in item.get("contentDetails", {}).get("channels", []) or []:
                if cid not in found:
                    found.append(cid)
                if len(found) >= limit:
                    return found
        return found


def _http_reason(exc: HttpError) -> str:
    try:
        errors = exc.error_details  # type: ignore[attr-defined]
        if errors:
            return str(errors[0].get("reason", ""))
    except Exception:
        pass
    try:
        import json

        payload = json.loads(exc.content.decode("utf-8"))
        return str(payload["error"]["errors"][0].get("reason", ""))
    except Exception:
        return ""


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
