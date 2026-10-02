"""Parsing X's GraphQL payload.

Fixtures follow the shape captured live on 2026-08-13, including the detail
that broke the first version: the author's `legacy` dict is now *empty* and the
real fields live in `core`, `profile_bio` and `relationship_counts`. Code that
trusted `legacy` produced blank authors rather than an error, so both shapes
are pinned here.
"""

from x_leads.search.parser import (
    parse_author,
    parse_time,
    parse_timeline,
    parse_tweet,
)


def user_current(**over):
    """The 2026 shape."""
    base = {
        "__typename": "User",
        "rest_id": "111",
        "core": {"name": "FSG", "screen_name": "FSGLoL"},
        "profile_bio": {"description": "video editor 8M+ views"},
        "relationship_counts": {"followers": 96, "following": 462},
        "tweet_counts": {"tweets": 3532},
        "location": {"location": "Portfolio"},
        "verification": {"verified": False},
        "is_blue_verified": True,
        "professional": {"category": [{"id": 1062, "name": "Editor"}]},
        "website": {"url": "https://t.co/abc"},
        "legacy": {},
    }
    base.update(over)
    return base


def user_legacy(**over):
    """The older shape, still served in places."""
    base = {
        "__typename": "User",
        "rest_id": "222",
        "legacy": {
            "screen_name": "oldstyle",
            "name": "Old Style",
            "description": "youtuber and podcaster",
            "followers_count": 4200,
            "friends_count": 100,
            "statuses_count": 900,
            "verified": True,
            "location": "London",
            "url": "https://t.co/xyz",
        },
    }
    base.update(over)
    return base


def tweet_result(text="hiring a video editor", user=None, **legacy_over):
    legacy = {
        "id_str": "999",
        "full_text": text,
        "created_at": "Thu Aug 13 02:51:33 +0000 2026",
        "lang": "en",
        "favorite_count": 3,
        "reply_count": 1,
        "retweet_count": 2,
        "quote_count": 0,
        "bookmark_count": 5,
    }
    legacy.update(legacy_over)
    return {
        "__typename": "Tweet",
        "rest_id": "999",
        "core": {"user_results": {"result": user or user_current()}},
        "legacy": legacy,
        "views": {"count": "25"},
    }


def timeline(entries):
    return {
        "data": {"search_by_raw_query": {"search_timeline": {"timeline": {
            "instructions": [{"type": "TimelineAddEntries", "entries": entries}]
        }}}}
    }


def entry(result, entry_id="tweet-999"):
    return {
        "entryId": entry_id,
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": result}},
        },
    }


# ------------------------------------------------------------------- authors
def test_current_user_shape():
    a = parse_author(user_current())
    assert a.handle == "FSGLoL"
    assert a.display_name == "FSG"
    assert a.bio == "video editor 8M+ views"
    assert a.followers == 96
    assert a.tweets == 3532
    assert a.blue_verified is True
    assert a.professional_category == "Editor"
    assert a.location == "Portfolio"


def test_legacy_user_shape_still_parses():
    a = parse_author(user_legacy())
    assert a.handle == "oldstyle"
    assert a.followers == 4200
    assert a.bio == "youtuber and podcaster"
    assert a.verified is True


def test_empty_legacy_does_not_blank_the_author():
    """The exact regression: `legacy` present but empty, real data in `core`."""
    a = parse_author(user_current(legacy={}))
    assert a.handle == "FSGLoL"
    assert a.followers == 96


def test_missing_user_is_an_empty_author_not_a_crash():
    a = parse_author(None)
    assert a.handle == ""
    assert a.followers == 0


def test_profile_url():
    assert parse_author(user_current()).profile_url == "https://x.com/FSGLoL"


# -------------------------------------------------------------------- tweets
def test_basic_tweet():
    t = parse_tweet(tweet_result())
    assert t.tweet_id == "999"
    assert t.text == "hiring a video editor"
    assert t.likes == 3
    assert t.views == 25
    assert t.lang == "en"
    assert t.url == "https://x.com/FSGLoL/status/999"


def test_note_tweet_wins_over_truncated_full_text():
    """Long job posts keep their real body in note_tweet; full_text is cut."""
    result = tweet_result(text="Hiring a video editor. Requirements: exp…")
    result["note_tweet"] = {
        "note_tweet_results": {"result": {"text": "A" * 900}}
    }
    t = parse_tweet(result)
    assert len(t.text) == 900


def test_visibility_wrapper_is_unwrapped():
    """These are ordinary posts; missing the wrapper drops them entirely."""
    wrapped = {
        "__typename": "TweetWithVisibilityResults",
        "tweet": tweet_result(text="need a video editor"),
    }
    t = parse_tweet(wrapped)
    assert t is not None
    assert t.text == "need a video editor"


def test_tweet_without_an_id_is_skipped():
    assert parse_tweet({"__typename": "Tweet", "legacy": {}}) is None


def test_tombstone_is_skipped():
    assert parse_tweet({"__typename": "TweetTombstone"}) is None
    assert parse_tweet(None) is None


def test_reply_and_quote_flags():
    t = parse_tweet(tweet_result(in_reply_to_status_id_str="5", is_quote_status=True))
    assert t.is_reply and t.is_quote


def test_created_at_parses_to_utc():
    dt = parse_time("Thu Aug 13 02:51:33 +0000 2026")
    assert dt.year == 2026 and dt.month == 8 and dt.day == 13


def test_bad_timestamp_is_none_not_an_exception():
    assert parse_time("not a date") is None
    assert parse_time(None) is None


# ------------------------------------------------------------------ timeline
def test_timeline_returns_tweets_and_cursor():
    payload = timeline([
        entry(tweet_result()),
        {"entryId": "cursor-bottom-0",
         "content": {"cursorType": "Bottom", "value": "CURSOR"}},
    ])
    tweets, cursor = parse_timeline(payload)
    assert len(tweets) == 1
    assert cursor == "CURSOR"


def test_non_tweet_entries_are_ignored():
    payload = timeline([
        entry(tweet_result()),
        {"entryId": "who-to-follow-1", "content": {"entryType": "TimelineTimelineModule"}},
        {"entryId": "cursor-top-1", "content": {"cursorType": "Top", "value": "T"}},
    ])
    tweets, _ = parse_timeline(payload)
    assert len(tweets) == 1


def test_multiple_instructions_are_all_read():
    payload = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
        "instructions": [
            {"type": "TimelineClearCache"},
            {"type": "TimelineAddEntries", "entries": [entry(tweet_result())]},
        ]
    }}}}}
    tweets, _ = parse_timeline(payload)
    assert len(tweets) == 1


def test_unknown_payload_shape_returns_empty_not_an_error():
    assert parse_timeline({}) == ([], None)
    assert parse_timeline({"data": {"nonsense": 1}}) == ([], None)


def test_one_bad_entry_does_not_lose_the_good_ones():
    payload = timeline([
        entry({"__typename": "Tweet"}, entry_id="tweet-broken"),
        entry(tweet_result()),
    ])
    tweets, _ = parse_timeline(payload)
    assert len(tweets) == 1
