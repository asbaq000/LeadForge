"""Classifier tests, built from real posts.

Every string in `LEADS`, `SUPPLIERS` and `NOISE` is an actual X post collected
on 2026-08-12/13 (trimmed, otherwise verbatim). They are here because each one
broke an earlier version of the rules, and they are the regression net for any
future tuning: change a weight, run this, see what moved.
"""

import pytest

from x_leads.leads.classifier import LeadClassifier
from x_leads.models import Author, Tweet


def tweet(text: str, bio: str = "", followers: int = 500, profession: str = "") -> Tweet:
    return Tweet(
        tweet_id="1",
        text=text,
        author=Author(
            handle="someone", bio=bio, followers=followers,
            professional_category=profession,
        ),
    )


clf = LeadClassifier()


# --------------------------------------------------------------------- leads
LEADS = [
    "Urgent: Hiring a Short Form Video Editor. Please drop your portfolio in the "
    "comments or send a direct message. Rate: $150 USD per video",

    "HIRING VIDEO EDITOR  DROP PORTFOLIO BELOW AND DM ME",

    "Looking for a video editor! $500 per 30-second reel. If you're confident in "
    "creating high-quality engaging edits, let's connect.",

    "looking for a long form youtube video editor! it will be 10-20 minute videos "
    "in a fast paced style. dm me or reply to this with your work!",

    "guys i need a video editor for my channel",

    "Does anyone know a video editor that can make me weekly product videos?",

    "I need a mobile videographer that has worked with beauty brands please.",

    "Looking for a full-time video editor who excels at motion graphics and can turn "
    "raw talking head footage into high-performing content. Pay: 1.5K SGD / month",

    "REMOTE VIDEO EDITOR. Remote (Nigeria). 250,000 per month. Requirements: "
    "Experience editing short-form videos, interviews and social media videos",

    "VIDEOGRAPHER NEEDED FOR OUR EVENT - Multiple events Sep-Feb. Travel covered. "
    "Send your portfolio.",

    "Looking for Shorts Editor to Add to My Work Team. I'm looking for someone who "
    "is available for 1 short daily, who has knowledge of After Effects",

    "Still need a video editor for my past stream, 4h of footage, need a style like "
    "Coryxkenshin with a lot of funny comedic sfx. I'm on a low budget around $10-15",

    "Need a sick videographer in Abuja this Friday. Please send recs",

    "PAID UGC OPPORTUNITY. Looking for incredible UGC creators for a campaign with a "
    "global brand. If interested, drop your favorite work below",
]

# The hard cases: posted by freelance editors touting for work, using the exact
# words a buyer would. These outnumbered real leads ~3:1 in the raw sample.
SUPPLIERS = [
    "Are you looking for a video editor? DM me",
    "Pov: Client asked for product launch video? Are you looking for a video "
    "editor/ motion designer? DM me",
    "A finished hook to a VSL for a B2B business. If you're looking for a video "
    "editor send a DM",
    "highlights from recent work. Looking for a video editor? DM",
    "Recent re-edit. Looking for an editor? hit the DMs.",
    "Minecraft Edit with @Riiphy EXCLUSIVELY in Premiere Pro. Looking for a "
    "Minecraft Editor? DM me",
    "Need editor/Looking for a video editor? (Short form)/Clips Shoot me a DM. "
    "Past client work: #viral #edit #youtube",
    "Hello, good day! I'm Juan Ruiz, a video editor and post-production specialist. "
    "I adapt to different styles. Portfolio: juanruiz.vercel.app",
    "I create and edit engaging faceless TikTok videos, YouTube Shorts & Instagram "
    "Reels designed to grab attention.",
    "I'd be interested in connecting. I can help with the editing and packaging side "
    "of the channel, including thumbnails and titles.",
    "Need an editor for your next video? DM me.",
    "Trust me to always cook as a video editor. Looking for a video editor? You can "
    "send me a message",
]

NOISE = [
    "Bruh why is video editing so hard for me?",
    "Automating video editing is hard. Guess this will be my long term project",
    "AI JUST TURNED A FULL WEEKEND OF VIDEO EDITING INTO ONE SENTENCE. One raw "
    "recording in. Captions, B-roll, motion graphics out.",
    "Launch a faceless YouTube channel today! Get monetized and earn your first "
    "$9,000 by Dec. I usually charge $93 for this guide, but today it's FREE. "
    "Like + Comment 'YT' and I'll send you",
    "YouTube is making changes to the YouTube Partner Program for new applicants. "
    "Creators will need 8000 qualified watch hours",
    "5 After Effects Plugins to Improve Your Edits! (Thread)",
    "Learn a skill here for free. Cybersecurity, UI/UX Design, AI Automation, "
    "Video Editing with CapCut",
    "Day 4 after YouTube monetization. 2.3m views. Is this a good start?",
    "BREAKING: Claude can now build you a full AI YouTube channel like a "
    "$10,000/month creator agency. Here are 7 prompts:",
]


@pytest.mark.parametrize("text", LEADS)
def test_real_leads_are_kept(text):
    lead = clf.classify(tweet(text))
    assert lead.is_lead, f"missed a real lead ({lead.reject_reason}): {text[:70]}"


@pytest.mark.parametrize("text", SUPPLIERS)
def test_freelancers_touting_for_work_are_rejected(text):
    lead = clf.classify(tweet(text))
    assert not lead.is_lead, (
        f"kept a supplier as a lead (score {lead.score}, {lead.signals}): {text[:70]}"
    )


@pytest.mark.parametrize("text", NOISE)
def test_noise_is_rejected(text):
    lead = clf.classify(tweet(text))
    assert not lead.is_lead, f"kept noise (score {lead.score}): {text[:70]}"


# ------------------------------------------------------------- specific rules
def test_second_person_pitch_is_the_discriminator():
    """The same words, opposite meanings, told apart by who is being addressed."""
    buyer = clf.classify(tweet("Looking for a video editor for my channel, paid"))
    seller = clf.classify(tweet("Are you looking for a video editor? DM me"))
    assert buyer.is_lead and not seller.is_lead
    assert "sales pitch" in seller.reject_reason


def test_hook_plus_hiring_survives_the_pitch_rule():
    """A real job post may open with a hook; the applicant instruction saves it."""
    lead = clf.classify(
        tweet("Need a video editor? We're hiring! Drop your portfolio below.")
    )
    assert lead.is_lead


def test_budget_promotes_a_lead():
    without = clf.classify(tweet("looking for a video editor for my channel"))
    with_budget = clf.classify(
        tweet("looking for a video editor for my channel, $500 per video")
    )
    assert with_budget.score > without.score


def test_asking_for_a_portfolio_outweighs_everything():
    lead = clf.classify(tweet("editor needed, drop your reel below"))
    assert lead.verdict in ("warm", "hot")


def test_off_topic_is_rejected_early():
    lead = clf.classify(tweet("Looking for a plumber in Manchester, paid, urgent"))
    assert not lead.is_lead
    assert "not about video" in lead.reject_reason


def test_styled_unicode_is_normalised():
    """Job posts love bold unicode; NFKC is what makes them matchable."""
    lead = clf.classify(tweet("𝐇𝐈𝐑𝐈𝐍𝐆 𝐕𝐈𝐃𝐄𝐎 𝐄𝐃𝐈𝐓𝐎𝐑 — drop your portfolio below"))
    assert lead.is_lead


def test_full_time_hyphen_does_not_break_the_match():
    """`[\\w\\s]` stops at a hyphen; this is why GAP exists."""
    lead = clf.classify(tweet("Looking for a full-time video editor. Pay: $1500/month"))
    assert lead.is_lead


def test_editor_bio_lowers_but_does_not_decide():
    """A working editor can still be hiring; the bio only nudges the score."""
    plain = clf.classify(tweet("HIRING VIDEO EDITOR. drop your portfolio below"))
    with_bio = clf.classify(
        tweet("HIRING VIDEO EDITOR. drop your portfolio below", bio="video editor | 8M+ views")
    )
    assert with_bio.score < plain.score
    assert with_bio.is_lead


def test_professional_category_is_a_supply_signal():
    a = clf.classify(tweet("looking for a video editor for my channel"))
    b = clf.classify(tweet("looking for a video editor for my channel", profession="Editor"))
    assert b.score < a.score


def test_min_followers_is_off_by_default():
    lead = clf.classify(tweet("Hiring a video editor, drop your reel", followers=12))
    assert lead.is_lead


def test_min_followers_when_asked_for():
    picky = LeadClassifier(min_followers=1000)
    lead = picky.classify(tweet("Hiring a video editor, drop your reel", followers=12))
    assert not lead.is_lead


def test_signals_explain_the_verdict():
    lead = clf.classify(tweet("Hiring a video editor! $400/month. Drop your portfolio."))
    assert any(s.startswith("demand:") for s in lead.signals)
    assert "context:budget" in lead.signals


def test_empty_text_is_rejected_not_crashed():
    assert not clf.classify(tweet("")).is_lead


def test_classify_all_sorts_best_first():
    tweets = [
        tweet("looking for an editor"),
        tweet("HIRING VIDEO EDITOR $500/video, drop your portfolio below"),
    ]
    leads = clf.classify_all(tweets)
    assert leads[0].score > leads[1].score


def test_bare_need_someone_is_too_weak_on_its_own():
    """Fan chatter ("need someone to edit this clip") is not a business lead."""
    assert not clf.classify(tweet("need someone to edit seungmin with hey hi")).is_lead


def test_need_someone_counts_when_a_channel_is_behind_it():
    assert clf.classify(tweet("need someone to edit my youtube videos weekly")).is_lead


# ---- suppliers found leaking through on a live run (2026-08-13), now pinned
LIVE_SUPPLIERS = [
    # A freelancer's hook: "Hiring Video Editor?" with the question mark.
    "Recent Talking Head VSL Edit. Hiring Video Editor? DM!",
    # An editor replying to somebody else's job post, quoting their words.
    "hey! i saw you are looking for an editor can i get a chance?",
    # An agency pitching in the third person, past every first-person rule.
    "Hello, I saw your post looking for a YouTube editor. I run FlowwStudio, a "
    "video editing team with experience in the IRL/streamer space. Portfolio: link",
    "I'm interested! I'd love to help with your channel, just sent you a DM",
]


@pytest.mark.parametrize("text", LIVE_SUPPLIERS)
def test_live_supplier_leaks_stay_closed(text):
    lead = clf.classify(tweet(text))
    assert not lead.is_lead, f"supplier leaked back in (score {lead.score}): {text[:60]}"


def test_a_real_hiring_post_is_unaffected_by_the_rhetorical_rule():
    """`hiring` joined the pitch rule; a genuine ad must still get through."""
    assert clf.classify(tweet("HIRING VIDEO EDITOR. DROP PORTFOLIO BELOW")).is_lead
    assert clf.classify(
        tweet("Looking for a video editor for my new YouTube channel! DM me")
    ).is_lead


def test_self_declaration_beyond_the_strict_article_form():
    """"I'm actually the designer" self-identifies as much as "I'm a designer"."""
    for text in (
        "Looking for a Thumbnail Designer. Well... I'm actually the designer",
        "I'm literally a video editor, dm me",
        "I'm a freelancer looking for a video editor gig",
    ):
        assert not clf.classify(tweet(text)).is_lead, text
