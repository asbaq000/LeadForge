"""Turning a niche's intent phrases into as few X searches as possible.

This is the main reason the scraper got fast. The old version ran 20 broad
keywords ("youtube", "tiktok", "influencer") across two result modes, which is
40 searches returning mostly people chatting about video. Here, intent phrases
are packed into OR groups and run once each against Latest.

Two limits were measured against live X on 2026-08-13 rather than assumed:

* **512 characters.** A query is silently answered with *zero* results once it
  exceeds roughly that — not an error, not a warning, just an empty timeline.
  14 phrases (446 chars) worked and 16 (509 chars) returned nothing, so
  `MAX_QUERY_CHARS` leaves real headroom below the cliff. Getting this wrong
  looks exactly like "nobody is hiring today", which is why it is enforced
  here and asserted in the tests.
* **Latest is strictly reverse-chronological**, and `since:` is honoured
  exactly. Together those let the collector stop as soon as it reaches the age
  cutoff instead of scrolling to a fixed depth.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Below X's ~512-char cliff with room for the suffix operators.
MAX_QUERY_CHARS = 460

# Applied to every search:
#   -filter:replies  a reply is a conversation, not a brief. Roughly a third of
#                    raw matches were replies, and almost none were leads.
#   -filter:retweets a retweet is someone else's ask, already seen.
#   lang:            keeps the timeline in a language the classifier can read.
BASE_FILTERS = ("-filter:replies", "-filter:retweets")


def _quote(phrase: str) -> str:
    """Exact-phrase form. Embedded quotes would break the query, so they go."""
    cleaned = phrase.replace('"', "").strip()
    return f'"{cleaned}"'


def _suffix(lang: str | None, since: str | None, extra: tuple[str, ...]) -> str:
    parts = list(BASE_FILTERS)
    if lang:
        parts.append(f"lang:{lang}")
    if since:
        parts.append(f"since:{since}")
    parts.extend(extra)
    return " ".join(parts)


def since_date(days: int, now: datetime | None = None) -> str:
    """X's `since:` takes a UTC date.

    One day is subtracted on top of `days` because `since:` is date-granular:
    asking for "the last 24 hours" at 01:00 UTC with today's date would only
    ever see one hour of posts.
    """
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days + 1)).strftime("%Y-%m-%d")


def build_queries(
    phrases: list[str],
    lang: str | None = "en",
    days: int = 2,
    extra_operators: tuple[str, ...] = (),
    max_chars: int = MAX_QUERY_CHARS,
    now: datetime | None = None,
) -> list[str]:
    """Pack `phrases` into the fewest queries that stay under the char limit.

    Greedy first-fit. Phrase order is preserved so a niche file can put its
    highest-yield phrases first and have them share the first query — which
    matters when a run is cut short by `--max-queries`.

    A phrase too long to ever fit is dropped rather than emitted as a query
    that would silently return nothing.
    """
    suffix = _suffix(lang, since_date(days, now) if days else None, extra_operators)
    # "(" + ") " + suffix
    overhead = len(suffix) + 3

    queries: list[str] = []
    batch: list[str] = []
    batch_len = 0

    for phrase in phrases:
        quoted = _quote(phrase)
        if not quoted.strip('"'):
            continue
        # " OR " between phrases
        addition = len(quoted) + (4 if batch else 0)

        if len(quoted) + overhead > max_chars:
            # Nothing can be done with this one; a query built around it would
            # return zero results and look like a dry search.
            continue

        if batch and batch_len + addition + overhead > max_chars:
            queries.append(f"({' OR '.join(batch)}) {suffix}")
            batch, batch_len = [], 0
            addition = len(quoted)

        batch.append(quoted)
        batch_len += addition

    if batch:
        queries.append(f"({' OR '.join(batch)}) {suffix}")

    return queries


def search_url(query: str, mode: str = "live") -> str:
    """A search URL.

    `live` (Latest) is the default and what the collector always uses: `top` is
    engagement-ranked, so it favours viral commentary about video editing over
    the small, zero-like post from someone who actually needs an editor. It
    also overlaps heavily with `live`, which is why running both — as the old
    scraper did — roughly doubled the time for almost no new leads.
    """
    from urllib.parse import quote

    return f"https://x.com/search?q={quote(query)}&src=typed_query&f={mode}"
