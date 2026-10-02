"""Cross-run dedupe, output rendering, session-file handling and niche loading."""

import json
import time

import pytest

from x_leads import output
from x_leads.auth.session import SessionManager
from x_leads.models import Author, Lead, Tweet
from x_leads.niches import available, load_niche
from x_leads.state import SeenStore


def make_lead(tweet_id="1", verdict="hot", score=12, text="Hiring a video editor"):
    return Lead(
        tweet=Tweet(
            tweet_id=tweet_id, text=text,
            author=Author(handle="buyer", followers=1000, bio="youtuber"),
        ),
        score=score, verdict=verdict, signals=["demand:hiring"],
    )


# -------------------------------------------------------------- seen store
def test_seen_store_round_trips(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    store.add("123")
    store.save()

    assert "123" in SeenStore(tmp_path / "seen.json")


def test_only_new_filters_previously_reported(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    store.add("old")
    leads = [make_lead("old"), make_lead("new")]
    assert [l.tweet.tweet_id for l in store.filter_new(leads)] == ["new"]


def test_missing_store_is_empty_not_an_error(tmp_path):
    assert len(SeenStore(tmp_path / "nope.json")) == 0


def test_corrupt_store_is_ignored_not_fatal(tmp_path):
    """A broken cache should cost a duplicate lead, never a run."""
    path = tmp_path / "seen.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(SeenStore(path)) == 0


def test_entries_expire(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({
        "seen": {"ancient": time.time() - 200 * 86400, "recent": time.time()}
    }), encoding="utf-8")
    store = SeenStore(path, retention_days=90)
    assert "recent" in store
    assert "ancient" not in store


def test_add_all_counts_only_new_ids(tmp_path):
    store = SeenStore(tmp_path / "s.json")
    assert store.add_all(["a", "b"]) == 2
    assert store.add_all(["b", "c"]) == 1


def test_clear(tmp_path):
    store = SeenStore(tmp_path / "s.json")
    store.add("x")
    store.save()
    store.clear()
    assert len(store) == 0
    assert not (tmp_path / "s.json").exists()


def test_save_is_atomic(tmp_path):
    """A half-written store must not be left where the next run reads it."""
    path = tmp_path / "seen.json"
    store = SeenStore(path)
    store.add("a")
    store.save()
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()


# ------------------------------------------------------------------ output
def test_json_round_trips():
    parsed = json.loads(output.to_json([make_lead()]))
    assert parsed[0]["verdict"] == "hot"
    assert parsed[0]["tweet_url"] == "https://x.com/buyer/status/1"


def test_jsonl_is_one_object_per_line():
    text = output.to_jsonl([make_lead("1"), make_lead("2")])
    lines = text.splitlines()
    assert len(lines) == 2
    assert all(json.loads(line) for line in lines)


def test_csv_has_a_header_and_flattens_signals():
    text = output.to_csv([make_lead()])
    assert "tweet_url" in text.splitlines()[0]
    assert "demand:hiring" in text


def test_csv_strips_newlines_from_text():
    """Embedded newlines split a row across lines in some spreadsheet importers."""
    lead = make_lead(text="Hiring an editor\n\nDrop your reel")
    body = output.to_csv([lead]).splitlines()
    assert len(body) == 2


def test_digest_leads_with_the_url():
    text = output.to_digest([make_lead()])
    assert "https://x.com/buyer/status/1" in text
    assert "HOT" in text


def test_empty_digest_suggests_what_to_try():
    text = output.to_digest([])
    assert "--hours" in text


def test_render_dispatch():
    for fmt in ("json", "jsonl", "csv", "digest"):
        assert output.render([make_lead()], fmt)
    with pytest.raises(ValueError):
        output.render([], "xml")


def test_default_filename_has_an_extension():
    assert output.default_filename("csv", "video").endswith(".csv")
    assert output.default_filename("digest").endswith(".txt")


# ----------------------------------------------------------------- niches
def test_shipped_niche_loads():
    niche = load_niche("video_editing")
    assert niche.phrases
    assert niche.language == "en"


def test_comment_keys_are_ignored():
    """The presets document themselves in _comment_* keys; JSON has no comments."""
    niche = load_niche("video_editing")
    assert all(not p.startswith("_comment") for p in niche.phrases)


def test_video_editing_is_listed():
    assert "video_editing" in available()


def test_unknown_niche_names_the_alternatives():
    with pytest.raises(FileNotFoundError, match="video_editing"):
        load_niche("nonexistent")


def test_niche_from_an_arbitrary_path(tmp_path):
    path = tmp_path / "custom.json"
    path.write_text(json.dumps({"name": "custom", "phrases": ["need an editor"]}),
                    encoding="utf-8")
    assert load_niche(str(path)).phrases == ["need an editor"]


def test_niche_without_phrases_is_rejected(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"name": "e", "phrases": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_niche(str(path))


# ---------------------------------------------------------------- session
def test_session_needs_an_auth_cookie(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"cookies": [{"name": "ct0", "domain": ".x.com"}]}),
                    encoding="utf-8")
    # ct0 is handed to logged-out visitors too, so it proves nothing.
    assert SessionManager(path).load() is None


def test_session_with_auth_cookie_loads(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"cookies": [
        {"name": "auth_token", "domain": ".x.com", "expires": time.time() + 86400}
    ]}), encoding="utf-8")
    assert SessionManager(path).load() is not None


def test_expired_auth_cookie_is_rejected(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"cookies": [
        {"name": "auth_token", "domain": ".x.com", "expires": time.time() - 10}
    ]}), encoding="utf-8")
    assert SessionManager(path).load() is None


def test_session_cookie_without_expiry_is_accepted(tmp_path):
    """Playwright reports -1 for session cookies and still replays them."""
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"cookies": [
        {"name": "auth_token", "domain": ".x.com", "expires": -1}
    ]}), encoding="utf-8")
    assert SessionManager(path).load() is not None


def test_twitter_com_domain_counts(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"cookies": [
        {"name": "auth_token", "domain": ".twitter.com", "expires": -1}
    ]}), encoding="utf-8")
    assert SessionManager(path).load() is not None


def test_unreadable_session_is_none_not_a_crash(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("garbage", encoding="utf-8")
    assert SessionManager(path).load() is None


def test_missing_session_file(tmp_path):
    assert SessionManager(tmp_path / "none.json").load() is None
    assert SessionManager(tmp_path / "none.json").exists() is False


# --------------------------------------------- seen-filtering is the default
def _run(config_kw, seen_store, tweets):
    """Drive the post-collection half of the scraper without a browser."""
    import asyncio
    from unittest.mock import patch

    from x_leads.scraper import ScrapeConfig, XLeadScraper

    scraper = XLeadScraper(ScrapeConfig(**config_kw), seen=seen_store)
    with patch("x_leads.scraper.ensure_session", return_value={}), \
         patch("x_leads.scraper.Collector") as collector:
        instance = collector.return_value.__aenter__.return_value
        async def run_queries(_):
            return tweets
        instance.run_queries = run_queries
        from x_leads.search.collector import CollectStats
        instance.stats = CollectStats()
        return asyncio.run(scraper.run())


def _hiring_tweet(tid):
    return Tweet(
        tweet_id=tid,
        text="Hiring a video editor for my channel! $500 per video. "
             "Drop your portfolio below.",
        author=Author(handle="buyer", followers=900),
    )


def test_already_reported_leads_are_skipped_by_default(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    tweets = [_hiring_tweet("a")]

    first = _run({}, store, tweets)
    assert len(first.leads) == 1

    second = _run({}, store, tweets)
    assert second.leads == []
    assert second.suppressed_as_seen == 1


def test_include_seen_brings_them_back(tmp_path):
    store = SeenStore(tmp_path / "seen.json")
    tweets = [_hiring_tweet("a")]

    _run({}, store, tweets)
    again = _run({"skip_seen": False}, store, tweets)
    assert len(again.leads) == 1
    assert again.suppressed_as_seen == 0


def test_min_verdict_does_not_bury_the_leads_it_hid(tmp_path):
    """A warm-only run must not mark that day's cold leads as reported.

    They were never shown, so they must still be new to a later cold run.
    """
    store = SeenStore(tmp_path / "seen.json")
    cold = Tweet(
        tweet_id="cold-1",
        text="looking for an editor",
        author=Author(handle="someone", followers=50),
    )
    hot = _hiring_tweet("hot-1")

    warm_run = _run({"min_verdict": "warm"}, store, [cold, hot])
    assert [l.tweet.tweet_id for l in warm_run.leads] == ["hot-1"]

    cold_run = _run({"min_verdict": "cold"}, store, [cold, hot])
    assert "cold-1" in [l.tweet.tweet_id for l in cold_run.leads]
