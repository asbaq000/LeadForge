"""Turn raw Threads payloads into a flat, stable post record.

Two extraction paths, used together:

1. GraphQL interception (primary) - Threads renders from JSON that follows the
   Instagram media schema. Far richer and far less brittle than reading the DOM.
2. DOM scrape (fallback) - if the JSON shape changes or a response is missed,
   we still recover username / permalink / text from the rendered cards.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

BASE = "https://www.threads.com"

# Keys that identify an Instagram/Threads "media" object.
_TEXT_KEYS = ("caption", "text_post_app_info")


def _as_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def looks_like_post(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if not (node.get("pk") or node.get("id") or node.get("code")):
        return False
    user = node.get("user")
    if not isinstance(user, dict) or not user.get("username"):
        return False
    return any(k in node for k in _TEXT_KEYS) or "code" in node


def walk_posts(node: Any, _depth: int = 0, _seen: set[int] | None = None) -> Iterator[dict]:
    """Depth-first search for post-shaped dicts anywhere in a JSON blob."""
    if _depth > 24:
        return
    if _seen is None:
        _seen = set()

    if isinstance(node, dict):
        marker = id(node)
        if marker in _seen:
            return
        _seen.add(marker)
        if looks_like_post(node):
            yield node
        for value in node.values():
            yield from walk_posts(value, _depth + 1, _seen)
    elif isinstance(node, list):
        for item in node:
            yield from walk_posts(item, _depth + 1, _seen)


def _caption_text(node: dict) -> str:
    cap = node.get("caption")
    if isinstance(cap, dict) and cap.get("text"):
        return str(cap["text"])
    # Some responses put the body straight on the node.
    for key in ("text", "body_text"):
        if isinstance(node.get(key), str) and node[key].strip():
            return node[key]
    return ""


def _link_attachment(node: dict) -> str:
    tpa = node.get("text_post_app_info")
    if isinstance(tpa, dict):
        lp = tpa.get("link_preview_attachment")
        if isinstance(lp, dict):
            return str(lp.get("url") or lp.get("display_url") or "")
    return ""


def normalize_graphql_post(node: dict, matched_query: str = "") -> dict | None:
    user = node.get("user") or {}
    username = str(user.get("username") or "").strip()
    if not username:
        return None

    text = _caption_text(node).strip()
    code = str(node.get("code") or "").strip()
    post_id = str(node.get("pk") or node.get("id") or code).strip()
    if not post_id:
        return None

    tpa = node.get("text_post_app_info") or {}
    permalink = f"{BASE}/@{username}/post/{code}" if code else f"{BASE}/@{username}"

    return {
        "post_id": post_id,
        "code": code,
        "permalink": permalink,
        "username": username,
        "full_name": str(user.get("full_name") or ""),
        "is_verified": bool(user.get("is_verified")),
        "user_id": str(user.get("pk") or user.get("id") or ""),
        "followers": _as_int(user.get("follower_count")),
        "bio": str(user.get("biography") or ""),
        "text": text,
        "taken_at": _as_int(node.get("taken_at")),
        "like_count": _as_int(node.get("like_count")),
        "reply_count": _as_int(tpa.get("direct_reply_count") or node.get("reply_count")),
        "repost_count": _as_int(tpa.get("repost_count")),
        "quote_count": _as_int(tpa.get("quote_count")),
        "link_attachment": _link_attachment(node),
        "matched_query": matched_query,
        "source": "graphql",
    }


def normalize_dom_post(card: dict, matched_query: str = "") -> dict | None:
    """`card` comes from the JS extractor in collector.py."""
    username = str(card.get("username") or "").strip().lstrip("@")
    code = str(card.get("code") or "").strip()
    text = str(card.get("text") or "").strip()
    if not username or not text:
        return None

    post_id = code or f"{username}:{abs(hash(text)) % (10 ** 12)}"
    permalink = card.get("permalink") or (
        f"{BASE}/@{username}/post/{code}" if code else f"{BASE}/@{username}"
    )

    return {
        "post_id": post_id,
        "code": code,
        "permalink": permalink,
        "username": username,
        "full_name": str(card.get("full_name") or ""),
        "is_verified": bool(card.get("is_verified")),
        "user_id": "",
        "followers": 0,
        "bio": "",
        "text": text,
        "taken_at": _as_int(card.get("taken_at")),
        "like_count": _as_int(card.get("like_count")),
        "reply_count": _as_int(card.get("reply_count")),
        "repost_count": 0,
        "quote_count": 0,
        "link_attachment": "",
        "matched_query": matched_query,
        "source": "dom",
    }


def post_age_hours(taken_at: int) -> float | None:
    """Hours since posting, or None when the timestamp is unknown."""
    if not taken_at:
        return None
    try:
        posted = datetime.fromtimestamp(taken_at, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return (datetime.now(timezone.utc) - posted).total_seconds() / 3600.0


def iso_posted_at(taken_at: int) -> str:
    if not taken_at:
        return ""
    try:
        return datetime.fromtimestamp(taken_at, tz=timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""
