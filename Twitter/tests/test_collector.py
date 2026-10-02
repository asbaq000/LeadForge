"""Collector behaviour that does not need a browser.

The retry path is the one worth pinning. On a live run X rate-limited one
query, the collector swallowed it, and the run reported success with the single
most valuable search silently missing — a blocked timeline and an empty one
look identical from the outside.
"""

import asyncio

import pytest

from x_leads.errors import SearchBlocked
from x_leads.models import Tweet
from x_leads.search.collector import Collector


def make_collector(**kw):
    kw.setdefault("block_backoff_s", 0)  # no real sleeping in tests
    return Collector(storage_state={"cookies": []}, **kw)


def tweet(tid="1"):
    return Tweet(tweet_id=tid, text="hiring a video editor")


def test_a_blocked_query_is_retried_and_can_succeed():
    c = make_collector(block_retries=2)
    calls = []

    async def flaky(query):
        calls.append(query)
        if len(calls) < 3:
            raise SearchBlocked("rate limited")
        return [tweet()]

    c._attempt_query = flaky
    result = asyncio.run(c.run_query("q"))

    assert len(calls) == 3
    assert len(result) == 1
    assert c.stats.blocked_queries == 0


def test_giving_up_is_recorded_loudly_not_silently():
    c = make_collector(block_retries=1)

    async def always_blocked(query):
        raise SearchBlocked("rate limited")

    c._attempt_query = always_blocked
    result = asyncio.run(c.run_query("q"))

    assert result == []
    assert c.stats.blocked_queries == 1
    assert any("rate limited" in e for e in c.stats.errors)
    assert "BLOCKED" in c.stats.summary()


def test_other_errors_are_not_retried():
    c = make_collector(block_retries=3)
    calls = []

    async def broken(query):
        calls.append(query)
        raise RuntimeError("something else")

    c._attempt_query = broken
    with pytest.raises(RuntimeError):
        asyncio.run(c.run_query("q"))
    assert len(calls) == 1


def test_results_merge_and_deduplicate_across_queries():
    c = make_collector()

    async def fake(query):
        t = tweet("same")
        t.matched_queries.append(query)
        return [t]

    c.run_query = fake
    merged = asyncio.run(c.run_queries(["a", "b"]))

    assert len(merged) == 1
    assert sorted(merged[0].matched_queries) == ["a", "b"]
    assert c.stats.unique_tweets == 1


def test_one_failing_query_does_not_lose_the_others():
    c = make_collector()

    async def fake(query):
        if query == "bad":
            raise RuntimeError("boom")
        return [tweet(query)]

    c.run_query = fake
    merged = asyncio.run(c.run_queries(["good", "bad"]))

    assert [t.tweet_id for t in merged] == ["good"]
    assert c.stats.errors


def test_cutoff_follows_the_age_window():
    from datetime import datetime, timezone

    c = make_collector(max_age_hours=24)
    delta = datetime.now(timezone.utc) - c.cutoff
    assert 23.9 < delta.total_seconds() / 3600 < 24.1
