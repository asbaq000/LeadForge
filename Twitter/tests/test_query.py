"""Query packing.

The character limit is the important one. X answers an over-long query with an
empty timeline rather than an error, so breaking this looks exactly like "there
are no leads today" — measured on 2026-08-13: 446 chars returned results, 509
returned none.
"""

from datetime import datetime, timezone

import pytest

from x_leads.niches import load_niche
from x_leads.search.query import (
    MAX_QUERY_CHARS,
    build_queries,
    search_url,
    since_date,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def test_every_query_stays_under_the_limit():
    phrases = [f"looking for a video editor number {i}" for i in range(60)]
    for q in build_queries(phrases, now=NOW):
        assert len(q) <= MAX_QUERY_CHARS, f"{len(q)} chars: {q}"


def test_the_shipped_niche_fits():
    niche = load_niche("video_editing")
    queries = build_queries(
        niche.phrases, lang=niche.language,
        extra_operators=niche.extra_operators, now=NOW,
    )
    assert queries
    for q in queries:
        assert len(q) <= MAX_QUERY_CHARS


def test_packing_is_dense_not_one_per_phrase():
    """The whole point: 47 phrases must not become 47 searches."""
    phrases = [f"phrase number {i}" for i in range(40)]
    queries = build_queries(phrases, now=NOW)
    assert len(queries) < 8


def test_every_phrase_is_included_somewhere():
    phrases = ["hiring an editor", "need a videographer", "edit my videos"]
    joined = " ".join(build_queries(phrases, now=NOW))
    for p in phrases:
        assert f'"{p}"' in joined


def test_phrases_keep_their_order():
    phrases = [f"phrase {i:02d}" for i in range(30)]
    queries = build_queries(phrases, now=NOW)
    assert '"phrase 00"' in queries[0]
    assert '"phrase 29"' in queries[-1]


def test_filters_are_always_applied():
    q = build_queries(["hiring an editor"], now=NOW)[0]
    assert "-filter:replies" in q
    assert "-filter:retweets" in q


def test_language_and_since_are_included():
    q = build_queries(["hiring an editor"], lang="en", days=2, now=NOW)[0]
    assert "lang:en" in q
    assert "since:2026-08-10" in q


def test_language_can_be_turned_off():
    q = build_queries(["hiring an editor"], lang=None, now=NOW)[0]
    assert "lang:" not in q


def test_since_pads_by_a_day():
    """`since:` is date-granular, so 'last 24h' asked for today would see an hour."""
    assert since_date(1, now=NOW) == "2026-08-11"


def test_extra_operators_are_appended():
    q = build_queries(["hiring an editor"], extra_operators=("min_faves:2",), now=NOW)[0]
    assert "min_faves:2" in q


def test_quotes_inside_a_phrase_do_not_break_the_query():
    q = build_queries(['need a "good" editor'], now=NOW)[0]
    assert q.count('"') % 2 == 0
    assert '"need a good editor"' in q


def test_impossibly_long_phrase_is_dropped_not_emitted():
    """A query built around it would silently return nothing."""
    monster = "x" * (MAX_QUERY_CHARS + 50)
    assert build_queries([monster], now=NOW) == []


def test_a_long_phrase_does_not_take_good_ones_with_it():
    queries = build_queries(["x" * 900, "hiring an editor"], now=NOW)
    assert len(queries) == 1
    assert '"hiring an editor"' in queries[0]


def test_empty_input():
    assert build_queries([], now=NOW) == []


def test_blank_phrases_are_skipped():
    assert build_queries(["", "   "], now=NOW) == []


def test_search_url_defaults_to_latest():
    url = search_url("hiring an editor")
    assert "f=live" in url
    assert "hiring" in url


@pytest.mark.parametrize("count", [1, 5, 14, 47, 100])
def test_limit_holds_at_any_size(count):
    phrases = [f"looking for a really specific video editor {i}" for i in range(count)]
    for q in build_queries(phrases, now=NOW):
        assert len(q) <= MAX_QUERY_CHARS
