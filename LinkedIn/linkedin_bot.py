# LeadForge · asbaq000
"""
LinkedIn video-editing job collector.

    python linkedin_bot.py                          full run
    python linkedin_bot.py --test "video editor"    discovery only

Finds recent video-editing hiring posts on LinkedIn and logs the
qualifying ones to a Google Sheet.

--test runs only the search/scroll/collect stage for one keyword and
prints the URLs it finds. It opens no posts, touches no Google Sheet,
and does not count against the daily caps - so it is safe to repeat
while checking that discovery works.
"""

import json
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gspread
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ============================================================
# CONFIGURATION - everything you'd want to change lives here
# ============================================================

ROOT = Path(__file__).parent

KEYWORDS_FILE = ROOT / "keywords.txt"

# Your service-account JSON.
GOOGLE_CREDENTIALS = ROOT / "credentials.json"

# Persistent Chromium profile, so the LinkedIn login survives runs.
PROFILE_DIRECTORY = ROOT / "linkedin_profile"

RUN_STATE_FILE = ROOT / "run_state.json"
KEYWORD_ROTATION_FILE = ROOT / "keyword_rotation.json"

# Debug dumps (HTML + screenshot) land here when discovery finds nothing.
DEBUG_DIRECTORY = ROOT / "debug"

GOOGLE_SHEET_NAME = "linkedin_job"
GOOGLE_WORKSHEET_NAME = "Results"

# Set to False to skip Sheets entirely and print results to the
# terminal instead - useful while debugging the scraper itself.
USE_GOOGLE_SHEETS = True


# ------------------------------------------------------------
# Job requirements
# ------------------------------------------------------------

# Only accept posts from the last N hours.
MAX_AGE_HOURS = 72

# Minimum long-form compensation, in $/minute-of-footage. No maximum.
LONG_FORM_MIN = 3.50

# Minimum Shorts compensation, in $/short (or /reel). No maximum.
SHORT_MIN = 10.00

# When True, a post whose rate IS stated but falls below the minimum is
# rejected. Posts with no stated rate are always kept and logged as
# "Not stated" either way - a missing price is not a disqualifier.
ENFORCE_RATE_MINIMUM = False


# Time-unit conversion, for posts quoting a rate per hour/day/week/
# month instead of per minute (e.g. "$50/hour"). These get converted to
# an equivalent $/minute figure so they can be checked against
# LONG_FORM_MIN on the same basis.
#
# Day/week/month assume a standard working schedule (8h/day, 5
# days/week, ~4.33 weeks/month) since posts never state actual hours -
# treat converted figures as estimates, not exact rates.

MINUTES_PER_HOUR = 60
WORK_HOURS_PER_DAY = 8
WORK_DAYS_PER_WEEK = 5
WORK_WEEKS_PER_MONTH = 4.33

MINUTES_PER_DAY = WORK_HOURS_PER_DAY * MINUTES_PER_HOUR
MINUTES_PER_WEEK = WORK_DAYS_PER_WEEK * MINUTES_PER_DAY
MINUTES_PER_MONTH = WORK_WEEKS_PER_MONTH * MINUTES_PER_WEEK

TIME_UNIT_TO_MINUTES = {
    "minute": 1,
    "hour": MINUTES_PER_HOUR,
    "day": MINUTES_PER_DAY,
    "week": MINUTES_PER_WEEK,
    "month": MINUTES_PER_MONTH,
}


# ------------------------------------------------------------
# Daily limits - these keep LinkedIn activity looking human.
# Raising them raises the odds of a challenge or a restricted account.
# ------------------------------------------------------------

MAX_RUNS_PER_DAY = 2
MAX_VIEWS_PER_DAY = 100

# How many keywords one run processes. Rotation picks up the rest next run.
MAX_KEYWORDS_PER_RUN = 1

# Total candidate URLs across ALL keywords in one run - not per keyword.
MAX_CANDIDATES_PER_RUN = 100

# Upper bound on candidates collected for a single keyword.
MAX_POSTS_PER_KEYWORD = 100


# ------------------------------------------------------------
# Pacing (seconds)
# ------------------------------------------------------------

MIN_DELAY = 8
MAX_DELAY = 18

POST_MIN_DELAY = 3
POST_MAX_DELAY = 7

SEARCH_MIN_DELAY = 3
SEARCH_MAX_DELAY = 6


# ------------------------------------------------------------
# Scrolling
# ------------------------------------------------------------

MAX_SCROLL_ROUNDS = 15

# Stop after this many consecutive rounds producing neither new page
# height nor new post URLs.
MAX_STALLED_ROUNDS = 3

# How long to wait (seconds) for lazy-loaded posts after each scroll.
SCROLL_SETTLE_TIMEOUT = 6


# ------------------------------------------------------------
# Challenge / soft-block detection
#
# Phrases LinkedIn shows on security-check and rate-limit screens.
# These often render as an overlay WITHOUT changing the URL, so a
# URL-only check (login/checkpoint/authwall) misses them.
# ------------------------------------------------------------

CHALLENGE_PHRASES = [
    "let's do a quick security check",
    "unusual activity",
    "verify it's you",
    "we've restricted",
    "please verify",
    "help us protect the linkedin community",
    "confirm you're not a robot",
]


# ============================================================
# RUN / VIEW STATE (resets daily)
# ============================================================

def _load_state():
    today = datetime.now().date().isoformat()
    default = {"date": today, "runs": 0, "views": 0}

    if not RUN_STATE_FILE.exists():
        return default

    try:
        state = json.loads(RUN_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        # A damaged state file should not block execution.
        return default

    if state.get("date") != today:
        return default

    state.setdefault("runs", 0)
    state.setdefault("views", 0)

    return state


def _save_state(state):
    RUN_STATE_FILE.write_text(json.dumps(state, indent=4), encoding="utf-8")


def can_run_today():
    return _load_state()["runs"] < MAX_RUNS_PER_DAY


def register_run():
    state = _load_state()
    state["runs"] += 1
    _save_state(state)


def views_remaining_today():
    return max(0, MAX_VIEWS_PER_DAY - _load_state()["views"])


def register_view():
    state = _load_state()
    state["views"] += 1
    _save_state(state)
    return state["views"]


def show_run_status():
    state = _load_state()
    print(f"Runs today: {state['runs']}/{MAX_RUNS_PER_DAY}")
    print(f"Post views today: {state['views']}/{MAX_VIEWS_PER_DAY}")


# ============================================================
# KEYWORDS + ROTATION (rotation is continuous, not reset daily)
# ============================================================

def load_keywords():
    if not KEYWORDS_FILE.exists():
        print(f"ERROR: {KEYWORDS_FILE} does not exist.")
        return []

    lines = KEYWORDS_FILE.read_text(encoding="utf-8").splitlines()
    keywords = [line.strip() for line in lines if line.strip()]

    # Drop duplicates, preserving order.
    return list(dict.fromkeys(keywords))


def get_rotation_start_index(total_keywords):
    if total_keywords <= 0:
        return 0

    try:
        state = json.loads(
            KEYWORD_ROTATION_FILE.read_text(encoding="utf-8")
        )
        next_index = state.get("next_index", 0)
    except Exception:
        next_index = 0

    # Modulo in case keywords.txt shrank or grew since this was saved.
    return next_index % total_keywords


def save_rotation_progress(next_index, total_keywords):
    if total_keywords <= 0:
        return

    KEYWORD_ROTATION_FILE.write_text(
        json.dumps({"next_index": next_index % total_keywords}, indent=4),
        encoding="utf-8",
    )


# ============================================================
# GOOGLE SHEETS
# ============================================================

SHEET_HEADERS = [
    "Post URL",
    "Keyword",
    "Posted",
    "Job Type",
    "Rate",
    "Post Text",
    "Found At",
]


def connect_sheet():
    """Return the worksheet, or None when Sheets is disabled."""

    if not USE_GOOGLE_SHEETS:
        print()
        print(
            "Google Sheets disabled in config - results will print to "
            "the terminal only."
        )
        return None

    print()
    print("Connecting to Google Sheets...")

    if not GOOGLE_CREDENTIALS.exists():
        raise FileNotFoundError(
            f"{GOOGLE_CREDENTIALS} not found. Save your service account "
            f"JSON there, then share the sheet '{GOOGLE_SHEET_NAME}' "
            f"with that account's client_email as an Editor."
        )

    client = gspread.service_account(filename=str(GOOGLE_CREDENTIALS))
    spreadsheet = client.open(GOOGLE_SHEET_NAME)

    try:
        worksheet = spreadsheet.worksheet(GOOGLE_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        print(f"Worksheet '{GOOGLE_WORKSHEET_NAME}' not found - creating it.")
        worksheet = spreadsheet.add_worksheet(
            title=GOOGLE_WORKSHEET_NAME, rows=1000, cols=10
        )
        worksheet.append_row(SHEET_HEADERS)

    print("Google Sheets connected.")

    return worksheet


def get_existing_urls(worksheet):
    if worksheet is None:
        return set()

    print("Loading existing URLs...")

    urls = set()

    for row in worksheet.get_all_records():
        url = str(row.get("Post URL", "")).strip()
        if url:
            urls.add(url)

    print(f"Existing URLs: {len(urls)}")

    return urls


def save_result(worksheet, result):
    found_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    if worksheet is None:
        print()
        print("-" * 40)
        print("[TERMINAL-ONLY - Sheets disabled]")
        print(f"URL:      {result['url']}")
        print(f"Keyword:  {result['keyword']}")
        print(f"Posted:   {result['posted']}")
        print(f"Job type: {result['job_type']}")
        print(f"Rate:     {result['rate']}")
        print(f"Found at: {found_at}")
        print(f"Text:     {result['text'][:300]}...")
        print("-" * 40)
        return

    worksheet.append_row([
        result["url"],
        result["keyword"],
        result["posted"],
        result["job_type"],
        result["rate"],
        result["text"][:1000],
        found_at,
    ])

    print("Saved to Google Sheet.")


def explain_sheet_error(error):
    """
    Print something actually useful for a Sheets failure.

    A bare print(error) can collapse to something opaque like
    "<Response [200]>" for some gspread exception types, so print the
    traceback plus the underlying HTTP response when one is attached -
    that is the part that explains what went wrong.
    """

    print()
    print("Google Sheets error:")

    traceback.print_exc()

    response = getattr(error, "response", None)

    if response is not None:
        print()
        print(f"HTTP status: {getattr(response, 'status_code', '?')}")
        try:
            print("Response body:", response.text)
        except Exception:
            pass

    message = str(error).lower()

    if "invalid_grant" in message or "account not found" in message:
        print()
        print(
            "This usually means the service account (or its Google Cloud "
            "project) no longer exists, or the JSON key was revoked. "
            "Generate a fresh key and save it as credentials.json."
        )
    elif "spreadsheetnotfound" in type(error).__name__.lower():
        print()
        print(
            f"The service account cannot see a spreadsheet named exactly "
            f"'{GOOGLE_SHEET_NAME}'. Check the spelling and share the "
            f"sheet with the client_email in credentials.json."
        )


# ============================================================
# TEXT HELPERS
# ============================================================

def normalize_text(text):
    if not text:
        return ""

    return re.sub(r"\s+", " ", text).strip()


# ============================================================
# JOB TYPE
# ============================================================

SHORTS_TERMS = [
    "shorts",
    "short form",
    "short-form",
    "short form video",
    "short-form video",
    "reels",
    "instagram reels",
    "tiktok",
    "tiktok videos",
]

LONG_FORM_TERMS = [
    "long form",
    "long-form",
    "long form video",
    "long-form video",
    "long video",
    "long videos",
    "youtube video",
    "youtube videos",
    "youtube channel",
    "podcast",
    "podcast video",
    "podcast videos",
]


def detect_job_type(text):
    """Return "Shorts", "Long-form", or None."""

    text = text.lower()

    # Shorts first, because a post can mention both "video" and "shorts".
    if any(term in text for term in SHORTS_TERMS):
        return "Shorts"

    if any(term in text for term in LONG_FORM_TERMS):
        return "Long-form"

    return None


# ============================================================
# VIDEO EDITING DETECTION
# ============================================================

EDITING_TERMS = [
    "video editor",
    "video editing",
    "youtube editor",
    "youtube video editor",
    "long form editor",
    "long-form editor",
    "long form video editor",
    "long-form video editor",
    "short form editor",
    "short-form editor",
    "shorts editor",
    "reels editor",
    "tiktok editor",
    "podcast editor",
    "video post-production",
    "video post production",
    "edit videos",
    "editing videos",
    "editor for youtube",
    "youtube editing",
]

HIRING_TERMS = [
    "hiring",
    "looking for",
    "looking to hire",
    "need an editor",
    "need a video editor",
    "editor wanted",
    "seeking an editor",
    "we're looking for",
    "we are looking for",
    "looking for an editor",
    "looking for a video editor",
    "need someone",
    "looking for someone",
    "want to hire",
    "we need an editor",
]


def is_video_editing_job(text):
    text = text.lower()

    has_editing = any(term in text for term in EDITING_TERMS)
    has_hiring = any(term in text for term in HIRING_TERMS)

    return has_editing and has_hiring


# ============================================================
# EXCLUDE SPECIALIZED GRAPHICS / ANIMATION
# ============================================================

EXCLUDED_TERMS = [
    # Motion graphics
    "motion graphics artist",
    "motion graphics designer",
    "motion graphic designer",
    "motion designer",
    "motion design",
    "motion graphics job",
    "motion graphics work",
    # 2D
    "2d animator",
    "2d animation",
    "2d artist",
    "2d motion graphics",
    # 3D
    "3d animator",
    "3d animation",
    "3d artist",
    "3d motion graphics",
    "3d modeling",
    "3d modelling",
    # VFX
    "vfx artist",
    "vfx designer",
    "vfx editor",
    "visual effects artist",
    "visual effects designer",
    # Character animation
    "character animator",
    "character animation",
    # CGI
    "cgi artist",
    "cgi animator",
    # Specific 3D tools
    "blender artist",
    "blender animator",
    "cinema 4d",
    "cinema 4d artist",
    "houdini artist",
    # Dedicated animation roles
    "animation specialist",
    "animation designer",
    "3d modeling artist",
    "3d modelling artist",
]


def is_complex_graphics_job(text):
    text = text.lower()

    return any(term in text for term in EXCLUDED_TERMS)


# ============================================================
# RATE PARSER
# ============================================================

# Digits with optional thousands separators / decimal.
NUM = r"(\d[\d,]*(?:\.\d+)?)"

# Non-minute time units, for rates quoted per hour/day/week/month.
TIME_UNIT = r"(hrs?|hours?|days?|weeks?|months?)"


def _parse_number(value):
    # Numbers may carry thousands separators, e.g. "$1,200 for 300 minutes".
    return float(value.replace(",", ""))


def _normalize_time_unit(raw):
    raw = raw.lower()

    if raw in ("hr", "hrs", "hour", "hours"):
        return "hour"
    if raw in ("day", "days"):
        return "day"
    if raw in ("week", "weeks"):
        return "week"
    if raw in ("month", "months"):
        return "month"

    return None


def _extract_long_form_rate(text):
    # $3-$5/min | $3-5/min | $3 to $5 per minute
    #
    # A range. There is no maximum cap on pricing, so what matters is
    # that the FLOOR of the stated range clears the minimum - use the
    # low end as the qualifying rate. The high end is kept for display.
    match = re.search(
        r"\$" + NUM + r"\s*(?:-|to)\s*\$?" + NUM
        + r"\s*(?:/|per|a)\s*(?:min|minute|minutes)\b",
        text,
    )
    if match:
        low = _parse_number(match.group(1))
        high = _parse_number(match.group(2))
        return low, f"${low:.2f}-${high:.2f}/min"

    # $5/min | $5/minute
    match = re.search(r"\$" + NUM + r"\s*/\s*(?:min|minute|minutes)\b", text)
    if match:
        rate = _parse_number(match.group(1))
        return rate, f"${rate:.2f}/min"

    # $5 per minute | $5 a minute
    match = re.search(
        r"\$" + NUM + r"\s+(?:per|a)\s+(?:min|minute|minutes)\b", text
    )
    if match:
        rate = _parse_number(match.group(1))
        return rate, f"${rate:.2f}/min"

    # $40-$60/hour | $500-700/week
    # A range priced per hour/day/week/month, converted to $/minute.
    match = re.search(
        r"\$" + NUM + r"\s*(?:-|to)\s*\$?" + NUM
        + r"\s*(?:/|per|a)\s*" + TIME_UNIT + r"\b",
        text,
    )
    if match:
        unit = _normalize_time_unit(match.group(3))
        if unit:
            unit_minutes = TIME_UNIT_TO_MINUTES[unit]
            low = _parse_number(match.group(1))
            high = _parse_number(match.group(2))
            low_per_min = low / unit_minutes
            high_per_min = high / unit_minutes
            return (
                low_per_min,
                f"${low:.2f}-${high:.2f}/{unit} "
                f"(~${low_per_min:.2f}-${high_per_min:.2f}/min)",
            )

    # $50/hour | $500/week - converted to $/minute.
    match = re.search(
        r"\$" + NUM + r"\s*(?:/|per|a)\s*" + TIME_UNIT + r"\b", text
    )
    if match:
        unit = _normalize_time_unit(match.group(2))
        if unit:
            unit_minutes = TIME_UNIT_TO_MINUTES[unit]
            stated = _parse_number(match.group(1))
            rate = stated / unit_minutes
            return rate, f"${stated:.2f}/{unit} (~${rate:.2f}/min)"

    # $350 / 70 minutes
    match = re.search(
        r"\$" + NUM + r"\s*/\s*" + NUM + r"\s*(?:min|minute|minutes)\b", text
    )
    if match:
        total = _parse_number(match.group(1))
        minutes = _parse_number(match.group(2))
        if minutes > 0:
            return total / minutes, f"${total / minutes:.2f}/min"

    # $350 for 70 minutes
    match = re.search(
        r"\$" + NUM + r".{0,60}?" + NUM + r"\s*(?:min|minute|minutes)\b", text
    )
    if match:
        total = _parse_number(match.group(1))
        minutes = _parse_number(match.group(2))
        if minutes > 0:
            return total / minutes, f"${total / minutes:.2f}/min"

    return None, None


def _extract_shorts_rate(text):
    # "reel(s)" is accepted alongside "short(s)" - Instagram Reels work
    # is one of the things the reels keywords are meant to catch, and
    # posts price it per-reel.

    # $10-$15/short | $10-15/reel | $10 to $15 per short
    match = re.search(
        r"\$" + NUM + r"\s*(?:-|to)\s*\$?" + NUM
        + r"\s*(?:/|per|a)\s*(?:shorts?|reels?)\b",
        text,
    )
    if match:
        low = _parse_number(match.group(1))
        high = _parse_number(match.group(2))
        return low, f"${low:.2f}-${high:.2f}/short"

    # $10/short | $10/reel
    match = re.search(r"\$" + NUM + r"\s*/\s*(?:shorts?|reels?)\b", text)
    if match:
        rate = _parse_number(match.group(1))
        return rate, f"${rate:.2f}/short"

    # $10 per short | $10 a reel
    match = re.search(
        r"\$" + NUM + r"\s+(?:per|a)\s+(?:shorts?|reels?)\b", text
    )
    if match:
        rate = _parse_number(match.group(1))
        return rate, f"${rate:.2f}/short"

    # $300 for 20 shorts
    match = re.search(
        r"\$" + NUM + r".{0,60}?" + NUM + r"\s*(?:shorts?|reels?)\b", text
    )
    if match:
        total = _parse_number(match.group(1))
        number = _parse_number(match.group(2))
        if number > 0:
            return total / number, f"${total / number:.2f}/short"

    return None, None


def extract_rate(text, job_type):
    """Return (numeric_rate, display_string), or (None, None)."""

    text = normalize_text(text.lower())

    if job_type == "Long-form":
        return _extract_long_form_rate(text)

    if job_type == "Shorts":
        return _extract_shorts_rate(text)

    return None, None


def rate_meets_minimum(rate, job_type):
    """
    True if the post clears the pay bar.

    A post with no stated rate always passes - a missing price is not a
    disqualifier, it just gets logged as "Not stated".
    """

    if rate is None or not ENFORCE_RATE_MINIMUM:
        return True

    if job_type == "Long-form":
        return rate >= LONG_FORM_MIN

    if job_type == "Shorts":
        return rate >= SHORT_MIN

    return True


# ============================================================
# POST AGE
# ============================================================

def _relative_date_from(text, now):
    # "2 hours ago" / "3 days ago"
    match = re.search(
        r"\b(\d+)\s+(minute|minutes|hour|hours|day|days)\s+ago\b", text
    )
    if match:
        number = int(match.group(1))
        unit = match.group(2)

        if "minute" in unit:
            return now - timedelta(minutes=number)
        if "hour" in unit:
            return now - timedelta(hours=number)
        if "day" in unit:
            return now - timedelta(days=number)

    # LinkedIn's compact formats: 5m, 2h, 1d
    match = re.search(r"\b(\d+)\s*([mhd])\b", text)
    if match:
        number = int(match.group(1))
        unit = match.group(2)

        if unit == "m":
            return now - timedelta(minutes=number)
        if unit == "h":
            return now - timedelta(hours=number)
        if unit == "d":
            return now - timedelta(days=number)

    if "just now" in text:
        return now

    return None


def extract_relative_date(text):
    text = text.lower()
    now = datetime.now(timezone.utc)

    # The real timestamp sits in the post header, near the top of the
    # extracted text. Look there first: scanning the whole body can
    # match an unrelated "3 d" or "2 hours ago" from the post copy or a
    # comment and produce a bogus age.
    return _relative_date_from(text[:400], now) or _relative_date_from(text, now)


def is_recent(post_date):
    if not post_date:
        return False

    age = datetime.now(timezone.utc) - post_date

    return age.total_seconds() <= MAX_AGE_HOURS * 3600


# ============================================================
# POST DISCOVERY
#
# ------------------------------------------------------------
# WHY THIS WAS REWRITTEN
# ------------------------------------------------------------
# The previous version discovered posts by CLICKING each post's
# relative-timestamp text ("20m", "3h"), reading the resulting URL,
# then calling page.go_back() to return to the results and scroll for
# more. That design cannot work, for four compounding reasons:
#
# 1. go_back() destroys the feed. LinkedIn re-renders search results
#    from scratch, scrolled back to the top with only the first batch
#    of posts mounted. So every scroll performed at the end of a round
#    was immediately undone by the first go_back() of the next round.
#    The loop scrolled forever and never reached deeper posts.
#
# 2. The per-round index reset to 0, so each round re-clicked the same
#    first posts. They deduped out against the already-collected list,
#    the "new this round" count stayed 0, and the loop concluded there
#    was nothing left to find.
#
# 3. The timestamp span is not itself a link in the current markup.
#    Clicking it - even with force=True - fires an event that navigates
#    nowhere, so page.url never changed and no URL was ever recorded.
#    Each round still burned one click plus a 1.5-3s sleep per post.
#
# 4. page.mouse.wheel() scrolls whatever is under the mouse cursor,
#    which sits at (0, 0) - outside the feed - so often nothing
#    scrolled at all. One 600-1000px wheel per round is also less than
#    a single LinkedIn post card's height.
#
# ------------------------------------------------------------
# WHAT IT DOES NOW
# ------------------------------------------------------------
# Discovery never navigates. LinkedIn still carries each post's
# urn:li:activity:<id> / urn:li:ugcPost:<id> identifier inside the
# rendered page - in embedded JSON payloads and element attributes -
# even though it no longer renders a plain permalink anchor. Those IDs
# are harvested straight out of the DOM and turned into permalinks.
#
# Because nothing navigates, scrolling and collecting are no longer in
# conflict: scroll, harvest, scroll again, accumulating as it goes. The
# loop stops on a real signal - the page stopped growing AND no new IDs
# appeared, for several rounds running.
# ============================================================

# Every LinkedIn post identifier shape that appears in search results
# markup. Both map onto the same /feed/update/ permalink.
URN_PATTERN = re.compile(r"urn:li:(activity|ugcPost):(\d{6,})")


# JS that pulls candidate identifiers out of the live DOM. Runs in the
# page so the whole serialized HTML never has to cross the wire, and so
# attribute values and anchor hrefs are read post-render.
HARVEST_SCRIPT = """
() => {
    const found = [];

    // 1. Anchors, when LinkedIn does render one.
    document.querySelectorAll(
        "a[href*='/feed/update/'], a[href*='/posts/']"
    ).forEach(a => found.push(a.getAttribute('href') || ''));

    // 2. Attributes that carry the URN directly.
    document.querySelectorAll(
        '[data-urn], [data-id], [data-activity-urn], [data-chameleon-result-urn]'
    ).forEach(el => {
        for (const attr of el.attributes) {
            if (attr.value && attr.value.indexOf('urn:li:') !== -1) {
                found.push(attr.value);
            }
        }
    });

    // 3. Embedded JSON payloads (LinkedIn ships post data in these
    //    <code> blocks even when the markup exposes no permalink).
    document.querySelectorAll(
        'code, script[type="application/json"]'
    ).forEach(el => {
        const t = el.textContent || '';
        if (t.indexOf('urn:li:') !== -1) found.push(t);
    });

    return found;
}
"""


def harvest_post_ids(page):
    """Return ordered, de-duplicated post IDs currently in the DOM."""

    try:
        chunks = page.evaluate(HARVEST_SCRIPT)
    except Exception as error:
        print("  Harvest failed, falling back to page HTML:", error)
        try:
            chunks = [page.content()]
        except Exception:
            return []

    ids = []
    seen = set()

    for chunk in chunks:
        for _kind, post_id in URN_PATTERN.findall(chunk or ""):
            if post_id not in seen:
                seen.add(post_id)
                ids.append(post_id)

    return ids


def build_post_url(post_id):
    return f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}/"


# Post cards can expose NO static permalink at all - confirmed via a
# raw page dump: no data-urn, no /posts/ or /feed/update/ hrefs, no
# embedded JSON containing a urn anywhere in the rendered markup, even
# though real matching posts are clearly there. Clicking the visible
# timestamp text was tried as a fallback, but it turned out to sit
# inside the SAME clickable region as the author's profile link, so
# it opens their profile instead of the post - worse than useless,
# since it silently produces wrong URLs rather than none at all.
#
# The reliable fix: LinkedIn's own page still has to FETCH this data
# from somewhere to render it - its frontend calls its own internal
# "Voyager"/GraphQL API and gets back JSON containing the real post
# URNs, then renders that into the stripped-down markup above. Reading
# those network responses directly (the same technique other LinkedIn
# scraping tools use) sidesteps the markup entirely: no clicking, no
# navigating away, nothing that can land on the wrong page.
def attach_urn_capture(page, collected):
    """
    Start capturing post URNs from LinkedIn's own internal API calls.

    `collected` is a set that response bodies' URNs get added to as
    they arrive - reading it directly (no polling needed) reflects
    whatever has been captured so far. Returns a handler to pass to
    page.remove_listener("response", handler) once done.

    DEBUG: every response that matches the URL/content-type filter
    gets appended to debug/api_responses.jsonl (url + a body snippet)
    regardless of whether a URN was found in it, so the actual traffic
    can be inspected directly instead of guessing at why nothing
    matched.
    """

    def handler(response):
        try:
            url = response.url

            # Deliberately NOT filtering by "voyager"/"graphql" in the
            # URL - that filter was hiding the real search-content
            # endpoint entirely, with zero visibility into what it
            # actually was. Content-type is enough to skip images/
            # fonts/css while still catching any JSON API response,
            # whatever its URL looks like.
            content_type = response.headers.get("content-type", "")

            if "json" not in content_type:
                return

            body = response.text()
        except Exception:
            return

        found = URN_PATTERN.findall(body or "")

        for _kind, post_id in found:
            collected.add(post_id)

        try:
            DEBUG_DIRECTORY.mkdir(exist_ok=True)

            with open(
                DEBUG_DIRECTORY / "api_responses.jsonl",
                "a",
                encoding="utf-8",
            ) as file:
                file.write(
                    json.dumps(
                        {
                            "url": url,
                            "urns_found": len(found),
                            "body_snippet": (body or "")[:3000],
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass

    page.on("response", handler)

    return handler


# Confirmed necessary (not hypothetical): for this account/page
# variant, neither the DOM/embedded-JSON harvest above nor LinkedIn's
# own network traffic exposes a post identifier ANYWHERE - checked
# directly, zero matches in either, even with an unfiltered capture of
# every JSON response. The "..." control-menu button is explicitly
# scoped to one post ("Open control menu for post by <author>"),
# unlike the timestamp text, which turned out to share a click target
# with the author's profile link and silently opens the wrong page.
def discover_post_urls_by_copy_link(page, already_seen, limit):
    """
    Open each visible post's "..." menu and use "Copy link to post" to
    reveal its real URL - read back via an intercepted clipboard
    write (see launch_browser's init script) rather than by
    navigating anywhere, so nothing can land on the wrong page.

    `already_seen` is every URL already collected this keyword - used
    only to skip duplicates, not mutated.
    """

    new_urls = []
    processed = 0

    while len(already_seen) + len(new_urls) < limit:

        try:
            menu_buttons = page.get_by_role(
                "button",
                name=re.compile(r"open control menu for post", re.I),
            ).all()
        except Exception:
            break

        if processed >= len(menu_buttons):
            # Nothing new to open without scrolling for more first.
            break

        button = menu_buttons[processed]
        processed += 1

        print(f"    Post {processed}/{len(menu_buttons)}: opening menu...")

        try:
            page.evaluate("window.__lastCopiedLink = null")

            button.scroll_into_view_if_needed(timeout=3000)
            button.click(timeout=3000, force=True)

            copy_item = page.get_by_text(
                re.compile(r"copy link to post", re.I)
            ).first

            copy_item.wait_for(timeout=3000)
            copy_item.click(timeout=3000, force=True)

            page.wait_for_timeout(500)

            copied = page.evaluate("window.__lastCopiedLink")
        except Exception as error:
            print(f"      Failed: {error}")

            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

            continue

        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        if (
            copied
            and "linkedin.com" in copied
            and copied not in already_seen
            and copied not in new_urls
        ):
            print(f"      Got: {copied}")
            new_urls.append(copied)
        else:
            print(f"      No usable link (got: {copied!r}).")

        time.sleep(random.uniform(1.0, 2.0))

    return new_urls


def _page_height(page):
    try:
        return page.evaluate(
            "() => Math.max(document.body.scrollHeight, "
            "document.documentElement.scrollHeight)"
        )
    except Exception:
        return 0


def _click_show_more(page):
    try:
        button = page.get_by_role(
            "button",
            name=re.compile(
                r"show more results|see more results|load more", re.I
            ),
        ).first

        if button.count() > 0 and button.is_visible():
            button.click(timeout=3000)
            time.sleep(random.uniform(1.5, 3.0))
    except Exception:
        pass


def scroll_once(page):
    """
    Scroll to the bottom and wait for lazily-loaded posts to mount.

    Returns True if the document actually grew, i.e. more results were
    appended. Uses window.scrollTo rather than mouse.wheel so it does
    not depend on where the (invisible) cursor happens to sit.
    """

    before = _page_height(page)

    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    except Exception:
        pass

    # A keyboard End press nudges LinkedIn's own scroll listeners, which
    # sometimes ignore a programmatic scrollTo alone.
    try:
        page.keyboard.press("End")
    except Exception:
        pass

    # Some result pages paginate with an explicit button instead.
    _click_show_more(page)

    # Poll for growth rather than sleeping a fixed amount - a fixed
    # sleep is either wasteful or too short depending on connection.
    deadline = time.time() + SCROLL_SETTLE_TIMEOUT

    while time.time() < deadline:
        time.sleep(0.5)
        if _page_height(page) > before:
            # Let the rest of the batch finish rendering.
            time.sleep(random.uniform(1.0, 2.0))
            return True

    return False


def save_debug_dump(page, keyword):
    """Dump the rendered page so a dry run can be inspected directly."""

    DEBUG_DIRECTORY.mkdir(exist_ok=True)

    safe = re.sub(r"[^a-z0-9]+", "_", keyword.lower()).strip("_")[:40]

    try:
        html_path = DEBUG_DIRECTORY / f"search_{safe}.html"
        html_path.write_text(page.content(), encoding="utf-8")

        png_path = DEBUG_DIRECTORY / f"search_{safe}.png"
        page.screenshot(path=str(png_path), full_page=True)

        print(f"  Saved debug dump: {html_path.name}, {png_path.name}")
    except Exception as error:
        print("  Could not save debug dump:", error)


def page_has_challenge(page):
    """True if LinkedIn is showing a security / verification wall."""

    try:
        visible_text = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        # If the body cannot even be read, treat it as blocked, to be safe.
        return True

    return any(phrase in visible_text for phrase in CHALLENGE_PHRASES)


def search_linkedin(page, keyword, budget=None):
    """
    Search `keyword` and return candidate post permalinks.

    `budget` caps how many URLs to collect. Raises
    RuntimeError("linkedin_challenge_detected") on a security challenge.
    """

    limit = min(
        MAX_POSTS_PER_KEYWORD,
        budget if budget is not None else MAX_POSTS_PER_KEYWORD,
    )

    encoded_keyword = keyword.replace(" ", "%20")

    # sortBy=date_posted biases results toward recent posts instead of
    # LinkedIn's default relevance ranking, which matters given the
    # freshness filter. If LinkedIn renames the param it is ignored
    # silently and results fall back to relevance - not an error.
    search_url = (
        "https://www.linkedin.com/search/results/content/"
        f"?keywords={encoded_keyword}"
        "&origin=GLOBAL_SEARCH_HEADER"
        "&sortBy=date_posted"
    )

    print()
    print("=" * 60)
    print(f"Searching keyword: {keyword}")
    print("=" * 60)

    # Attach BEFORE navigating - the first batch of API responses fires
    # during/right after the initial page load, and a listener attached
    # afterward would miss them.
    captured_urns = set()
    response_handler = attach_urn_capture(page, captured_urns)

    try:

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as error:
            print("Search navigation error:", error)
            return []

        time.sleep(random.uniform(SEARCH_MIN_DELAY, SEARCH_MAX_DELAY))

        current_url = page.url.lower()

        if any(x in current_url for x in ("login", "checkpoint", "authwall")):
            print("LinkedIn requires authentication.")
            return []

        if page_has_challenge(page):
            print(
                "LinkedIn showed a security/verification challenge on the "
                "search page. Stopping this run rather than hammering it."
            )
            raise RuntimeError("linkedin_challenge_detected")

        # Search results render client-side after the shell loads, so a
        # fixed sleep is unreliable - on a slow load the cards have not
        # mounted yet and discovery reports a false "0 candidates". Wait
        # for the results container instead.
        try:
            page.locator("main").first.wait_for(timeout=15000)
            page.wait_for_timeout(2000)
        except PlaywrightTimeoutError:
            print("Results container did not appear within 15s.")

        # --------------------------------------------------------
        # Scroll-and-harvest loop. No navigation happens here, so scroll
        # position is preserved across rounds and the feed keeps growing.
        # --------------------------------------------------------

        urls = []
        seen_ids = set()
        stalled_rounds = 0

        for round_number in range(1, MAX_SCROLL_ROUNDS + 1):

            new_this_round = 0

            # Network-captured URNs first - the primary, most durable
            # source. This is passive (nothing gets clicked or
            # navigated), so it can never land on the wrong page, and
            # it reflects LinkedIn's own structured data rather than
            # whatever markup happens to be rendered right now.
            for post_id in list(captured_urns):
                if post_id in seen_ids:
                    continue

                seen_ids.add(post_id)
                urls.append(build_post_url(post_id))
                new_this_round += 1

                if len(urls) >= limit:
                    break

            # DOM/embedded-JSON harvest as a secondary source, in case
            # a page variant DOES expose something the network capture
            # missed (e.g. a response that arrived before this round's
            # listener processed it).
            if len(urls) < limit:

                for post_id in harvest_post_ids(page):
                    if post_id in seen_ids:
                        continue

                    seen_ids.add(post_id)
                    urls.append(build_post_url(post_id))
                    new_this_round += 1

                    if len(urls) >= limit:
                        break

            # Last resort: use LinkedIn's own "Copy link to post"
            # action per visible post. Confirmed necessary for this
            # account/page variant - both sources above find nothing
            # here, checked directly.
            if len(urls) < limit:

                copied_urls = discover_post_urls_by_copy_link(
                    page, urls, limit
                )

                for copied_url in copied_urls:
                    urls.append(copied_url)
                    new_this_round += 1

            print(
                f"Round {round_number}/{MAX_SCROLL_ROUNDS}: "
                f"{len(urls)} candidate URLs (+{new_this_round} new)"
            )

            if len(urls) >= limit:
                print(f"Reached the {limit}-candidate budget for this keyword.")
                break

            grew = scroll_once(page)

            # A round only counts as stalled when BOTH signals are dead:
            # the page did not grow and no new IDs were harvested.
            # Either one alone is normal - LinkedIn sometimes appends a
            # batch that is entirely posts already seen, and sometimes
            # renders new posts without the document getting taller.
            if not grew and new_this_round == 0:
                stalled_rounds += 1
                print(
                    f"  No new content "
                    f"({stalled_rounds}/{MAX_STALLED_ROUNDS})."
                )

                if stalled_rounds >= MAX_STALLED_ROUNDS:
                    print("  End of results reached.")
                    break
            else:
                stalled_rounds = 0

            time.sleep(random.uniform(1.5, 3.5))

        if not urls:
            print("0 candidates found - saving a debug dump for inspection.")
            save_debug_dump(page, keyword)

        return urls

    finally:

        try:
            page.remove_listener("response", response_handler)
        except Exception:
            pass


# ============================================================
# BROWSER
# ============================================================

def launch_browser(playwright):
    """Launch a persistent Chromium context and return (context, page)."""

    print()
    print("Opening Chromium...")

    context = playwright.chromium.launch_persistent_context(
        str(PROFILE_DIRECTORY),
        headless=False,
        # Playwright's default fingerprint (the AutomationControlled
        # blink feature, a visible navigator.webdriver flag) is one of
        # the things sites check when detecting bots - disable it.
        #
        # Deliberately NOT overriding user_agent: a hardcoded UA string
        # drifts out of sync with the bundled Chromium version, and a
        # UA-vs-real-capabilities mismatch is itself a strong bot signal
        # - worse than Playwright's accurate default.
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1366, "height": 768},
    )

    # navigator.webdriver still reads true even with the blink flag
    # disabled in some Chromium builds - patch it on every page.
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', "
        "{get: () => undefined});"
    )

    # Captures whatever LinkedIn's own "Copy link to post" action
    # writes to the clipboard, without needing clipboard-read
    # permissions (which would prompt) - just shadow writeText and
    # stash the value on window instead of actually touching the
    # system clipboard.
    context.add_init_script(
        "window.__lastCopiedLink = null;"
        "if (navigator.clipboard && navigator.clipboard.writeText) {"
        "  navigator.clipboard.writeText = (text) => {"
        "    window.__lastCopiedLink = text;"
        "    return Promise.resolve();"
        "  };"
        "}"
    )

    page = context.pages[0] if context.pages else context.new_page()

    return context, page


def is_logged_in(page):
    # LinkedIn's logged-out homepage does NOT redirect to a "/login"
    # URL - it renders an embedded sign-in form (email/password inputs)
    # directly at https://www.linkedin.com/. A URL-only check misses
    # this, so look for that form too.
    try:
        if page.locator("input#session_key").count() > 0:
            return False
    except Exception:
        pass

    return True


def ensure_logged_in(page):
    """
    Return True if the session is usable.

    An unattended run (Task Scheduler / cron) has nobody to press ENTER,
    so blocking on input() there would hang forever with the task stuck
    "running". Detect that and fail loudly instead.
    """

    try:
        page.goto(
            "https://www.linkedin.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
    except Exception as error:
        print("Could not open LinkedIn:", error)
        return False

    time.sleep(3)

    current_url = page.url.lower()

    needs_login = (
        any(x in current_url for x in ("login", "authwall", "checkpoint"))
        or not is_logged_in(page)
    )

    if not needs_login:
        return True

    print()
    print("=" * 60)
    print("LOGIN REQUIRED")

    if not sys.stdin.isatty():
        print(
            "No interactive terminal (this looks like a scheduled run) - "
            "cannot prompt for manual login."
        )
        print(
            "The saved LinkedIn session has likely expired. Run this "
            "script manually once to log in again, then scheduled runs "
            "will resume working."
        )
        print("=" * 60)
        return False

    print("Log into LinkedIn normally in the browser.")
    print("When you are done, come back to this terminal.")
    print("=" * 60)

    input("\nPress ENTER after login...")

    return True


def read_post(page, url):
    """Open a post and return its normalized text, or None."""

    try:
        print("Opening post...")

        page.goto(url, wait_until="domcontentloaded", timeout=30000)

        time.sleep(random.uniform(POST_MIN_DELAY, POST_MAX_DELAY))

        current_url = page.url.lower()

        if any(x in current_url for x in ("checkpoint", "login", "authwall")):
            print("LinkedIn requires authentication/checkpoint.")
            return None

        if page_has_challenge(page):
            print(
                "LinkedIn showed a security/verification challenge on "
                "this post. Stopping."
            )
            raise RuntimeError("linkedin_challenge_detected")

        # Scope to the post itself, not the whole page. A body-level
        # inner_text() pulls in the global nav, both rails, and the
        # comments - which corrupts job-type detection, date extraction
        # and rate extraction with text from unrelated content. Try the
        # post container first; fall back to <main>, which at least
        # excludes the nav and left rail.
        try:
            post_element = page.locator("article, [data-urn]").first
            post_text = post_element.inner_text(timeout=10000)
        except Exception:
            post_text = page.locator("main").first.inner_text(timeout=10000)

        return normalize_text(post_text)

    except PlaywrightTimeoutError:
        print("Post timed out.")
        return None

    except RuntimeError:
        raise

    except Exception as error:
        print("Post reading error:", error)
        return None


# ============================================================
# PROCESS ONE POST
# ============================================================

def process_post(page, url, keyword):
    """Read and filter one post. Returns a result dict, or None."""

    print()
    print("-" * 60)
    print(f"Checking: {url}")

    text = read_post(page, url)

    register_view()

    if not text:
        print("Rejected: could not read post.")
        return None

    if not is_video_editing_job(text):
        print("Rejected: not clearly a video-editing hiring post.")
        return None

    if is_complex_graphics_job(text):
        print("Rejected: specialized animation/motion/VFX work.")
        return None

    job_type = detect_job_type(text)

    if not job_type:
        print("Rejected: job type could not be determined.")
        return None

    post_date = extract_relative_date(text)

    if not post_date:
        print("Rejected: post age could not be determined.")
        return None

    if not is_recent(post_date):
        print(f"Rejected: older than {MAX_AGE_HOURS} hours.")
        return None

    rate, rate_string = extract_rate(text, job_type)

    if not rate_meets_minimum(rate, job_type):
        print(f"Rejected: rate {rate_string} is below the minimum.")
        return None

    if rate is None:
        rate_string = "Not stated"

    print()
    print("*" * 60)
    print("QUALIFIED POST")
    print(f"Keyword: {keyword}")
    print(f"Type:    {job_type}")
    print(f"Rate:    {rate_string}")
    print(f"Posted:  {post_date.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"URL:     {url}")
    print("*" * 60)

    return {
        "url": url,
        "keyword": keyword,
        "posted": post_date.strftime("%Y-%m-%d %H:%M UTC"),
        "job_type": job_type,
        "rate": rate_string,
        "text": text,
    }


def process_candidates(page, urls, keyword, worksheet, existing_urls):
    """Read each candidate URL and save whatever qualifies."""

    # Track whether this batch is entirely posts already saved - if so,
    # we have caught up with previously-seen content and can stop early
    # instead of re-checking known posts every run.
    consecutive_known = 0

    for index, url in enumerate(urls, start=1):

        if views_remaining_today() <= 0:
            print("Daily post-view cap reached. Stopping this keyword.")
            return False

        print()
        print(f"Candidate {index}/{len(urls)}")

        if url in existing_urls:
            print("Already saved. Skipping.")
            consecutive_known += 1

            if consecutive_known >= 10:
                print(
                    "Hit 10 already-known posts in a row - assuming we "
                    "have caught up with previously-seen content for "
                    "this keyword. Moving on."
                )
                return True

            continue

        consecutive_known = 0

        result = process_post(page, url, keyword)

        if result:
            try:
                save_result(worksheet, result)
                existing_urls.add(result["url"])
            except Exception as error:
                explain_sheet_error(error)

        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    return True


# ============================================================
# KEYWORD LOOP
# ============================================================

def run_keywords(page, keywords, worksheet, existing_urls):
    """Work through the keyword rotation for this run."""

    total_keywords = len(keywords)

    # Rotate rather than always starting at index 0 - with a large
    # keyword list the daily view cap gets used up before the later
    # keywords are ever reached, so each run picks up where the last one
    # stopped and wraps around.
    start_index = get_rotation_start_index(total_keywords)

    rotated_indices = (
        list(range(start_index, total_keywords))
        + list(range(0, start_index))
    )

    print()
    print(
        f"Keyword rotation starting at index {start_index}: "
        f"'{keywords[start_index]}'"
    )

    keywords_processed = 0
    candidates_found = 0

    for position, index in enumerate(rotated_indices, start=1):

        keyword = keywords[index]

        if views_remaining_today() <= 0:
            print()
            print("Daily post-view cap reached. Stopping keyword loop.")
            return

        if keywords_processed >= MAX_KEYWORDS_PER_RUN:
            print()
            print(
                f"Reached the {MAX_KEYWORDS_PER_RUN} keyword(s)-per-run "
                f"limit. Remaining keywords will be picked up next run."
            )
            return

        remaining_budget = MAX_CANDIDATES_PER_RUN - candidates_found

        if remaining_budget <= 0:
            print()
            print(
                f"Hit the {MAX_CANDIDATES_PER_RUN} candidates-per-run "
                f"budget. Stopping."
            )
            return

        # Persist rotation progress BEFORE processing, so a run that dies
        # partway (challenge, cap, crash) resumes after this keyword
        # instead of retrying it forever.
        save_rotation_progress(index + 1, total_keywords)
        keywords_processed += 1

        print()
        print("=" * 60)
        print(f"KEYWORD {position}/{total_keywords} (index {index})")
        print(keyword)
        print("=" * 60)

        try:
            post_urls = search_linkedin(page, keyword, budget=remaining_budget)
        except RuntimeError:
            print(
                "Challenge detected during search. Ending the run - the "
                "persistent profile keeps the session, so just re-run later."
            )
            return
        except Exception as error:
            print("Search error:", error)
            continue

        candidates_found += len(post_urls)

        print()
        print(f"Found {len(post_urls)} candidate URLs.")

        try:
            keep_going = process_candidates(
                page, post_urls, keyword, worksheet, existing_urls
            )
        except RuntimeError:
            print("Challenge detected while reading a post. Ending the run.")
            return

        if not keep_going:
            return

        if position < total_keywords:
            print()
            print("Waiting before the next keyword...")
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


# ============================================================
# ENTRY POINTS
# ============================================================

# Keep diagnostic runs short and light - just enough to confirm
# discovery works, not a full-scale scrape. Each attempt opens a
# menu and clicks around, which adds up fast against a live account.
TEST_DISCOVERY_BUDGET = 5


def test_discovery(keyword):
    """
    Discovery-only smoke test: search, scroll, print URLs.

    Opens no posts, touches no sheet, ignores the daily caps. Capped
    at TEST_DISCOVERY_BUDGET candidates so a diagnostic run stays
    short instead of working through the full per-keyword limit.
    """

    print()
    print("=" * 60)
    print(f"DISCOVERY TEST: {keyword}")
    print(f"(capped at {TEST_DISCOVERY_BUDGET} candidates)")
    print("=" * 60)

    with sync_playwright() as playwright:
        context, page = launch_browser(playwright)

        try:
            if not ensure_logged_in(page):
                return

            urls = search_linkedin(
                page, keyword, budget=TEST_DISCOVERY_BUDGET
            )

            print()
            print("=" * 60)
            print(f"RESULT: {len(urls)} candidate URLs")
            print("=" * 60)

            for index, url in enumerate(urls, start=1):
                print(f"{index:3}. {url}")

            if not urls:
                print()
                print(
                    "Nothing found. Check debug/search_*.html - if the "
                    "dump contains no 'urn:li:activity:' text at all, the "
                    "session is probably logged out or LinkedIn served a "
                    "challenge page."
                )

        finally:
            if sys.stdin.isatty():
                print()
                print("Press ENTER to close the browser...")
                input()

            context.close()


def main():
    print()
    print("=" * 60)
    print("LINKEDIN VIDEO JOB COLLECTOR")
    print("=" * 60)

    show_run_status()

    if not can_run_today():
        print()
        print(
            f"Maximum of {MAX_RUNS_PER_DAY} runs already reached today. "
            f"Exiting."
        )
        return

    if views_remaining_today() <= 0:
        print()
        print(
            "Daily post-view cap already reached. Exiting without opening "
            "any posts."
        )
        return

    keywords = load_keywords()

    if not keywords:
        print("No keywords found.")
        return

    print()
    print(f"Loaded {len(keywords)} keywords:")
    for keyword in keywords:
        print(f"  - {keyword}")

    try:
        worksheet = connect_sheet()
        existing_urls = get_existing_urls(worksheet)
    except Exception as error:
        explain_sheet_error(error)
        return

    # Setup succeeded, so this run is really going to touch LinkedIn -
    # only now does it count against the daily run cap. A config failure
    # should not burn one of the daily runs.
    register_run()
    print("Run registered.")

    with sync_playwright() as playwright:

        try:
            context, page = launch_browser(playwright)
        except Exception as error:
            print("Could not launch browser:", error)
            return

        try:
            if not ensure_logged_in(page):
                return

            run_keywords(page, keywords, worksheet, existing_urls)

        finally:
            # Keep the browser open for interactive runs so the results
            # can be reviewed. Scheduled runs must still close it - a
            # lingering browser locks the persistent profile directory
            # and blocks the next run from starting.
            if sys.stdin.isatty():
                print()
                print(
                    "Run finished. Browser stays open - press ENTER here "
                    "to close it."
                )
                input()

            context.close()

    print()
    print("=" * 60)
    print("FINISHED")
    print("=" * 60)

    show_run_status()



# LeadForge: editable local settings
import json as _lf_json
from pathlib import Path as _lf_Path
_lf_settings = _lf_Path(__file__).parent / "dashboard.config.json"
if _lf_settings.exists():
    for _lf_key, _lf_value in _lf_json.loads(_lf_settings.read_text(encoding="utf-8")).items():
        if _lf_key in ('GOOGLE_SHEET_NAME', 'GOOGLE_WORKSHEET_NAME', 'USE_GOOGLE_SHEETS', 'MAX_AGE_HOURS', 'LONG_FORM_MIN', 'SHORT_MIN', 'ENFORCE_RATE_MINIMUM', 'MAX_RUNS_PER_DAY', 'MAX_VIEWS_PER_DAY', 'MAX_KEYWORDS_PER_RUN', 'MAX_CANDIDATES_PER_RUN', 'MAX_POSTS_PER_KEYWORD', 'MIN_DELAY', 'MAX_DELAY', 'EDITING_TERMS', 'HIRING_TERMS', 'EXCLUDED_TERMS'):
            if type(_lf_value) is not type(globals()[_lf_key]):
                raise ValueError(f"Invalid type for {_lf_key} in dashboard.config.json")
            globals()[_lf_key] = _lf_value

if __name__ == "__main__":

    if "--test" in sys.argv:
        position = sys.argv.index("--test")
        keyword = " ".join(sys.argv[position + 1:]).strip() or "video editor"
        test_discovery(keyword)
    else:
        main()
