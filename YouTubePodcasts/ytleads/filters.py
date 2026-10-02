"""Subscriber-range and upload-cadence gates."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SubResult(NamedTuple):
    ok: bool
    subscribers: int
    reason: str


def check_subscribers(
    stats: dict[str, Any], min_subs: int, max_subs: int, reject_hidden: bool
) -> SubResult:
    hidden = bool(stats.get("hiddenSubscriberCount"))
    raw = stats.get("subscriberCount")

    if hidden or raw is None:
        if reject_hidden:
            return SubResult(False, 0, "subscriber count hidden")
        return SubResult(True, 0, "subscriber count hidden (allowed)")

    try:
        subs = int(raw)
    except (TypeError, ValueError):
        return SubResult(False, 0, "unparseable subscriber count")

    if subs < min_subs:
        return SubResult(False, subs, f"{subs:,} subs < {min_subs:,}")
    if subs > max_subs:
        return SubResult(False, subs, f"{subs:,} subs > {max_subs:,}")
    return SubResult(True, subs, "")


class CadenceResult(NamedTuple):
    ok: bool
    last_upload: str | None
    days_since_upload: float | None
    median_gap_days: float | None
    sampled: int
    reason: str
    video_titles: list[str]
    video_ids: list[str]
    episodes_per_month: float | None


def check_cadence(
    upload_items: list[dict[str, Any]],
    max_days_since_upload: int,
    max_median_gap_days: int,
    min_sampled: int,
) -> CadenceResult:
    """Enforce "at least one upload every N weeks" from the uploads playlist.

    Note: playlistItems.snippet.publishedAt is when the video was *added to the
    playlist*, which is not always the publish date. contentDetails.
    videoPublishedAt is the real one, so that is what we read.
    """
    dated: list[tuple[datetime, str, str]] = []
    for item in upload_items:
        ts = parse_ts(item.get("contentDetails", {}).get("videoPublishedAt")) or parse_ts(
            item.get("snippet", {}).get("publishedAt")
        )
        if ts:
            snip = item.get("snippet", {}) or {}
            vid = (item.get("contentDetails", {}) or {}).get("videoId") or (
                (snip.get("resourceId") or {}).get("videoId") or ""
            )
            dated.append((ts, snip.get("title", ""), vid))

    titles = [t for _, t, _ in dated]
    vids = [v for _, _, v in dated if v]

    if not dated:
        return CadenceResult(False, None, None, None, 0, "no uploads found",
                             titles, vids, None)

    dated.sort(key=lambda p: p[0], reverse=True)
    stamps = [d for d, _, _ in dated]

    now = datetime.now(timezone.utc)
    newest = stamps[0]
    days_since = (now - newest).total_seconds() / 86400.0
    last_upload = newest.date().isoformat()

    if len(stamps) < min_sampled:
        return CadenceResult(
            False, last_upload, round(days_since, 1), None, len(stamps),
            f"only {len(stamps)} uploads -- too few to judge cadence",
            titles, vids, None,
        )

    if days_since > max_days_since_upload:
        return CadenceResult(
            False, last_upload, round(days_since, 1), None, len(stamps),
            f"last upload {days_since:.0f}d ago > {max_days_since_upload}d",
            titles, vids, None,
        )

    gaps = [
        (stamps[i] - stamps[i + 1]).total_seconds() / 86400.0
        for i in range(len(stamps) - 1)
    ]
    median_gap = statistics.median(gaps) if gaps else 0.0
    # Publishing rate over the sampled window, which is what an outreach note
    # actually wants ("weekly show") rather than a raw gap in days.
    span_days = (stamps[0] - stamps[-1]).total_seconds() / 86400.0
    per_month = round((len(stamps) - 1) / span_days * 30.0, 1) if span_days > 0 else None

    if median_gap > max_median_gap_days:
        return CadenceResult(
            False, last_upload, round(days_since, 1), round(median_gap, 1), len(stamps),
            f"median gap {median_gap:.0f}d > {max_median_gap_days}d",
            titles, vids, per_month,
        )

    return CadenceResult(
        True, last_upload, round(days_since, 1), round(median_gap, 1), len(stamps),
        "", titles, vids, per_month,
    )


def published_after_iso(days: int) -> str:
    """RFC-3339 timestamp for search.list publishedAfter."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
