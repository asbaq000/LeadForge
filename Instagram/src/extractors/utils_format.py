"""
Small formatting / normalization helpers shared by the parser and exporters.

Nothing in here touches the network — pure functions only, so they're easy
to reason about and to test.
"""

import re

_PROFILE_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?instagram\.com/([^/?#]+)", re.IGNORECASE
)


def normalize_username(raw: str) -> str:
    """
    Accept whatever the user pasted and return a bare username.

        'instagram'                              -> 'instagram'
        '@natgeo'                                -> 'natgeo'
        'https://www.instagram.com/nasa/?hl=en'  -> 'nasa'
    """
    value = (raw or "").strip()
    if not value:
        return ""

    match = _PROFILE_URL_RE.match(value)
    if match:
        value = match.group(1)

    return value.lstrip("@").strip("/").strip().lower()


def read_usernames(path: str):
    """Read an input file of usernames, skipping blanks and '#' comments."""
    usernames = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            username = normalize_username(line)
            if username and username not in seen:
                seen.add(username)
                usernames.append(username)
    return usernames


def safe_int(value, default: int = 0) -> int:
    """Instagram sometimes hands back None or a string where a count belongs."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_str(value, default: str = "") -> str:
    return value if isinstance(value, str) else default


def dig(data, *keys, default=None):
    """
    Walk a chain of dict keys without a pile of nested .get() calls.

        dig(payload, 'data', 'user', 'edge_owner_to_timeline_media', 'count')
    """
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current
