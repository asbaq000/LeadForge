"""Reading the money out of a post, and getting it into the output.

The cases here are the phrasings that showed up in real collected posts, plus
the near-misses that a naive "find a dollar sign" version got wrong.
"""

import pytest

from x_leads.leads.budget import extract_budget, find_budget
from x_leads.leads.classifier import LeadClassifier
from x_leads.models import Author, Tweet
from x_leads import output


# ------------------------------------------------------------- the amount
@pytest.mark.parametrize("text,expected", [
    ("Hiring an editor. $500 per video.", "$500/video"),
    ("Budget: $1,500 a month for shorts", "$1,500/month"),
    ("paying $5k/month for a full time editor", "$5,000/month"),
    ("Need an editor, $500 per 30-second reel", "$500/reel"),
    ("rate is $50/hr", "$50/hour"),
    ("$2000 monthly retainer", "$2,000/month"),
    # A currency code is rendered as its symbol where there is a well-known
    # one, so "300 usd" and "$300" don't sort as two different things.
    ("looking for an editor, 300 usd per video", "$300/video"),
    ("we pay INR 5000 per edit", "₹5,000/edit"),
    ("budget 400 aed per reel", "400 AED/reel"),
    ("paying 5k/month", "5,000/month"),
    ("my budget is around 150", "150"),
    ("$1.5k per video", "$1,500/video"),
    ("Compensation: $2,500/mo + bonus", "$2,500/month"),
    # A trailing symbol is how most non-US posters write it.
    ("budget is around 30$ per thumbnail", "$30/thumbnail"),
    # "/vid" has to be a known unit, or the unit search runs on and finds the
    # "a week" belonging to the volume clause instead.
    ("Editor wanted. $150/vid, 4 vids a week.", "$150/video"),
])
def test_amounts_are_read(text, expected):
    assert extract_budget(text) == expected


def test_a_range_keeps_both_ends():
    assert extract_budget("Editor needed, $500-$1000 per video") == "$500-$1,000/video"
    # Only the second number carries the currency here.
    assert extract_budget("budget 150 to 200 usd") == "$150-$200"


def test_the_period_is_optional():
    assert extract_budget("Hiring a video editor, budget $800") == "$800"


def test_parts_are_available_for_sorting():
    found = find_budget("$500-$1000 per video")
    assert (found.low, found.high, found.currency, found.period) == (
        500.0, 1000.0, "$", "video"
    )


# ------------------------------------------------------- and what it isn't
@pytest.mark.parametrize("text", [
    "Just hired an editor, no budget talk here",
    "I need a video editor for my channel",
    "3 videos a week, long term",
    "we shot 4k footage, need an editor",
    "looking for an editor, pay is negotiable",
    "we have 50000 subscribers and need an editor",
])
def test_no_money_means_no_budget(text):
    assert extract_budget(text) == ""


@pytest.mark.parametrize("text", [
    # The guru post, which the classifier rejects anyway — but a rejected row
    # is still read, and "$10,000/month" next to it would be a lie.
    "I made $10k last month editing videos. DM for the system.",
    "$500 giveaway! Retweet and tag a video editor",
    "Giving away $500 to a random editor who retweets this",
    "buy $PEPE now, 100x gains",
    "our startup raised $2m, hiring soon",
    "$100 in sales today",
])
def test_someone_elses_money_is_not_a_budget(text):
    assert extract_budget(text) == ""


def test_zero_is_not_a_budget():
    assert extract_budget("$0 budget but great exposure") == ""


def test_styled_unicode_still_parses():
    """Job posts love styled unicode; normalise() folds it back to ASCII."""
    assert extract_budget("𝐇𝐈𝐑𝐈𝐍𝐆 editor — $750 per video") == "$750/video"


def test_a_link_cannot_fake_an_amount():
    assert extract_budget("apply https://x.com/jobs/500usd") == ""


# --------------------------------------------------------------- the wiring
def _tweet(text, tid="1"):
    return Tweet(tweet_id=tid, text=text, author=Author(handle="buyer", followers=900))


def test_the_classifier_stamps_budget_and_niche():
    lead = LeadClassifier(niche="video_editing").classify(
        _tweet("Hiring a video editor for my channel! $500 per video. "
               "Drop your portfolio below.")
    )
    assert lead.verdict == "hot"
    assert lead.budget == "$500/video"
    assert lead.niche == "video_editing"


def test_rejected_leads_carry_them_too():
    """The audit view is exactly where a dropped budget needs to be visible."""
    lead = LeadClassifier(niche="video_editing").classify(
        _tweet("I'm a video editor, hire me! $50 per edit, DM me")
    )
    assert lead.verdict == "rejected"
    assert lead.budget == "$50/edit"
    assert lead.niche == "video_editing"


def test_csv_carries_both_columns():
    lead = LeadClassifier(niche="video_editing").classify(
        _tweet("Hiring a video editor! Budget $800/video. Drop your reel below.")
    )
    header, row = output.to_csv([lead]).splitlines()[:2]
    columns = header.split(",")
    assert "budget" in columns and "niche" in columns
    assert row.split(",")[columns.index("budget")] == "$800/video"
    assert row.split(",")[columns.index("niche")] == "video_editing"


def test_digest_shows_the_budget_on_the_headline():
    lead = LeadClassifier(niche="video_editing").classify(
        _tweet("Hiring a video editor! $800 per video. Drop your reel below.")
    )
    assert "$800/video" in output.to_digest([lead]).splitlines()[3]
