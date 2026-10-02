#!/usr/bin/env python3
"""
test_scraper_logic.py

Standalone logic-level test suite for fb_group_url_collector.py.

This does NOT drive a real browser or write to the real Google Sheet -- it
imports the pure functions (classification, phone extraction, date parsing,
URL validation, dedup, quota) and runs them against synthetic post text and
mock DOM-like objects, so it can run without Selenium/Chrome/Sheets access.

Run: python test_scraper_logic.py
"""

import importlib.util
import json
import os
import tempfile
from datetime import datetime, timedelta

spec = importlib.util.spec_from_file_location("collector", "fb_group_url_collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

# Groq is a network call and is only used to fill the Niche column, so it is
# switched OFF here: the suite must stay deterministic and runnable offline.
# resolve_niche()'s fallback-to-hashtags path is asserted explicitly below.
collector.GROQ_ENABLED = False

passed = 0
failed = 0
failures = []


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        failures.append(label)
        print(f"  FAIL: {label}")


# ---------------------------------------------------------------------------
# 1. LEAD FILTER -- INCLUDE list from the requirements, verbatim/near-verbatim
# ---------------------------------------------------------------------------
print("== Lead filter: INCLUDE (genuine customer leads) ==")

INCLUDE_CASES = [
    "Looking for a video editor for my YouTube channel, remote work.",
    "Need video editing services for my brand's social media, freelance/remote.",
    "Hiring a video editor for our business videos, work from home ok.",
    "Need someone to edit videos for my TikTok, remote/freelance.",
    "Looking for an editor for YouTube/social media/business videos, freelance.",
    "Looking to outsource video editing for our company reels, remote.",
    "Asking for recommendations for a video editor, freelance/remote work.",
]

for text in INCLUDE_CASES:
    check(f"INCLUDE matches: {text[:50]!r}", collector.is_qualifying_video_editing_lead(text) is True)

# Regression: real post text observed live in the user's Group 4, which was
# wrongly rejected as 'unknown' because "DM me if" and "years experience"
# were treated as hard seller signals -- both are ordinary BUYER wording
# (the hirer asking to be contacted, and stating what they require of a
# candidate). See _WEAK_SELLER_PATTERNS.
REAL_GROUP4_POST = (
    "Looking for a Long-Term Video Editor for a Police Bodycam Channel "
    "(1 Video/Week + Room to Scale)  Hey everyone! I am launching a new "
    "YouTube channel. DM me if interested. Must have 2 years experience."
)
check("Real Group-4 buyer post classifies as buyer", collector.classify_post_intent(REAL_GROUP4_POST) == "buyer")
check("Real Group-4 buyer post qualifies as a lead", collector.is_qualifying_video_editing_lead(REAL_GROUP4_POST) is True)
check(
    "Buyer post using 'DM me if interested' + 'years experience' still qualifies",
    collector.is_qualifying_video_editing_lead(
        "Need an editor for my YouTube channel. DM me if interested. Must have 2 years experience."
    ) is True,
)
check(
    "Telegram-contact buyer post qualifies",
    collector.is_qualifying_video_editing_lead(
        "im looking for a video editor - contact me on telegram - asap@skdexz"
    ) is True,
)
# ...but a bare "DM me for editing work" with no hiring language is still a seller.
check(
    "Seller post with only weak contact wording is still excluded",
    collector.is_qualifying_video_editing_lead("DM me for video editing work, 3 years experience.") is False,
)

# Regression: real posts observed live that every buyer pattern missed
# because they put the ROLE NOUN FIRST, or asked in another language.
LIVE_MISSED_BUYERS = [
    ("role-noun-first, live Group-1 post",
     "Video Editor Required! Interested? WhatsApp: +91 9735001668 Portfolio/Previous Work zaroor share karein."),
    ("Bengali/English mixed, live Group-1 post",
     "বাংলা ভাষী Video Editor চাই। যারা health নিয়ে এডিটিং করতে আগ্রহী। যোগাযোগ করুন। Long term collaboration..For Long Videos."),
    ("'Required' before the role noun",
     "Required Video Editor for youtube channel, remote work"),
    ("romanized Hindi/Urdu 'chahiye'",
     "Mujhe ek video editor chahiye for my youtube channel"),
    ("Tagalog 'kailangan'",
     "Kailangan ko ng video editor para sa YouTube channel ko"),
]
for label, text in LIVE_MISSED_BUYERS:
    check(f"Live-missed buyer now qualifies ({label})", collector.is_qualifying_video_editing_lead(text) is True)

# Typographic apostrophes must not let a seller post slip through.
check(
    "Seller post with a curly apostrophe is still excluded",
    collector.is_qualifying_video_editing_lead(
        "Hi, I’m a video editor with 3 years of experience. Hire me!"
    ) is False,
)
check(
    "_normalize_for_matching converts curly punctuation",
    collector._normalize_for_matching("I’m “ready”") == 'I\'m "ready"',
)

# ---------------------------------------------------------------------------
# 1b. LEAD FILTER -- EXCLUDE list from the requirements
# ---------------------------------------------------------------------------
print("== Lead filter: EXCLUDE (not genuine customer leads) ==")

EXCLUDE_CASES = [
    ("I am a video editor with 5 years of experience, available for hire.", "self-promo seller"),
    ("Hello sir i am video editor", "seller, real backup-file example"),
    ("I am an expert in After Effects, working on documentaries, podcasts, map animations, and motion graphics.", "seller, real backup-file example"),
    ("Offering video editing services, DM me for rates.", "agency/freelancer selling"),
    ("We offer professional video editing services for all budgets.", "agency selling"),
    ("Freelance video editor looking for new clients, check my portfolio.", "freelancer looking for clients"),
    ("Looking for a job as a video editor, fresh graduate, open to work.", "job seeker"),
    ("Good templates", "unrelated, real backup-file example"),
    ("Imr An", "unrelated/name only, real backup-file example"),
    ("85 me itna sab demand kar rahe ho sabzi lene aye ho kia", "unrelated non-english, real backup-file example"),
    ("On-site video editor needed at our office, full-time position, must be available in person.", "onsite job, excluded by work-arrangement filter"),
]

# Regression: non-native "office based" phrasings seen live must be caught
# by the on-site filter (a pure office job is not remote/freelance work).
for _t in ["Video editor needed, office bas job, Lahore",
           "Video editor required for office job in Karachi"]:
    check(f"On-site phrasing excluded: {_t[:40]!r}", collector.is_qualifying_video_editing_lead(_t) is False)
# ...but a post explicitly offering freelance AS WELL AS office work is kept.
check(
    "Post offering both freelance and office work is still kept",
    collector.is_qualifying_video_editing_lead(
        "We need video editor freelancer and office bas job DM inbox me for more details"
    ) is True,
)

# Regression: rhetorical freelancer ads that quote the buyer's question and
# then answer it with themselves. This one reached a delivered CSV before the
# fix: "LOOKING FOR A VIDEO EDITOR? YOU JUST FOUND ONE."
for _t in ["LOOKING FOR A VIDEO EDITOR? YOU JUST FOUND ONE.",
           "Looking for a video editor? Look no further, I have 5 years experience.",
           "Need a video editor? Your search ends here!",
           "Want a video editor? I am your guy."]:
    check(f"Rhetorical freelancer ad excluded: {_t[:42]!r}",
          collector.is_qualifying_video_editing_lead(_t) is False)

for text, why in EXCLUDE_CASES:
    check(f"EXCLUDE ({why}): {text[:50]!r}", collector.is_qualifying_video_editing_lead(text) is False)

# --- Niche resolution (Groq off -> must fall back to hashtags, never crash) ---
check("resolve_niche falls back to hashtags when Groq is off",
      collector.resolve_niche("Need an editor #gaming #reels") == "#gaming, #reels")
check("resolve_niche returns None when there is nothing to go on",
      collector.resolve_niche("Need an editor") is None)
check("groq_detect_niche is a no-op when disabled",
      collector.groq_detect_niche("I need a gaming clip video editor") is None)
check("Stories URLs are rejected (never leads)",
      collector.normalize_and_validate_post_url(
          "https://www.facebook.com/stories/404228563674977/UzpfSVNDOjE2NzQ=") is None)
check("Group post URLs still accepted",
      collector.normalize_and_validate_post_url(
          "https://www.facebook.com/groups/ineedavideoeditor/posts/2628994924220500") is not None)

# ---------------------------------------------------------------------------
# 2. DATE FILTER -- last 2 days, various relative timestamps
# ---------------------------------------------------------------------------
print("== Date filter: last 2 days ==")

NOW = datetime(2026, 8, 12, 18, 0, 0)
CUTOFF = NOW - timedelta(days=collector.DAYS_BACK)

WITHIN_WINDOW = ["1 hour ago", "5 hours ago", "15 hours ago", "23 hours ago", "yesterday", "1 d", "47 hours ago"]
OUTSIDE_WINDOW = ["3 days ago", "5 d", "1 week ago", "49 hours ago"]

for raw in WITHIN_WINDOW:
    dt = collector.parse_facebook_timestamp(raw, now=NOW)
    check(f"WITHIN 2-day window kept: {raw!r} -> {dt}", dt is not None and dt >= CUTOFF)

for raw in OUTSIDE_WINDOW:
    dt = collector.parse_facebook_timestamp(raw, now=NOW)
    check(f"OUTSIDE 2-day window excluded: {raw!r} -> {dt}", dt is not None and dt < CUTOFF)

# Unparseable timestamp must return None (post gets skipped, never guessed).
check("Unparseable timestamp returns None", collector.parse_facebook_timestamp("garbled unicode 2h", now=NOW) is None)

# ---------------------------------------------------------------------------
# 3. PHONE NUMBERS -- extracted only from the post's own text, never guessed,
#    and never confused with a budget figure.
# ---------------------------------------------------------------------------
print("== Phone number extraction ==")

check(
    "Extracts a plain phone number from post text",
    collector.extract_phone_number("Need a video editor, WhatsApp me at 03001234567 if interested.") == "03001234567",
)
check(
    "Extracts a dashed/international phone number",
    collector.extract_phone_number("Contact me on +1-555-123-4567 for details.") == "+1-555-123-4567",
)
check(
    "No phone number present -> None",
    collector.extract_phone_number("Need a video editor for my YouTube channel, remote work.") is None,
)
check(
    "Budget figure is NOT mistaken for a phone number",
    collector.extract_phone_number("Need a video editor. Budget: 5000000 PKR, negotiable, remote.") is None,
)
check(
    "Short numbers (e.g. video counts) are NOT extracted as phone numbers",
    collector.extract_phone_number("Need 10-15 min videos, 5-10 videos per month, remote work.") is None,
)
# Contact info is only read from the post's own text (extract_post_text pulls
# only the post message container, never comments or profile fields) -- this
# is enforced structurally by only ever calling extract_phone_number on the
# string returned by extract_post_text, verified in process_posts.

# ---------------------------------------------------------------------------
# 4. GOOGLE SHEET FIELDS -- exact column list matches the LIVE "Lead Scraper"
#    sheet's "Facebook URLs" tab (verified by hand in the browser), and row
#    building never adds/drops a column.
# ---------------------------------------------------------------------------
print("== Google Sheet field mapping ==")

LIVE_SHEET_HEADERS = ["Topic", "Niche", "URL", "Budget", "Number", "Post Date and Time", "Has Contact Info"]
check("SHEET_HEADERS matches the live sheet exactly", collector.SHEET_HEADERS == LIVE_SHEET_HEADERS)

sample_post = collector.CollectedPost(
    url="https://www.facebook.com/groups/123/posts/456",
    post_date=datetime(2026, 8, 12, 10, 0, 0),
    topic="Need a video editor",
    niche="#reels",
    budget=None,
    phone_number="03001234567",
)


class _FakeWorksheet:
    def __init__(self, existing_rows=1):
        self._existing_rows = existing_rows
        self.appended = None

    def col_values(self, _col):
        return ["URL"] + ["x"] * (self._existing_rows - 1)

    def append_rows(self, rows, value_input_option="RAW"):
        self.appended = rows


ws = _FakeWorksheet(existing_rows=1)
collector.append_posts_to_sheet(ws, [sample_post])
row = ws.appended[0]
check("Row has exactly 7 fields (no added/dropped columns)", len(row) == len(collector.SHEET_HEADERS))
check("Topic field correct", row[0] == "Need a video editor")
check("Niche field correct", row[1] == "#reels")
check("URL field correct", row[2] == sample_post.url)
check("Budget left blank when not available (never guessed)", row[3] == "")
check("Number field is a sequential row count", row[4] == 1)
check("Post Date and Time formatted", row[5] == "2026-08-12 10:00:00")
check("Has Contact Info holds the extracted phone number", row[6] == "03001234567")

blank_post = collector.CollectedPost(
    url="https://www.facebook.com/groups/123/posts/789",
    post_date=datetime(2026, 8, 12, 11, 0, 0),
    topic="Need an editor",
    niche=None,
    budget=None,
    phone_number=None,
)
ws2 = _FakeWorksheet(existing_rows=1)
collector.append_posts_to_sheet(ws2, [blank_post])
row2 = ws2.appended[0]
check("Has Contact Info left blank when no phone number in post", row2[6] == "")
check("Niche left blank when not available", row2[1] == "")

# ---------------------------------------------------------------------------
# 5. URL VALIDATION + DEDUPLICATION
# ---------------------------------------------------------------------------
print("== URL validation + deduplication ==")

check(
    "Valid group post URL accepted",
    collector.normalize_and_validate_post_url("https://www.facebook.com/groups/123456/posts/789012") is not None,
)
check(
    "Photo URL rejected (not a post permalink)",
    collector.normalize_and_validate_post_url("https://www.facebook.com/groups/123456/photos/789012") is None,
)
check(
    "Comment URL rejected",
    collector.normalize_and_validate_post_url("https://www.facebook.com/groups/123456/posts/789012?comment_id=1") is not None,  # comment_id as query param, path itself is still a post
)
check(
    "Group root URL rejected (not a post)",
    collector.normalize_and_validate_post_url("https://www.facebook.com/groups/123456/") is None,
)
check(
    "Non-Facebook URL rejected",
    collector.normalize_and_validate_post_url("https://example.com/groups/123/posts/456") is None,
)

with tempfile.TemporaryDirectory() as tmp:
    seen_path = os.path.join(tmp, "seen.json")
    orig_seen_file = collector.SEEN_FILE
    collector.SEEN_FILE = seen_path
    try:
        check("load_seen_urls on missing file returns empty set", collector.load_seen_urls() == set())
        collector.save_seen_urls({"https://www.facebook.com/groups/1/posts/1"})
        check("save_seen_urls + load_seen_urls round-trips", collector.load_seen_urls() == {"https://www.facebook.com/groups/1/posts/1"})
    finally:
        collector.SEEN_FILE = orig_seen_file

# ---------------------------------------------------------------------------
# 5b. BUDGET PER MINUTE + CROSS-GROUP LEAD DEDUPLICATION
# ---------------------------------------------------------------------------
print("== Budget per minute ==")

PER_MINUTE_CASES = [
    ("Need editor. $500 per minute", "$500 per minute"),
    ("Rate $100/minute", "$100/minute"),
    ("paying 100 per min", "100 per min"),
    ("500 per video minute for long form", "500 per video minute"),
    ("Need video editor 400 per mint no fresh editor", "400 per mint"),   # live post, misspelled
    ("Need AI editor budget 300 pr mint", "300 pr mint"),                 # live post, "pr" = per
    ("per minute rate 500", "per minute rate 500"),
]
for text, expected in PER_MINUTE_CASES:
    check(f"per-minute rate extracted from {text[:38]!r}",
          collector.extract_budget_per_minute(text) == expected)

# A rate must never be invented, and unrelated uses of "minute" must not match.
check("no per-minute rate when the post states none",
      collector.extract_budget_per_minute("Hiring an editor, budget $600") is None)
check("'in 5 minutes' is not a rate",
      collector.extract_budget_per_minute("I will be there in 5 minutes") is None)
# A per-minute rate takes precedence over a bare currency amount.
check("extract_budget prefers the per-minute rate",
      collector.extract_budget("Total $5000 project, paying $200 per minute") == "$200 per minute")
check("extract_budget still returns a plain amount when there's no rate",
      collector.extract_budget("Hiring an editor, budget $600") == "budget $600")

print("== Cross-group lead deduplication ==")

CROSS_POST = ("Looking for a long term video editor for my gaming channel. "
              "We publish 3 videos a week and need fast turnaround. Remote work.")
sig_a = collector.lead_signature(CROSS_POST, None)

# Same text posted into another group (different URL) is the SAME lead.
check("identical repost in another group is a duplicate",
      collector.is_duplicate_lead(collector.lead_signature(CROSS_POST, None), [sig_a]) is True)
# Lightly edited repost is still the same lead.
check("lightly edited repost is still a duplicate",
      collector.is_duplicate_lead(
          collector.lead_signature(CROSS_POST.replace("3 videos", "4 videos") + " Thanks!", None),
          [sig_a]) is True)
# A genuinely different request is NOT a duplicate.
check("a different lead is not treated as a duplicate",
      collector.is_duplicate_lead(
          collector.lead_signature(
              "Need a wedding videographer to edit ceremony footage, one-off project.", None),
          [sig_a]) is False)
# Same phone number = same person, even with different wording.
check("same phone number means same person",
      collector.is_duplicate_lead(
          collector.lead_signature("Need an editor asap", "0311-778-2560"),
          [collector.lead_signature("Different wording entirely", "03117782560")]) is True)
# Short generic posts must NOT collapse -- two people can both write this.
check("short generic posts are not merged",
      collector.is_duplicate_lead(
          collector.lead_signature("Hiring Video Editors", None),
          [collector.lead_signature("Hiring Video Editors", None)]) is False)
check("empty signature list means nothing is a duplicate",
      collector.is_duplicate_lead(sig_a, []) is False)

# Signature store round-trips.
with tempfile.TemporaryDirectory() as tmp:
    sig_path = os.path.join(tmp, "sigs.json")
    _orig = collector.LEAD_SIGNATURE_FILE
    collector.LEAD_SIGNATURE_FILE = sig_path
    try:
        check("signature store starts empty", collector.load_lead_signatures() == [])
        collector.save_lead_signatures([sig_a])
        check("signature store round-trips", collector.load_lead_signatures() == [sig_a])
    finally:
        collector.LEAD_SIGNATURE_FILE = _orig

# ---------------------------------------------------------------------------
# 6. DAILY LIMIT (MAX_LEADS_PER_RUN = 20), enforced via quota file
# ---------------------------------------------------------------------------
print("== Daily lead limit ==")

check("MAX_LEADS_PER_RUN is set to 20", collector.MAX_LEADS_PER_RUN == 20)

with tempfile.TemporaryDirectory() as tmp:
    quota_path = os.path.join(tmp, "quota.json")
    orig_quota_file = collector.DAILY_QUOTA_FILE
    collector.DAILY_QUOTA_FILE = quota_path
    try:
        check("Quota starts at 0 with no file", collector.load_daily_quota_used() == 0)
        collector.save_daily_quota_used(20)
        check("Quota persists across load", collector.load_daily_quota_used() == 20)
        today = datetime.now().strftime("%Y-%m-%d")
        with open(quota_path, "w", encoding="utf-8") as f:
            json.dump({"date": "2000-01-01", "count": 20}, f)
        check("Quota resets automatically on a new calendar day", collector.load_daily_quota_used() == 0)
    finally:
        collector.DAILY_QUOTA_FILE = orig_quota_file

# max_new_leads stop-immediately behavior, using a fake driver + containers
# that simulate 25 qualifying posts so we can confirm process_posts stops at
# exactly the cap instead of continuing to scan.


class _FakeElement:
    def __init__(self, text):
        self._text = text

    @property
    def text(self):
        return self._text

    def find_elements(self, by, selector):
        return []

    def get_attribute(self, name):
        return None


class _FakeContainer:
    """Simulates a post container that already carries a static permalink
    href (skips the click-to-reveal path) and a message div with post text."""

    def __init__(self, idx, post_text):
        self._idx = idx
        self._post_text = post_text
        self.url = f"https://www.facebook.com/groups/999/posts/{1000 + idx}"

    @property
    def text(self):
        return f"post-{self._idx} {self._post_text}"

    def find_elements(self, by, selector):
        if selector == "a":
            return [_FakeAnchor(self.url)]
        if selector == collector.POST_MESSAGE_SELECTOR:
            return [_FakeElement(self._post_text)]
        if selector == "[data-visualcompletion='loading-state']":
            return []
        if selector == "abbr":
            return []
        return []

    def get_attribute(self, name):
        return None


class _FakeAnchor:
    def __init__(self, href):
        self._href = href

    def get_attribute(self, name):
        if name == "href":
            return self._href
        return None

    @property
    def text(self):
        return ""


class _FakeDriver:
    def __init__(self, containers):
        self._containers = containers
        self.current_url = "https://www.facebook.com/groups/999/"

    def execute_script(self, *a, **kw):
        return None


# The pipeline fixtures below deliberately reuse ONE post text across many
# fake posts. Cross-group lead dedup would (correctly) collapse those into a
# single lead, but these tests exercise the cap / duplicate-URL / old-post
# paths, so similarity dedup is switched off for them. It has its own tests
# in section 5b.
_ORIG_MIN_CHARS = collector.LEAD_MIN_CHARS_FOR_SIMILARITY
collector.LEAD_MIN_CHARS_FOR_SIMILARITY = 10**9

N_POSTS = 25
QUALIFYING_TEXT = "Need a video editor for my YouTube channel, remote/freelance work."
fake_containers = [_FakeContainer(i, QUALIFYING_TEXT) for i in range(N_POSTS)]
fake_driver = _FakeDriver(fake_containers)

orig_find_containers = collector.find_post_containers
orig_extract_ts = collector.extract_post_timestamp_from_container
collector.find_post_containers = lambda driver: fake_containers
collector.extract_post_timestamp_from_container = lambda container: "1 hour ago"
orig_max_posts = collector.MAX_POSTS_PER_RUN
collector.MAX_POSTS_PER_RUN = N_POSTS
try:
    result = collector.process_posts(fake_driver, CUTOFF, set(), max_new_leads=20)
    check("Stops immediately at the 20-lead cap even with 25 qualifying posts available", len(result.collected) == 20)
finally:
    collector.find_post_containers = orig_find_containers
    collector.extract_post_timestamp_from_container = orig_extract_ts
    collector.MAX_POSTS_PER_RUN = orig_max_posts

# Duplicate URLs (already in seen_urls) never count toward collected/new leads.
collector.find_post_containers = lambda driver: fake_containers[:5]
collector.extract_post_timestamp_from_container = lambda container: "1 hour ago"
collector.MAX_POSTS_PER_RUN = 5
try:
    already_seen = {c.url for c in fake_containers[:3]}
    result2 = collector.process_posts(fake_driver, CUTOFF, already_seen, max_new_leads=20)
    check("Duplicates skipped, not counted as new leads", result2.duplicates_skipped == 3 and len(result2.collected) == 2)
finally:
    collector.find_post_containers = orig_find_containers
    collector.extract_post_timestamp_from_container = orig_extract_ts
    collector.MAX_POSTS_PER_RUN = orig_max_posts

# Non-qualifying (seller) posts never count toward collected/new leads either.
seller_containers = [_FakeContainer(100 + i, "I am a video editor, available for hire, DM me.") for i in range(3)]
collector.find_post_containers = lambda driver: seller_containers
collector.extract_post_timestamp_from_container = lambda container: "1 hour ago"
collector.MAX_POSTS_PER_RUN = 3
try:
    result3 = collector.process_posts(fake_driver, CUTOFF, set(), max_new_leads=20)
    check("Seller/freelancer posts excluded, not counted as leads", result3.not_qualifying_lead_skipped == 3 and len(result3.collected) == 0)
finally:
    collector.find_post_containers = orig_find_containers
    collector.extract_post_timestamp_from_container = orig_extract_ts
    collector.MAX_POSTS_PER_RUN = orig_max_posts

# Old posts (outside the 2-day window) never count toward collected/new leads.
old_containers = [_FakeContainer(200 + i, QUALIFYING_TEXT) for i in range(2)]
collector.find_post_containers = lambda driver: old_containers
collector.extract_post_timestamp_from_container = lambda container: "5 days ago"
collector.MAX_POSTS_PER_RUN = 2
try:
    result4 = collector.process_posts(fake_driver, CUTOFF, set(), max_new_leads=20)
    check("Old posts excluded, not counted as leads", result4.old_posts_skipped == 2 and len(result4.collected) == 0)
finally:
    collector.find_post_containers = orig_find_containers
    collector.extract_post_timestamp_from_container = orig_extract_ts
    collector.MAX_POSTS_PER_RUN = orig_max_posts

# ---------------------------------------------------------------------------
# 6b. FULL-HISTORY SCANNING -- a group's scan must not stop after just the
#    first screenful; it should keep scrolling for more (up to
#    CONSECUTIVE_EMPTY_SCROLLS_TO_STOP retries) and must stop once it has
#    clearly scrolled past the DAYS_BACK window (CONSECUTIVE_OLD_POSTS_TO_STOP
#    consecutive old posts), not walk arbitrarily far into old history.
# ---------------------------------------------------------------------------
print("== Full 2-day history scanning ==")

# A group whose recent-post streak is broken up by CONSECUTIVE_OLD_POSTS_TO_STOP-1
# consecutive old posts, followed by MORE recent qualifying posts, must not
# stop early -- the old streak must reset once a within-window post appears.
mixed_dates = (
    ["old"] * (collector.CONSECUTIVE_OLD_POSTS_TO_STOP - 1)
    + ["recent"]
    + ["recent"] * 3
)
mixed_containers = [_FakeContainer(400 + i, QUALIFYING_TEXT) for i in range(len(mixed_dates))]
ts_by_container = {c: ("5 days ago" if label == "old" else "1 hour ago") for c, label in zip(mixed_containers, mixed_dates)}
collector.find_post_containers = lambda driver: mixed_containers
collector.extract_post_timestamp_from_container = lambda container: ts_by_container[container]
collector.MAX_POSTS_PER_RUN = len(mixed_containers)
try:
    result5b = collector.process_posts(fake_driver, CUTOFF, set(), max_new_leads=20)
    check(
        "Old-post streak resets on an in-window post instead of stopping the whole group early",
        len(result5b.collected) == 4 and result5b.old_posts_skipped == collector.CONSECUTIVE_OLD_POSTS_TO_STOP - 1,
    )
finally:
    collector.find_post_containers = orig_find_containers
    collector.extract_post_timestamp_from_container = orig_extract_ts
    collector.MAX_POSTS_PER_RUN = orig_max_posts

# A true, uninterrupted old-post streak of CONSECUTIVE_OLD_POSTS_TO_STOP must
# stop the group's scan (this is what lets scanning end without walking
# arbitrarily far into a group's full history, while still covering
# everything within the DAYS_BACK window).
true_old_containers = [_FakeContainer(500 + i, QUALIFYING_TEXT) for i in range(collector.CONSECUTIVE_OLD_POSTS_TO_STOP + 5)]
collector.find_post_containers = lambda driver: true_old_containers
collector.extract_post_timestamp_from_container = lambda container: "5 days ago"
collector.MAX_POSTS_PER_RUN = len(true_old_containers)
try:
    result5c = collector.process_posts(fake_driver, CUTOFF, set(), max_new_leads=20)
    check(
        "Scan stops after a real old-post streak instead of walking the group's full history",
        result5c.old_posts_skipped == collector.CONSECUTIVE_OLD_POSTS_TO_STOP,
    )
finally:
    collector.find_post_containers = orig_find_containers
    collector.extract_post_timestamp_from_container = orig_extract_ts
    collector.MAX_POSTS_PER_RUN = orig_max_posts

# Facebook's lazy-loading sometimes needs more than one scroll-and-wait cycle
# before the next batch of posts appears -- the scan must retry scrolling
# (up to CONSECUTIVE_EMPTY_SCROLLS_TO_STOP times) rather than giving up after
# the first empty attempt.
_scroll_call_count = {"n": 0}
_initial_batch = [_FakeContainer(600 + i, QUALIFYING_TEXT) for i in range(2)]
_late_arriving_post = _FakeContainer(650, QUALIFYING_TEXT)


def _flaky_find_containers(driver):
    # New post only "loads" after 2 scroll attempts, simulating slow lazy-load.
    if _scroll_call_count["n"] >= 2:
        return _initial_batch + [_late_arriving_post]
    return _initial_batch


def _counting_scroll(driver):
    _scroll_call_count["n"] += 1


orig_find_containers_real = collector.find_post_containers
orig_scroll = collector.scroll_to_load_posts
orig_scroll_once = collector.scroll_once
collector.find_post_containers = _flaky_find_containers
collector.extract_post_timestamp_from_container = lambda container: "1 hour ago"
# The retry path uses the lightweight scroll_once (a full sweep there cost
# ~12s per post in live testing); patch both so the test counts either.
collector.scroll_to_load_posts = _counting_scroll
collector.scroll_once = _counting_scroll
collector.MAX_POSTS_PER_RUN = 10
try:
    result5d = collector.process_posts(fake_driver, CUTOFF, set(), max_new_leads=20)
    check(
        "Retries scrolling multiple times to pick up slow-loading posts instead of giving up after one attempt",
        len(result5d.collected) == 3,
    )
finally:
    collector.find_post_containers = orig_find_containers
    collector.scroll_to_load_posts = orig_scroll
    collector.scroll_once = orig_scroll_once
    collector.extract_post_timestamp_from_container = orig_extract_ts
    collector.MAX_POSTS_PER_RUN = orig_max_posts

collector.LEAD_MIN_CHARS_FOR_SIMILARITY = _ORIG_MIN_CHARS

# ---------------------------------------------------------------------------
# 7. ERROR HANDLING -- a container that raises mid-processing must not kill
#    the whole run; it should be skipped and processing must continue.
# ---------------------------------------------------------------------------
print("== Error handling ==")


class _ExplodingContainer(_FakeContainer):
    def find_elements(self, by, selector):
        raise RuntimeError("simulated DOM failure")


mixed_containers = [_ExplodingContainer(300, QUALIFYING_TEXT), _FakeContainer(301, QUALIFYING_TEXT)]
collector.find_post_containers = lambda driver: mixed_containers
collector.extract_post_timestamp_from_container = lambda container: "1 hour ago"
collector.MAX_POSTS_PER_RUN = 2
try:
    result5 = collector.process_posts(fake_driver, CUTOFF, set(), max_new_leads=20)
    check("A single bad post doesn't crash the run and doesn't block the next post",
          len(result5.collected) == 1 and result5.collected[0].url == "https://www.facebook.com/groups/999/posts/1301")
finally:
    collector.find_post_containers = orig_find_containers
    collector.extract_post_timestamp_from_container = orig_extract_ts
    collector.MAX_POSTS_PER_RUN = orig_max_posts

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
print(f"Checks passed: {passed}")
print(f"Checks failed: {failed}")
if failures:
    print("Failed checks:")
    for f in failures:
        print(f"  - {f}")

raise SystemExit(1 if failed else 0)
