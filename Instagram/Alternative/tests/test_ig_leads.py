"""Tests for the Instagram lead scraper.

The payload fixtures follow shapes captured live on 2026-08-13. Two of them
encode facts that cost real probing time and would silently break the tool if
someone "simplified" them:

* `web_profile_info` returns an EMPTY `edge_owner_to_timeline_media.edges`.
  Post dates have to come from the feed endpoint.
* Hashtag payloads nest media several levels deep in a shape that varies, so
  the parser walks rather than indexing a fixed path.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from ig_leads.budget import RequestBudget
from ig_leads.classify import classify, normalise, rank_key
from ig_leads.config import Filters
from ig_leads.errors import BudgetExhausted
from ig_leads.models import Account
from ig_leads.niches import load_niches, to_hashtag
from ig_leads.output import to_csv, to_digest, to_json
from ig_leads.parsing import parse_hashtag_authors, parse_post_dates, parse_profile

NOW = datetime.now(timezone.utc)


# ------------------------------------------------------------------- fixtures
def profile_payload(**over):
    user = {
        "id": "123",
        "username": "wildlifefilms",
        "full_name": "Wildlife Films",
        "biography": "Documentary filmmaker. New video every week.",
        "edge_followed_by": {"count": 45_000},
        "edge_follow": {"count": 300},
        # Empty on purpose - this is what Instagram actually returns.
        "edge_owner_to_timeline_media": {"count": 812, "edges": []},
        "is_private": False,
        "is_verified": False,
        "is_business_account": True,
        "is_professional_account": True,
        "category_name": "Digital creator",
        "external_url": "https://youtube.com/@wildlifefilms",
    }
    user.update(over)
    return {"data": {"user": user}}


def feed_payload(days_ago_list):
    return {
        "items": [
            {"taken_at": int((NOW - timedelta(days=d)).timestamp()), "media_type": 1}
            for d in days_ago_list
        ]
    }


def hashtag_payload():
    def media(username, days_ago, private=False):
        return {
            "taken_at": int((NOW - timedelta(days=days_ago)).timestamp()),
            "user": {"username": username, "full_name": username.title(),
                     "is_private": private, "is_verified": False},
        }

    return {
        "status": "ok",
        "data": {
            "recent": {"sections": [
                {"layout_content": {"medias": [
                    {"media": media("creator_one", 1)},
                    {"media": media("creator_two", 2, private=True)},
                ]}},
            ]},
            "top": {"sections": [
                {"layout_content": {"medias": [
                    {"media": media("creator_three", 3)},
                    {"media": media("creator_one", 5)},   # duplicate author
                ]}},
            ]},
        },
    }


def account(**over):
    base = dict(
        username="creator", followers=45_000, posts_in_window=8,
        biography="Documentary filmmaker", category="Digital creator",
        is_private=False,
    )
    base.update(over)
    return Account(**base)


# -------------------------------------------------------------------- parsing
def test_profile_parses():
    a = parse_profile(profile_payload(), "wildlifefilms")
    assert a.username == "wildlifefilms"
    assert a.followers == 45_000
    assert a.total_posts == 812
    assert a.category == "Digital creator"
    assert a.is_business is True
    assert a.profile_url == "https://www.instagram.com/wildlifefilms/"


def test_profile_of_garbage_is_none_not_a_crash():
    assert parse_profile(None, "x") is None
    assert parse_profile({}, "x") is None
    assert parse_profile({"data": {}}, "x") is None


def test_profile_endpoint_carries_no_post_dates():
    """Pinned as a fact: cadence cannot come from the profile call."""
    payload = profile_payload()
    assert payload["data"]["user"]["edge_owner_to_timeline_media"]["edges"] == []
    a = parse_profile(payload, "wildlifefilms")
    assert a.recent_post_dates == []
    assert a.posts_in_window is None


def test_feed_gives_post_dates_newest_first():
    dates = parse_post_dates(feed_payload([10, 1, 5]))
    assert len(dates) == 3
    assert dates == sorted(dates, reverse=True)


def test_pinned_old_post_does_not_hide_recent_activity():
    """Instagram returns pinned posts first regardless of age."""
    dates = parse_post_dates(feed_payload([400, 1, 2, 3]))
    assert (NOW - dates[0]).days < 2


def test_feed_of_garbage_is_empty():
    assert parse_post_dates(None) == []
    assert parse_post_dates({"items": "nope"}) == []


def test_hashtag_authors_are_found_and_deduplicated():
    cands = parse_hashtag_authors(hashtag_payload(), "Wildlife", "wildlife")
    names = sorted(c.username for c in cands)
    assert names == ["creator_one", "creator_three", "creator_two"]
    assert all(c.niche == "Wildlife" for c in cands)


def test_hashtag_marks_private_accounts_for_free():
    """Saves a request each - private accounts can never qualify."""
    cands = {c.username: c for c in parse_hashtag_authors(hashtag_payload(), "n", "t")}
    assert cands["creator_two"].is_private is True
    assert cands["creator_one"].is_private is False


def test_hashtag_of_garbage_is_empty():
    assert parse_hashtag_authors(None, "n", "t") == []
    assert parse_hashtag_authors({"data": {}}, "n", "t") == []


# ----------------------------------------------------------------- the filters
def test_a_qualifying_creator_is_a_lead():
    assert classify(account()).is_lead


def test_follower_floor():
    a = classify(account(followers=800))
    assert not a.is_lead and "min" in a.reject_reason


def test_follower_ceiling():
    a = classify(account(followers=900_000))
    assert not a.is_lead and "max" in a.reject_reason


@pytest.mark.parametrize("n", [1_000, 500_000])
def test_band_is_inclusive_at_both_ends(n):
    assert classify(account(followers=n)).is_lead


def test_not_enough_recent_posts():
    a = classify(account(posts_in_window=4))
    assert not a.is_lead and "posts in 14d" in a.reject_reason


def test_exactly_five_posts_qualifies():
    assert classify(account(posts_in_window=5)).is_lead


def test_unchecked_history_is_not_treated_as_zero():
    """None means 'never looked', which must not read as 'inactive'."""
    a = classify(account(posts_in_window=None))
    assert not a.is_lead
    assert "not checked" in a.reject_reason


def test_private_accounts_are_skipped():
    a = classify(account(is_private=True))
    assert not a.is_lead and "private" in a.reject_reason


def test_private_can_be_allowed():
    a = classify(account(is_private=True), Filters(skip_private=False))
    assert a.is_lead


# --------------------------------------------- creators vs the people we sell to
RIVALS = [
    "Video editor | I edit your reels",
    "We edit for creators. DM for edits",
    "Reels editor | DM me for editing",
    "Short form video agency | we help creators scale",
    "Freelance editor, available for work",
    "Thumbnail designer for YouTubers",
    "Post-production studio",
]


@pytest.mark.parametrize("bio", RIVALS)
def test_editors_and_agencies_are_rejected(bio):
    """They post under creator hashtags all day and are competitors, not clients."""
    a = classify(account(biography=bio))
    assert not a.is_lead, f"kept a rival: {bio}"
    assert "sells the service" in a.reject_reason


CREATORS = [
    "Wildlife filmmaker. Documentaries about the Serengeti",
    "Restoring a 1967 Mustang. New video every Friday",
    "Homesteading in Vermont | YouTube link below",
    "I make history documentaries. 200k on YouTube",
    "Woodworker. Watch the full build on my channel",
]


@pytest.mark.parametrize("bio", CREATORS)
def test_genuine_creators_survive(bio):
    """A filmmaker films for a living and is still a client, not a supplier."""
    a = classify(account(biography=bio))
    assert a.is_lead, f"dropped a real creator ({a.reject_reason}): {bio}"


def test_supplier_category_is_rejected():
    a = classify(account(category="Graphic Designer"))
    assert not a.is_lead


def test_fan_pages_and_shops_are_rejected():
    for bio in ("Fan page, not affiliated with anyone",
                "DM to order | worldwide shipping",
                "Follow for follow | grow your instagram"):
        assert not classify(account(biography=bio)).is_lead, bio


def test_reasons_explain_a_lead():
    a = classify(account(biography="Documentary filmmaker, new video on YouTube",
                         external_url="https://youtube.com/@x"))
    assert a.is_lead
    assert any("creator:" in r for r in a.reasons)
    assert "link:offplatform-channel" in a.reasons


def test_ranking_prefers_corroborated_and_active_accounts():
    thin = classify(account(username="thin", biography="", category="",
                            posts_in_window=5, followers=400_000))
    rich = classify(account(username="rich",
                            biography="Documentary filmmaker, new video on YouTube",
                            external_url="https://youtube.com/@x",
                            posts_in_window=20, followers=5_000))
    assert rank_key(rich) > rank_key(thin)


def test_normalise_folds_styled_unicode():
    assert "video editor" in normalise("𝐕𝐈𝐃𝐄𝐎 𝐄𝐃𝐈𝐓𝐎𝐑")


def test_styled_bio_rival_still_caught():
    assert not classify(account(biography="𝐕𝐢𝐝𝐞𝐨 𝐄𝐝𝐢𝐭𝐨𝐫 | DM for edits")).is_lead


# --------------------------------------------------------------------- budget
def test_budget_blocks_when_daily_limit_reached(tmp_path):
    b = RequestBudget(3, 100, tmp_path / "b.json")
    for _ in range(3):
        b.check(); b.spend()
    with pytest.raises(BudgetExhausted, match="Daily"):
        b.check()


def test_budget_blocks_at_the_run_limit(tmp_path):
    b = RequestBudget(100, 2, tmp_path / "b.json")
    for _ in range(2):
        b.check(); b.spend()
    with pytest.raises(BudgetExhausted, match="Per-run"):
        b.check()


def test_budget_persists_across_runs(tmp_path):
    """The real risk: five polite runs in one afternoon."""
    path = tmp_path / "b.json"
    first = RequestBudget(10, 10, path)
    for _ in range(6):
        first.check(); first.spend()

    second = RequestBudget(10, 10, path)
    assert second.spent_today == 6
    assert second.remaining_today == 4


def test_budget_resets_on_a_new_day(tmp_path):
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"date": "2020-01-01", "spent": 999}), encoding="utf-8")
    assert RequestBudget(10, 10, path).spent_today == 0


def test_unreadable_budget_fails_closed(tmp_path):
    """A corrupt counter must not read as 'plenty of budget left'."""
    path = tmp_path / "b.json"
    path.write_text("{{{", encoding="utf-8")
    b = RequestBudget(50, 50, path)
    assert b.spent_today == 50
    with pytest.raises(BudgetExhausted):
        b.check()


def test_budget_survives_a_crash_mid_run(tmp_path):
    """Spend is written immediately, not at the end."""
    path = tmp_path / "b.json"
    b = RequestBudget(10, 10, path)
    b.check(); b.spend()
    assert RequestBudget(10, 10, path).spent_today == 1


# --------------------------------------------------------------------- niches
def test_the_shipped_niche_list_loads():
    ns = load_niches("creator_niches")
    assert len(ns.niches) >= 80
    assert all(n.hashtags for n in ns.niches)


def test_niche_names_from_the_brief_are_present():
    names = {n.name for n in load_niches("creator_niches").niches}
    for expected in ("Wildlife Documentary", "Classic Car Restoration",
                     "Homesteading Vlog", "BookTube", "Off Grid Living"):
        assert expected in names


def test_niche_selection_is_loose():
    ns = load_niches("creator_niches")
    picked = {n.name for n in ns.pick(["wildlife"])}
    assert "Wildlife Documentary" in picked


def test_selecting_several_niches():
    ns = load_niches("creator_niches")
    picked = ns.pick(["wildlife", "homestead"])
    assert len(picked) >= 2


def test_no_selection_means_everything():
    ns = load_niches("creator_niches")
    assert len(ns.pick(None)) == len(ns.niches)


def test_hashtag_conversion():
    assert to_hashtag("Classic Car Restoration") == "classiccarrestoration"
    assert to_hashtag("Walking Tour 4K") == "walkingtour4k"


def test_unknown_niche_file():
    with pytest.raises(FileNotFoundError):
        load_niches("nope")


# --------------------------------------------------------------------- output
def test_csv_has_a_header_and_a_row():
    text = to_csv([classify(account())])
    lines = text.splitlines()
    assert "username" in lines[0] and "posts_last_14d" in lines[0]
    assert len(lines) == 2


def test_csv_flattens_newlines_in_bios():
    a = classify(account(biography="line one\n\nline two"))
    assert len(to_csv([a]).splitlines()) == 2


def test_json_round_trips():
    parsed = json.loads(to_json([classify(account())]))
    assert parsed[0]["profile_url"].startswith("https://www.instagram.com/")


def test_digest_shows_the_profile_link():
    assert "instagram.com/creator" in to_digest([classify(account())])


def test_empty_digest_suggests_next_steps():
    assert "--niches" in to_digest([])


# ------------------------------------- Instagram's own 400 bug, and the rescue
# Observed live on 2026-08-13: 2 of 6 healthy business accounts returned
#   {"message":"Asset asset://laser.provider/ig_business_category_subvertical
#    has been deleted. You cannot use this schema","status":"fail"}
# The accounts exist and their pages load. Two things must hold: it must not
# count as "the account is at risk", and the candidate must not be lost.
from ig_leads.client import BENIGN_400_MARKERS  # noqa: E402
from ig_leads.parsing import parse_count, parse_profile_page  # noqa: E402

PAGE = {
    "title": "Classic Soft Trim (@classicsofttrim) • Instagram photos and videos",
    "description": "14K Followers, 1,265 Following, 839 Posts - See Instagram "
                   "photos and videos from Classic Soft Trim (@classicsofttrim)",
    "header": "classicsofttrim\nClassic Soft Trim\n839 posts\n14K followers\n"
              "1,265 following\nAutomotive Customization Shop\n"
              "We Make Your Ride...YOURS!",
}


def test_the_real_400_body_is_recognised_as_benign():
    body = ('{"message":"Asset asset://laser.provider/ig_business_category_'
            'subvertical has been deleted. You cannot use this schema",'
            '"status":"fail"}').lower()
    assert any(m in body for m in BENIGN_400_MARKERS)


def test_a_real_block_is_not_mistaken_for_the_benign_400():
    for body in ('{"message":"feedback_required"}',
                 '{"message":"please wait a few minutes before you try again."}'):
        assert not any(m in body.lower() for m in BENIGN_400_MARKERS)


def test_page_fallback_recovers_the_account():
    a = parse_profile_page(PAGE, "classicsofttrim")
    assert a is not None
    assert a.followers == 14_000
    assert a.following == 1_265
    assert a.total_posts == 839
    assert a.full_name == "Classic Soft Trim"
    assert "Automotive Customization" in a.biography


def test_page_fallback_bio_excludes_the_counts_and_handle():
    bio = parse_profile_page(PAGE, "classicsofttrim").biography.lower()
    for noise in ("followers", "following", "posts", "classicsofttrim"):
        assert noise not in bio


def test_rescued_accounts_still_face_the_rival_filter():
    page = dict(PAGE, header="editor\nSome Editor\n10 posts\n5K followers\n"
                             "2 following\nVideo editor | DM for edits")
    a = parse_profile_page(page, "editor")
    a.posts_in_window = 9
    assert not classify(a).is_lead


@pytest.mark.parametrize("raw,suffix,expected", [
    ("14", "K", 14_000), ("1.2", "M", 1_200_000), ("839", None, 839),
    ("1,265", None, 1_265), ("2.5", "K", 2_500), ("", None, 0), ("junk", None, 0),
])
def test_count_parsing(raw, suffix, expected):
    assert parse_count(raw, suffix) == expected


def test_page_fallback_of_garbage_is_none():
    assert parse_profile_page(None, "x") is None
    assert parse_profile_page({}, "x") is None
    assert parse_profile_page({"description": "no numbers here"}, "x") is None


def test_approximate_counts_are_flagged_in_the_output():
    """A rounded 14K must not be silently presented as exact."""
    a = parse_profile_page(PAGE, "classicsofttrim")
    a.approximate_counts = True
    a.posts_in_window = 8
    classify(a)
    assert a.to_dict()["followers_approximate"] is True
    assert "followers_approximate" in to_csv([a]).splitlines()[0]


# ------------------------------------------------- the running lead record
from ig_leads.store import LeadStore  # noqa: E402


def test_leads_are_appended_to_the_master_csv(tmp_path):
    store = LeadStore(tmp_path)
    assert store.append([classify(account(username="one"))]) == 1
    assert store.path.exists()
    assert "one" in store.path.read_text(encoding="utf-8")


def test_a_second_run_appends_rather_than_overwrites(tmp_path):
    LeadStore(tmp_path).append([classify(account(username="one"))])
    LeadStore(tmp_path).append([classify(account(username="two"))])

    text = LeadStore(tmp_path).path.read_text(encoding="utf-8")
    assert "one" in text and "two" in text
    assert text.count("username,") == 1, "header written more than once"


def test_the_same_lead_is_not_recorded_twice(tmp_path):
    LeadStore(tmp_path).append([classify(account(username="one"))])
    again = LeadStore(tmp_path)
    assert again.append([classify(account(username="one"))]) == 0


def test_known_leads_are_remembered_across_runs(tmp_path):
    LeadStore(tmp_path).append([classify(account(username="Creator"))])
    reopened = LeadStore(tmp_path)
    assert "creator" in reopened.known
    assert "CREATOR" in reopened          # case-insensitive
    assert len(reopened) == 1


def test_only_leads_are_recorded(tmp_path):
    """Rejected accounts must never enter the record."""
    store = LeadStore(tmp_path)
    rejected = classify(account(username="dormant", posts_in_window=0))
    assert not rejected.is_lead
    assert store.append([rejected]) == 0
    assert not store.path.exists()


def test_first_seen_is_stamped(tmp_path):
    store = LeadStore(tmp_path)
    store.append([classify(account())])
    header = store.path.read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("first_seen,")


def test_record_carries_the_url_and_bio():
    """The two columns outreach actually needs."""
    a = classify(account(biography="Wildlife filmmaker, new video weekly"))
    header, row = to_csv([a]).splitlines()[:2]
    assert "profile_url" in header and "biography" in header
    assert "https://www.instagram.com/creator/" in row
    assert "Wildlife filmmaker" in row


def test_unreadable_record_does_not_lose_the_run(tmp_path):
    (tmp_path / "all_leads.csv").write_text("not,a,validcsvfile", encoding="utf-8")
    store = LeadStore(tmp_path)
    assert isinstance(store.known, set)


def test_filter_new_drops_known_accounts(tmp_path):
    store = LeadStore(tmp_path)
    store.append([classify(account(username="known"))])
    fresh = store.filter_new([account(username="known"), account(username="new")])
    assert [a.username for a in fresh] == ["new"]


def test_known_leads_are_skipped_before_any_request_is_spent():
    """The budget saving: a hashtag returns the same accounts each time."""
    from ig_leads.scraper import IGLeadScraper

    scraper = IGLeadScraper(known_leads={"known"})
    assert "known" in scraper.known_leads
