"""Reading Instagram's JSON payloads.

Written defensively. Instagram reshapes these responses without notice, and the
failure mode is silent: `web_profile_info` still returns HTTP 200 with a
perfectly valid envelope while the field you wanted has moved. Every field is
read through a list of candidate paths and a missing one yields a default.

One measured fact drives the whole design (2026-08-13):
`web_profile_info` returns `edge_owner_to_timeline_media.edges` **empty** for a
logged-in web session. It has the follower count, bio, category and privacy
flag, but no posts. So post dates come from a second endpoint,
`/api/v1/feed/user/<username>/username/`, which does return them with
`taken_at`. Anything that tries to get cadence out of the profile call will
quietly conclude every account is inactive.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ig_leads.models import Account, Candidate

LOGGER = logging.getLogger(__name__)


def _dig(obj: Any, *path, default=None):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def parse_profile(payload: dict | None, username: str) -> Account | None:
    """`web_profile_info` -> Account (without post dates)."""
    user = _dig(payload, "data", "user")
    if not isinstance(user, dict):
        return None

    return Account(
        username=user.get("username") or username,
        full_name=user.get("full_name") or "",
        biography=user.get("biography") or "",
        followers=int(_dig(user, "edge_followed_by", "count", default=0) or 0),
        following=int(_dig(user, "edge_follow", "count", default=0) or 0),
        total_posts=int(
            _dig(user, "edge_owner_to_timeline_media", "count", default=0) or 0
        ),
        is_private=bool(user.get("is_private")),
        is_verified=bool(user.get("is_verified")),
        is_business=bool(user.get("is_business_account")),
        is_professional=bool(user.get("is_professional_account")),
        category=user.get("category_name") or user.get("business_category_name") or "",
        external_url=user.get("external_url") or "",
        user_id=str(user.get("id") or ""),
    )


_COUNT_RE = re.compile(
    r"([\d][\d,.\s]*)\s*([KMB])?\s+Followers.*?"
    r"([\d][\d,.\s]*)\s*([KMB])?\s+Following.*?"
    r"([\d][\d,.\s]*)\s*([KMB])?\s+Posts",
    re.IGNORECASE | re.DOTALL,
)
_MULTIPLIER = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_count(number: str, suffix: str | None) -> int:
    """'14' + 'K' -> 14000. Instagram rounds these, so the result is approximate."""
    cleaned = (number or "").replace(",", "").replace(" ", "").strip()
    if not cleaned:
        return 0
    try:
        value = float(cleaned)
    except ValueError:
        return 0
    return int(value * _MULTIPLIER.get((suffix or "").upper(), 1))


def parse_profile_page(data: dict | None, username: str) -> Account | None:
    """Build an Account from the public page's Open Graph tags.

    Used only when the profile API refuses an account (see BENIGN_400_MARKERS).
    The counts are Instagram's rounded display values, so `followers` here is
    approximate — good enough for a 1k-500k band, not for a boundary decision.
    """
    if not isinstance(data, dict):
        return None

    description = data.get("description") or ""
    match = _COUNT_RE.search(description)
    if not match:
        return None

    followers = parse_count(match.group(1), match.group(2))
    following = parse_count(match.group(3), match.group(4))
    posts = parse_count(match.group(5), match.group(6))

    title = data.get("title") or ""
    full_name = title.split("(@")[0].strip() if "(@" in title else ""

    # The header block holds the bio; the first lines repeat the handle, name
    # and the counts, so drop anything that is one of those.
    header = data.get("header") or ""
    bio_lines = []
    for line in header.splitlines():
        line = line.strip()
        low = line.lower()
        if not line or line.lstrip("@").lower() == username.lower():
            continue
        if any(w in low for w in ("followers", "following", "posts")):
            continue
        if line == full_name:
            continue
        if low in ("follow", "message", "following", "verified"):
            continue
        bio_lines.append(line)

    return Account(
        username=username,
        full_name=full_name,
        biography=" ".join(bio_lines[:6]),
        followers=followers,
        following=following,
        total_posts=posts,
    )


def parse_post_dates(payload: dict | None) -> list[datetime]:
    """`/feed/user/<name>/username/` -> post timestamps, newest first.

    Pinned posts are the trap here: Instagram returns them at the top of the
    feed regardless of age, so the *first* item is not reliably the newest.
    Sorting fixes it — and without that, an account that pinned an old post
    looks dormant.
    """
    if not isinstance(payload, dict):
        return []

    items = payload.get("items")
    if not isinstance(items, list):
        return []

    dates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        when = _ts(item.get("taken_at") or item.get("taken_at_timestamp"))
        if when:
            dates.append(when)

    return sorted(dates, reverse=True)


def parse_hashtag_authors(payload: dict | None, niche: str, hashtag: str) -> list[Candidate]:
    """Pull post authors out of a hashtag payload.

    The payload nests media under `data.recent.sections[].layout_content.medias[]`
    and a parallel `top` block, with the exact shape varying between responses.
    Rather than hard-coding that path, this walks the structure looking for any
    object that carries both a `user.username` and a timestamp — which is what
    a media object is, whatever it happens to be wrapped in this week.
    """
    if not isinstance(payload, dict):
        return []

    found: dict[str, Candidate] = {}

    def walk(node: Any, depth: int = 0):
        if depth > 12 or node is None:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return

        user = node.get("user")
        taken = node.get("taken_at") or node.get("taken_at_timestamp")
        if isinstance(user, dict) and user.get("username") and taken:
            name = user["username"]
            if name not in found:
                found[name] = Candidate(
                    username=name,
                    niche=niche,
                    hashtag=hashtag,
                    is_private=user.get("is_private"),
                    is_verified=user.get("is_verified"),
                    seen_post_at=_ts(taken),
                )
        for value in node.values():
            walk(value, depth + 1)

    walk(payload)
    return list(found.values())
