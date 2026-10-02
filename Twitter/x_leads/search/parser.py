"""Turning X's SearchTimeline GraphQL payload into Tweet objects.

Written defensively on purpose. X has already moved the author fields once:
`user.legacy.screen_name` became `user.core.screen_name`, `followers_count`
became `relationship_counts.followers`, and `description` became
`profile_bio.description`. A payload captured on 2026-08-13 carried an *empty*
`legacy` dict alongside the new fields, so code that trusted `legacy` got blank
authors rather than an error — the worst kind of breakage, because the run
still "succeeds" and every lead is anonymous.

Every field is therefore read through a list of candidate paths, newest first,
and a missing field yields a default instead of raising. `parse_timeline`
returns what it could parse and counts what it could not, so a schema change
shows up as a warning with a number attached.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Iterable

from x_leads.models import Author, Tweet

LOGGER = logging.getLogger(__name__)

# X's created_at format: "Thu Aug 13 02:51:33 +0000 2026"
_TIME_FORMAT = "%a %b %d %H:%M:%S %z %Y"


def _dig(obj: Any, *path: str, default: Any = None) -> Any:
    """Walk a dotted path, returning `default` the moment it doesn't exist."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _first(obj: Any, paths: Iterable[tuple[str, ...]], default: Any = None) -> Any:
    """The first path that resolves to something non-empty."""
    for path in paths:
        value = _dig(obj, *path)
        if value not in (None, "", {}, []):
            return value
    return default


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, _TIME_FORMAT)
    except (ValueError, TypeError):
        return None


def parse_author(user_result: dict | None) -> Author:
    """Author fields, tolerant of both the old `legacy` and current shapes."""
    if not isinstance(user_result, dict):
        return Author()

    legacy = user_result.get("legacy") or {}

    handle = _first(user_result, [
        ("core", "screen_name"),
        ("legacy", "screen_name"),
    ], default="")

    name = _first(user_result, [
        ("core", "name"),
        ("legacy", "name"),
    ], default="")

    bio = _first(user_result, [
        ("profile_bio", "description"),
        ("legacy", "description"),
    ], default="")

    location = _first(user_result, [
        ("location", "location"),
        ("legacy", "location"),
    ], default="")

    followers = _as_int(_first(user_result, [
        ("relationship_counts", "followers"),
        ("legacy", "followers_count"),
    ], default=0))

    following = _as_int(_first(user_result, [
        ("relationship_counts", "following"),
        ("legacy", "friends_count"),
    ], default=0))

    tweets = _as_int(_first(user_result, [
        ("tweet_counts", "tweets"),
        ("legacy", "statuses_count"),
    ], default=0))

    verified = bool(_first(user_result, [
        ("verification", "verified"),
        ("legacy", "verified"),
    ], default=False))

    website = _first(user_result, [
        ("website", "url"),
        ("legacy", "url"),
    ], default="")

    # X's self-declared profession. `category` is a list of {id, name}; the
    # name is what matters ("Editor", "Video Creator", "Content creator").
    category = ""
    cats = _dig(user_result, "professional", "category")
    if isinstance(cats, list) and cats:
        first = cats[0]
        if isinstance(first, dict):
            category = first.get("name") or ""

    return Author(
        handle=handle or "",
        display_name=name or "",
        bio=bio or "",
        location=location or "",
        followers=followers,
        following=following,
        tweets=tweets,
        verified=verified,
        blue_verified=bool(user_result.get("is_blue_verified")),
        professional_category=category,
        website=website or "",
        user_id=user_result.get("rest_id") or legacy.get("user_id_str") or "",
    )


def _unwrap(result: dict | None) -> dict | None:
    """Peel `TweetWithVisibilityResults`, which wraps the real tweet.

    X returns this variant for anything behind an interstitial. Missing it
    drops those tweets entirely, and they are ordinary posts.
    """
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        inner = result.get("tweet")
        if isinstance(inner, dict):
            return inner
    return result


def parse_tweet(result: dict | None) -> Tweet | None:
    """One tweet, or None if this entry isn't a usable tweet."""
    result = _unwrap(result)
    if not isinstance(result, dict):
        return None

    legacy = result.get("legacy") or {}
    tweet_id = result.get("rest_id") or legacy.get("id_str")
    if not tweet_id:
        return None

    # Posts over 280 characters keep their real body in `note_tweet` and a
    # truncated copy in `full_text`. Job posts are exactly the long ones, so
    # reading only `full_text` would cut off the budget and the requirements —
    # the parts that make a lead worth having.
    text = _dig(
        result, "note_tweet", "note_tweet_results", "result", "text", default=""
    ) or legacy.get("full_text") or ""

    author = parse_author(_dig(result, "core", "user_results", "result"))

    return Tweet(
        tweet_id=str(tweet_id),
        text=text,
        created_at=parse_time(legacy.get("created_at")),
        lang=legacy.get("lang") or "",
        author=author,
        likes=_as_int(legacy.get("favorite_count")),
        replies=_as_int(legacy.get("reply_count")),
        retweets=_as_int(legacy.get("retweet_count")),
        quotes=_as_int(legacy.get("quote_count")),
        bookmarks=_as_int(legacy.get("bookmark_count")),
        views=_as_int(_dig(result, "views", "count")),
        is_reply=bool(legacy.get("in_reply_to_status_id_str")),
        is_quote=bool(legacy.get("is_quote_status")),
        is_retweet="retweeted_status_result" in result,
    )


def _instructions(payload: dict) -> list[dict]:
    """The instruction list, wherever this response variant keeps it."""
    for path in (
        ("data", "search_by_raw_query", "search_timeline", "timeline", "instructions"),
        ("data", "search_by_raw_query", "search_timeline", "timeline_v2", "instructions"),
        ("data", "search", "timeline", "instructions"),
    ):
        found = _dig(payload, *path)
        if isinstance(found, list):
            return found
    return []


def parse_timeline(payload: dict) -> tuple[list[Tweet], str | None]:
    """All tweets in one SearchTimeline response, plus the bottom cursor.

    The cursor is returned for the caller's "is there more?" decision — X keeps
    handing out a bottom cursor even on an exhausted timeline, so its *absence*
    means the end but its presence proves nothing.
    """
    tweets: list[Tweet] = []
    cursor: str | None = None
    skipped = 0

    for instruction in _instructions(payload):
        if not isinstance(instruction, dict):
            continue
        entries = instruction.get("entries")
        # TimelineReplaceEntry carries a single entry instead of a list.
        if entries is None and instruction.get("entry"):
            entries = [instruction["entry"]]
        if not isinstance(entries, list):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("entryId", "")
            content = entry.get("content") or {}

            if entry_id.startswith("cursor-bottom") or content.get("cursorType") == "Bottom":
                cursor = content.get("value") or cursor
                continue

            if not entry_id.startswith("tweet-"):
                # Modules ("Who to follow", topic carousels) and top cursors.
                continue

            result = _dig(content, "itemContent", "tweet_results", "result")
            tweet = parse_tweet(result)
            if tweet is None:
                skipped += 1
                continue
            tweets.append(tweet)

    if skipped:
        LOGGER.debug("Skipped %d unparseable timeline entries", skipped)
    return tweets, cursor
