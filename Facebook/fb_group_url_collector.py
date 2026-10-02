#!/usr/bin/env python3
"""
fb_group_url_collector.py

Collects Facebook POST URLs (only) from a Facebook Group page that the user
has already opened and manually logged into, in their own Chrome browser.

SAFETY / SCOPE (read before modifying):
  - This script never logs into Facebook. It never sees, asks for, or stores
    a Facebook email/password, and it never creates or persists Facebook
    cookies of any kind.
  - It attaches Selenium to a Chrome instance the USER already started and
    authenticated by hand (via Chrome's --remote-debugging-port). If no such
    session exists, the script simply fails to connect -- it never tries to
    open a login form or fill one in.
  - It does not attempt to defeat CAPTCHAs, 2FA, or any other Facebook
    security/anti-bot mechanism, and it does not try to hide the fact that
    it is automation.
  - It only reads posts already rendered in the DOM of the page the user has
    open, plus modest auto-scrolling to let the group's normal lazy-loading
    fire. It never navigates itself into a group the user hasn't opened.
  - Facebook does not expose real post permalinks as static hrefs (confirmed
    via live testing): the visible timestamp link resolves only to the bare
    group URL. To read the permalink, this performs an ordinary left click
    on that already-visible element -- the same click a real user would
    make -- then returns to the group feed via browser back navigation.
    This is normal, visible interaction with content already on the page,
    not a bypass of any login/CAPTCHA/anti-bot mechanism, and it is capped
    by MAX_POSTS_PER_RUN per run.
  - It collects post permalink URLs (and, optionally, the post's displayed
    timestamp). It never extracts names, profile URLs, emails, comments, or
    any other personal information.
  - It also does light best-effort parsing of each post's OWN text (topic,
    niche/hashtags, budget, and -- if the poster explicitly included one in
    their OWN post text, for people to contact them -- a phone number). This
    only ever reads the post's own visible text, never a profile, comment,
    or other field. Phone numbers embedded in Topic/Niche/Budget text are
    still redacted from those specific fields to keep them readable; the
    number itself is captured once, in the "Has Contact Info" column.

Run this only against your own Facebook account and groups you already
belong to, in line with Facebook's Terms of Service and applicable law.
"""

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Set
from urllib.parse import urlparse, urlunparse, parse_qs

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

DAYS_BACK = 2                       # Only keep posts newer than this many days.

SHEET_NAME = "Lead Scraper"         # Google Sheet (spreadsheet) name.
TAB_NAME = "Facebook URLs"          # Worksheet (tab) name inside that sheet.

# Preferred over SHEET_NAME when set: the spreadsheet key from its URL
# (https://docs.google.com/spreadsheets/d/<KEY>/edit). Opening by key is
# unambiguous -- it doesn't depend on the file's title, and it works when
# several sheets share a name.
SHEET_KEY = "1MsRNteY3tq7Uv3BJFsYeaq9vypPQ4q0QrDjbhRC_mjY"

SERVICE_ACCOUNT_FILE = "service_account.json"
SEEN_FILE = "seen_fb_urls.json"

# CROSS-GROUP DEDUPLICATION
# People cross-post the same request into many groups, which produces
# different post URLs for what is really ONE lead. URL dedup alone can't
# catch that, so each saved lead also stores a signature (normalized post
# text + any phone number) and a new lead matching a stored signature is
# skipped no matter which group it came from.
LEAD_SIGNATURE_FILE = "seen_lead_signatures.json"
# How alike two posts must read to count as the same lead. 0.90 tolerates
# small edits between reposts without merging genuinely different requests.
LEAD_SIMILARITY_THRESHOLD = 0.90
LEAD_SIGNATURE_CHARS = 240
# Below this many characters a post is too generic to compare safely -- two
# different people both posting "Hiring Video Editors" are two real leads,
# so short posts are only ever deduped by an exact phone-number match.
LEAD_MIN_CHARS_FOR_SIMILARITY = 40
LOG_FILE = "fb_urls_scraper.log"

# Hard cap on new leads saved to Google Sheets per calendar day. Tracked in
# DAILY_QUOTA_FILE as {"date": "YYYY-MM-DD", "count": N}; the count resets
# automatically the first time the script runs on a new calendar day --
# there is no separate "9 AM" trigger inside the script itself. If you want
# it to actually run every day at 9 AM, schedule `python
# fb_group_url_collector.py` with Windows Task Scheduler (or cron) for that
# time; this quota just makes repeated/early runs on the same day safe.
MAX_LEADS_PER_RUN = 20
DAILY_QUOTA_FILE = "daily_lead_quota.json"

# How many times to scroll the page to trigger Facebook's own lazy loading,
# and how long to pause between scrolls (seconds) to let posts render.
SCROLL_ROUNDS = 3
SCROLL_PAUSE_SECONDS = 1.0

# Chrome remote-debugging port. The user must start Chrome themselves with:
#   chrome.exe --remote-debugging-port=9222 --user-data-dir="<their normal profile dir>"
# and log into Facebook by hand in that window. This script only *attaches*
# to that already-running, already-authenticated browser.
CHROME_DEBUGGER_ADDRESS = "127.0.0.1:9222"

# Facebook does not expose real post permalinks as static hrefs (confirmed
# via live testing) -- only clicking the timestamp reveals it. Each click
# navigates away and back, so this is a hard safety ceiling on how many
# posts a single group visit will click through, to bound worst-case run
# time. In normal operation this is NOT what stops a group's scan -- that's
# governed by CONSECUTIVE_OLD_POSTS_TO_STOP below, which detects once the
# post feed has scrolled past the DAYS_BACK window and stops there, so a
# run effectively covers the group's FULL available post history within
# that window, not just the first few posts.
MAX_POSTS_PER_RUN = 60
CLICK_NAV_TIMEOUT_SECONDS = 4

# How many scroll rounds to retry, back-to-back, when the page has no more
# unprocessed posts loaded yet -- Facebook's lazy-loading sometimes needs
# more than one scroll-and-wait cycle to fetch the next batch. Only after
# this many consecutive empty attempts is a group considered fully scanned.
CONSECUTIVE_EMPTY_SCROLLS_TO_STOP = 3

# Once this many posts IN A ROW are older than the DAYS_BACK cutoff, assume
# the feed has been scrolled past the recent-posts window and stop scanning
# this group -- this is what lets a run cover a group's full available
# 2-day history without also walking arbitrarily far into old history.
CONSECUTIVE_OLD_POSTS_TO_STOP = 8

# The sheet is meant for leads (people looking to hire), not other
# freelancers advertising themselves. When True, posts whose text looks
# like a service-provider pitch are skipped (see classify_post_intent).
# This is a best-effort keyword heuristic, not a guarantee -- ambiguous
# posts are kept rather than dropped, to avoid losing real leads.
EXCLUDE_SELLER_POSTS = True

# Only keep buyer posts that want remote/freelance work, not on-site/
# in-person/full-time roles. Same "keep if unclear" philosophy as above --
# a post that doesn't mention work arrangement at all is kept, since most
# posts in a freelance group are implicitly remote/freelance already.
EXCLUDE_ONSITE_POSTS = True

# Stricter lead qualification: only keep posts that (a) clearly read as
# someone looking to hire (not just "unknown"/ambiguous), and (b) actually
# mention video-editing context (video/editor/reels/TikTok/DaVinci/Premiere/
# After Effects/etc.), on top of the remote/freelance check above. This
# trades recall for precision -- ambiguous posts like a bare name or an
# unrelated comment are now dropped instead of kept, since the goal is a
# clean list of real video-editing leads, not "everything that might be one".
REQUIRE_CLEAR_BUYER_INTENT = True
REQUIRE_VIDEO_EDITING_CONTEXT = True

# ---------------------------------------------------------------------------
# GROQ (NICHE DETECTION ONLY)
#
# Groq is used for ONE narrow job: reading a qualifying post and naming the
# content niche it is about (e.g. "gaming", "real estate", "podcast"), for
# the existing Niche column. It deliberately does NOT decide whether a post
# is a lead, and does not touch budget, phone, dates, or dedup -- all of
# that stays with the existing, live-verified rules. If the key is blank,
# the API errors, or the call times out, the run silently falls back to the
# previous hashtag-based niche and everything else behaves identically.
# ---------------------------------------------------------------------------

# >>> PUT YOUR GROQ API KEY HERE <<<
GROQ_API_KEY = __import__("os").environ.get("GROQ_API_KEY", "")

GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TIMEOUT_SECONDS = 15
GROQ_ENABLED = True          # set False to disable Groq entirely
GROQ_MAX_NICHE_CHARS = 60    # keep the Niche cell short and sheet-friendly

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_HEADERS = ["Topic", "Niche", "URL", "Budget", "Number", "Post Date and Time", "Has Contact Info"]


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Configure logging to both console and LOG_FILE with the required format."""
    logger = logging.getLogger("fb_group_url_collector")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Facebook post text routinely contains characters outside the Windows
    # console's default cp1252 codepage (combining marks, emoji, non-Latin
    # scripts). Without an explicit UTF-8 reconfigure, logging a post's text
    # raises UnicodeEncodeError and kills the run.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# DATA MODEL
# ---------------------------------------------------------------------------

@dataclass
class CollectedPost:
    url: str
    post_date: Optional[datetime]  # None only if we could not parse it (post is skipped before this is used)
    topic: Optional[str] = None    # best-effort first line/sentence of the post's own text
    niche: Optional[str] = None    # best-effort hashtags found in the post's own text
    budget: Optional[str] = None   # best-effort currency amount found in the post's own text
    phone_number: Optional[str] = None  # phone number the poster explicitly included in their OWN post text, if any -- never from a profile or comment


# ---------------------------------------------------------------------------
# TIMESTAMP PARSING
# ---------------------------------------------------------------------------

_RELATIVE_PATTERNS = [
    # (regex, unit-in-minutes multiplier or special handling)
    (re.compile(r"^just now$", re.I), lambda m, now: now),
    (re.compile(r"^(\d+)\s*s(ec(ond)?s?)?$", re.I), lambda m, now: now - timedelta(seconds=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*m(in(ute)?s?)?$", re.I), lambda m, now: now - timedelta(minutes=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*minutes?\s+ago$", re.I), lambda m, now: now - timedelta(minutes=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*h(rs?|ours?)?$", re.I), lambda m, now: now - timedelta(hours=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*hours?\s+ago$", re.I), lambda m, now: now - timedelta(hours=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*d(ays?)?$", re.I), lambda m, now: now - timedelta(days=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*days?\s+ago$", re.I), lambda m, now: now - timedelta(days=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*w(eeks?)?$", re.I), lambda m, now: now - timedelta(weeks=int(m.group(1)))),
    (re.compile(r"^(\d+)\s*weeks?\s+ago$", re.I), lambda m, now: now - timedelta(weeks=int(m.group(1)))),
    (re.compile(r"^yesterday$", re.I), lambda m, now: now - timedelta(days=1)),
    (re.compile(r"^yesterday at (.+)$", re.I), None),  # handled specially below
]

# Absolute formats Facebook commonly shows (varies by locale/UI version).
_ABSOLUTE_FORMATS = [
    "%B %d, %Y at %I:%M %p",   # "August 10, 2026 at 3:45 PM"
    "%B %d at %I:%M %p",       # "August 10 at 3:45 PM" (current year assumed)
    "%b %d, %Y at %I:%M %p",   # "Aug 10, 2026 at 3:45 PM"
    "%b %d at %I:%M %p",       # "Aug 10 at 3:45 PM"
    "%B %d, %Y",                # "August 10, 2026"
    "%b %d, %Y",                 # "Aug 10, 2026"
    "%m/%d/%Y",
    "%Y-%m-%dT%H:%M:%S",        # ISO-ish, sometimes present in title/aria attrs
]


def parse_facebook_timestamp(raw_text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """
    Convert a Facebook-displayed timestamp string into a datetime.

    Supports relative forms ("5 minutes ago", "2 hrs", "yesterday", "3 d")
    and a handful of common absolute formats. Returns None if the text
    cannot be confidently parsed -- callers must skip the post in that case
    rather than guessing.
    """
    if not raw_text:
        return None

    text = raw_text.strip()
    if now is None:
        now = datetime.now()

    # "Yesterday at 3:45 PM" needs the time-of-day preserved.
    m = re.match(r"^yesterday at (.+)$", text, re.I)
    if m:
        time_part = m.group(1).strip()
        for fmt in ("%I:%M %p", "%H:%M"):
            try:
                t = datetime.strptime(time_part, fmt).time()
                base = now - timedelta(days=1)
                return datetime.combine(base.date(), t)
            except ValueError:
                continue
        # Couldn't parse the time portion confidently -- fall back to
        # "some time yesterday", which is good enough for a days-back filter.
        return now - timedelta(days=1)

    for pattern, handler in _RELATIVE_PATTERNS:
        if handler is None:
            continue
        m = pattern.match(text)
        if m:
            try:
                return handler(m, now)
            except Exception:
                return None

    for fmt in _ABSOLUTE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=now.year)
            return parsed
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# URL NORMALIZATION / VALIDATION
# ---------------------------------------------------------------------------

# Path shapes that indicate a genuine Facebook *post* permalink.
_POST_URL_PATTERNS = [
    re.compile(r"^/groups/[^/]+/posts/[^/]+"),
    re.compile(r"^/groups/[^/]+/permalink/[^/]+"),
    re.compile(r"^/permalink\.php$"),
    re.compile(r"^/story\.php$"),
    re.compile(r"^/[^/]+/posts/[^/]+"),  # page/profile posts, still a "post"
    # NOTE: /stories/ URLs are deliberately NOT accepted. Facebook Stories
    # are ephemeral and are not group posts; they must never be treated as
    # leads or written to the sheet. See _EXCLUDED_PATH_SNIPPETS below.
]

# Path/query shapes that must be rejected even if they superficially match
# a pattern above (comments, photos, reactions, profile/group landing pages).
_EXCLUDED_PATH_SNIPPETS = (
    "/stories/", "/story/",  # Facebook Stories are not posts -- never leads
    "/photo", "/photos/", "/videos/", "/media",
    "/comment/", "/reactions", "/friends/", "/about", "/members",
    "/user/", "/profile.php",
)


def normalize_and_validate_post_url(raw_url: str) -> Optional[str]:
    """
    Return a normalized canonical post URL, or None if raw_url does not look
    like a genuine Facebook post permalink (profile, group root, comment,
    photo, reaction, or other non-post link).
    """
    if not raw_url:
        return None

    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None

    if "facebook.com" not in parsed.netloc:
        return None

    path = parsed.path or ""

    for snippet in _EXCLUDED_PATH_SNIPPETS:
        if snippet in path:
            return None

    matched = any(p.match(path) for p in _POST_URL_PATTERNS)
    if not matched:
        return None

    # Keep only the query params that identify the post itself
    # (story_fbid/id for permalink.php / story.php); drop tracking params.
    query = parse_qs(parsed.query)
    keep = {}
    for key in ("story_fbid", "id"):
        if key in query:
            keep[key] = query[key][0]

    normalized_query = "&".join(f"{k}={v}" for k, v in sorted(keep.items()))

    normalized = urlunparse((
        "https",
        "www.facebook.com",
        path.rstrip("/"),
        "",
        normalized_query,
        "",
    ))
    return normalized


# ---------------------------------------------------------------------------
# BROWSER ATTACHMENT (no login automation, ever)
# ---------------------------------------------------------------------------

def attach_to_user_chrome():
    """
    Attach Selenium to a Chrome window the user already started and logged
    into manually via --remote-debugging-port. This function never opens a
    login page, never fills in credentials, and never touches Facebook
    cookies -- it only reuses the DOM of the tab the user already has open.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.debugger_address = CHROME_DEBUGGER_ADDRESS

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:
        log.error(
            "Could not attach to Chrome at %s. Make sure you started Chrome "
            "yourself with --remote-debugging-port=9222 and are logged into "
            "Facebook in that window. Underlying error: %s",
            CHROME_DEBUGGER_ADDRESS, exc,
        )
        raise
    return driver


def ensure_page_visible(driver) -> bool:
    """
    Make sure the page is actually VISIBLE to Chrome, restoring the window
    if it is minimized.

    Chrome throttles hidden/minimized pages: Facebook's feed then never
    renders past its first placeholder and its scroll-based pagination
    never fires, so a scrape silently finds zero posts in every group
    (confirmed via live testing -- with the window minimized the feed
    stayed at scrollHeight 1831 with 1 post; once visible it grew to
    13096 with posts loading continuously). Because that failure looks
    exactly like "these groups have no leads", this is checked explicitly
    and loudly rather than left to chance.

    Returns True if the page ended up visible.
    """
    try:
        driver.execute_cdp_cmd("Page.bringToFront", {})
    except Exception as exc:
        log.debug("Page.bringToFront unavailable: %s", exc)

    def visibility():
        try:
            return driver.execute_script("return document.visibilityState;")
        except Exception:
            return None

    if visibility() == "visible":
        return True

    # Still hidden -- the OS window itself is minimized. Restore it (Windows).
    if sys.platform == "win32":
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetForegroundWindow()
            title_buf = ctypes.create_unicode_buffer(512)
            # Find the Chrome window belonging to this automation session by
            # walking top-level windows for a Chrome-class window.
            SW_RESTORE = 9
            found = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def enum_cb(hwnd_, _lparam):
                cls = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetClassNameW(hwnd_, cls, 256)
                if cls.value == "Chrome_WidgetWin_1":
                    ctypes.windll.user32.GetWindowTextW(hwnd_, title_buf, 512)
                    if title_buf.value.strip():
                        found.append(hwnd_)
                return True

            ctypes.windll.user32.EnumWindows(enum_cb, None)
            for h in found:
                ctypes.windll.user32.ShowWindow(h, SW_RESTORE)
                ctypes.windll.user32.SetForegroundWindow(h)
            del hwnd
        except Exception as exc:
            log.debug("Could not restore the Chrome window automatically: %s", exc)

        try:
            driver.execute_cdp_cmd("Page.bringToFront", {})
        except Exception:
            pass

    if visibility() == "visible":
        return True

    log.warning(
        "The Chrome window appears MINIMIZED or hidden (document.visibilityState "
        "is not 'visible'). Chrome throttles hidden pages, so Facebook will not "
        "render or paginate group feeds and this run will find few or no posts. "
        "Please leave the debug Chrome window open and NOT minimized while the "
        "scraper runs."
    )
    return False


def detect_group_page(driver) -> bool:
    """
    Confirm the currently active tab is a Facebook Group page. Returns False
    (and logs guidance) if it is not -- the script must not try to navigate
    there itself.
    """
    try:
        current_url = driver.current_url
    except Exception as exc:
        log.error("Could not read the current page URL: %s", exc)
        return False

    log.info("Current page detected: %s", current_url)

    if "facebook.com" not in current_url:
        log.error("The active browser tab is not on facebook.com. "
                   "Please open your Facebook Group first.")
        return False

    if "/groups/" not in urlparse(current_url).path:
        log.error("The active tab does not look like a Facebook Group page "
                   "(no '/groups/' in the URL). Please open a Facebook Group "
                   "you are a member of, then re-run the collector.")
        return False

    return True


def scroll_once(driver) -> None:
    """
    A single gentle scroll step plus a short pause.

    Facebook virtualizes the group feed (only a couple of posts stay in the
    DOM at a time), so the "ran out of loaded posts" path in process_posts
    runs after almost every post. Doing a full SCROLL_ROUNDS sweep there
    cost ~12s per post and dominated run time; this keeps that path cheap
    while still letting the feed page in.
    """
    try:
        # Scroll the LAST rendered post to the bottom of the viewport. This
        # is what actually trips Facebook's IntersectionObserver-based
        # pagination -- a blind window.scrollBy can land in already-loaded
        # space and never trigger a fetch (confirmed live: scrollIntoView on
        # the last post grew the feed from 1831px to 13096px, while
        # scrollBy left it frozen).
        driver.execute_script("""
          const sel = '[data-ad-rendering-role="story_message"]';
          let nodes = document.querySelectorAll(sel);
          if (!nodes.length) nodes = document.querySelectorAll('div[role="article"]');
          if (nodes.length) nodes[nodes.length - 1].scrollIntoView({block: 'end'});
          else window.scrollTo(0, document.body.scrollHeight);
        """)
    except Exception as exc:
        log.debug("Light scroll failed: %s", exc)
    time.sleep(SCROLL_PAUSE_SECONDS)


def scroll_to_load_posts(driver) -> None:
    """
    Scroll the page so Facebook's own lazy-loading renders more posts.

    Scrolls by a fraction of the viewport rather than by
    document.body.scrollHeight: Facebook virtualizes the feed, rendering
    only posts near the viewport, so a full-page-height jump skips straight
    past posts that never get a chance to render (confirmed via live
    testing -- big jumps left the feed showing only loading skeletons).
    Smaller steps with a pause let each batch actually paint before moving
    on.
    """
    for i in range(SCROLL_ROUNDS):
        try:
            driver.execute_script("window.scrollBy(0, window.innerHeight * 0.8);")
        except Exception as exc:
            log.warning("Scroll attempt %d failed, continuing with posts loaded so far: %s", i + 1, exc)
            break
        time.sleep(SCROLL_PAUSE_SECONDS)
    log.info("Finished scrolling (%d rounds) to load additional posts.", SCROLL_ROUNDS)


# ---------------------------------------------------------------------------
# POST CONTENT PARSING (topic / niche / budget)
#
# These pull best-effort structure out of a post's OWN visible text -- public
# post content, not personal data about a person. As a safeguard against a
# poster's phone number leaking into these fields, PHONE_LIKE_RUN strips any
# digit run of 7+ digits before the text is used.
# ---------------------------------------------------------------------------

PHONE_LIKE_RUN = re.compile(r"\d[\d\-\s]{6,}\d")

_MONEY_AMOUNT = r"\d(?:[\d,]*\d)?(?:\.\d+)?"  # e.g. "5000", "5,000", "99.50" -- never ends on a bare separator
_BUDGET_PATTERN = re.compile(
    rf"(?:budget|price|rate)\s*[:\-]?\s*(?:\$|USD|PKR|Rs\.?|INR|₹)?\s?{_MONEY_AMOUNT}"
    rf"|(?:\$|USD|PKR|Rs\.?|INR|₹)\s?{_MONEY_AMOUNT}",
    re.I,
)

# A rate quoted PER MINUTE of finished video -- how this market usually
# prices work ("$500 per minute", "$100/minute", "100 per min",
# "500 per video minute", "400 per mint" [sic], "rate: 300/min").
# Checked BEFORE the generic budget pattern so the per-minute rate is what
# lands in the Budget column, not a bare number from elsewhere in the post.
_CURRENCY = r"(?:\$|USD|PKR|Rs\.?|INR|₹|£|€)"
_MINUTE_WORD = r"(?:minutes?|minute|mins?|min|mint|mnt)"
_PER_MINUTE_PATTERN = re.compile(
    # "$500 per minute" / "500 per video minute" / "100 / min" / "400 per mint"
    rf"{_CURRENCY}?\s?{_MONEY_AMOUNT}\s*(?:{_CURRENCY})?\s*(?:per|pr|/|a|each)\s*"
    rf"(?:finished\s+|video\s+|edited\s+)?{_MINUTE_WORD}\b"
    # ...or the rate stated first: "per minute rate 500", "rate per min: $300"
    rf"|(?:rate|budget|price|pay(?:ing)?)?\s*(?:per|pr|/)\s*{_MINUTE_WORD}\s*"
    rf"(?:rate|charges?)?\s*[:\-]?\s*{_CURRENCY}?\s?{_MONEY_AMOUNT}",
    re.I,
)


def extract_budget_per_minute(post_text: Optional[str]) -> Optional[str]:
    """
    A per-minute rate the poster stated in their OWN text, returned as they
    wrote it (e.g. "$500 per minute", "400 per mint"). None when the post
    doesn't mention one -- a rate is never inferred or converted.
    """
    if not post_text:
        return None
    m = _PER_MINUTE_PATTERN.search(_normalize_for_matching(post_text))
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(0)).strip()

_HASHTAG_PATTERN = re.compile(r"#\w+")


def _redact_phone_like(text: str) -> str:
    """Strip any 7+ digit run (phone-number shaped) out of extracted post text."""
    return PHONE_LIKE_RUN.sub("[redacted]", text)


def extract_post_text(container) -> Optional[str]:
    """
    Extract the post's OWN message text -- not comments, not UI chrome.

    Anchors on [data-ad-rendering-role="story_message"], which is where
    current Facebook renders a post's message (confirmed via live DOM
    inspection). Scoping to that element is what keeps comment text and
    button labels out of the classification input; the older
    [dir='auto'] fallback swept those in, so it is deliberately not used
    as a general fallback here.
    """
    from selenium.webdriver.common.by import By

    try:
        candidates = container.find_elements(By.CSS_SELECTOR, POST_MESSAGE_SELECTOR)
    except Exception:
        return None

    best = ""
    for el in candidates:
        try:
            text = (el.text or "").strip()
        except Exception:
            continue
        if len(text) > len(best):
            best = text

    if best:
        return best

    # Some groups don't render story_message at all (see the role="article"
    # fallback in _FIND_POST_CONTAINERS_JS). In that case the container IS
    # the post, so read it directly -- but strip the trailing engagement/
    # comment chrome so comment text never reaches the classifier.
    try:
        raw = (container.text or "").strip()
    except Exception:
        return None
    if not raw:
        return None

    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        # Everything from the reactions/comments bar onward belongs to other
        # people, not the poster -- stop there.
        if re.match(r"^(like|comment|share|send|reply|see more|view \d+ (more )?comments?"
                    r"|all comments|most relevant|top comments|\d+ comments?"
                    r"|write a comment)\b", stripped, re.I):
            break
        lines.append(stripped)

    return "\n".join(lines).strip() or None


def extract_topic(post_text: Optional[str]) -> Optional[str]:
    """First line/sentence of the post's own text, used as a rough 'topic'."""
    if not post_text:
        return None
    first_line = post_text.strip().splitlines()[0].strip()
    topic = first_line[:120]
    return _redact_phone_like(topic) if topic else None


def extract_niche(post_text: Optional[str]) -> Optional[str]:
    """Hashtags found in the post's own text, used as a rough 'niche' signal."""
    if not post_text:
        return None
    tags = _HASHTAG_PATTERN.findall(post_text)
    return ", ".join(dict.fromkeys(tags)) if tags else None


_GROQ_NICHE_PROMPT = (
    "You read a Facebook post from someone hiring a video editor and name the "
    "CONTENT NICHE of the videos they need edited.\n"
    "Reply with JSON only: {\"niche\": \"...\"}\n"
    "Rules:\n"
    "- The niche is the SUBJECT/GENRE of their videos (e.g. gaming, real estate, "
    "fitness, podcast, travel vlog, cooking, fashion, crypto, education, "
    "documentary, kids content, true crime, car reviews, wedding).\n"
    "- It may be stated indirectly -- infer it from what the videos are about.\n"
    "- It is NOT the platform (YouTube/TikTok/Reels), NOT the format "
    "(shorts/long-form), NOT the editing software, NOT the job title.\n"
    "- If the post does not indicate a niche, return {\"niche\": \"\"}. "
    "Never invent one.\n"
    "- Keep it under 5 words, lowercase."
)


def groq_detect_niche(post_text: Optional[str]) -> Optional[str]:
    """
    Ask Groq for the post's content niche. Used ONLY to fill the existing
    Niche column -- it never decides whether a post is a lead.

    Returns None on any problem (no key, network error, bad response, empty
    answer) so the caller can fall back to the hashtag-based niche. A niche
    is a nice-to-have; it must never be able to break a run.
    """
    if not (GROQ_ENABLED and GROQ_API_KEY and post_text):
        return None

    try:
        import requests

        resp = requests.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _GROQ_NICHE_PROMPT},
                    {"role": "user", "content": post_text[:4000]},
                ],
            },
            timeout=GROQ_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            log.warning("Groq niche lookup failed (HTTP %s): %s -- falling back to hashtags.",
                        resp.status_code, resp.text[:160])
            return None

        content = resp.json()["choices"][0]["message"]["content"]
        niche = (json.loads(content).get("niche") or "").strip()
    except Exception as exc:
        log.warning("Groq niche lookup errored (%s) -- falling back to hashtags.", exc)
        return None

    if not niche or niche.lower() in {"none", "n/a", "unknown", "unspecified", ""}:
        return None
    return niche[:GROQ_MAX_NICHE_CHARS]


def resolve_niche(post_text: Optional[str]) -> Optional[str]:
    """
    Niche for the sheet: Groq's read of the post if available, otherwise the
    hashtags already parsed from the post's own text.
    """
    return groq_detect_niche(post_text) or extract_niche(post_text)


def extract_budget(post_text: Optional[str]) -> Optional[str]:
    """
    Budget for the sheet. A per-minute rate wins when the post states one
    (that's how this market quotes work); otherwise fall back to any plain
    currency amount mentioned. None if the post names no figure at all --
    a budget is never guessed.
    """
    if not post_text:
        return None
    per_minute = extract_budget_per_minute(post_text)
    if per_minute:
        return _redact_phone_like(per_minute)
    m = _BUDGET_PATTERN.search(post_text)
    return _redact_phone_like(m.group(0).strip()) if m else None


_CONTACT_KEYWORD_PATTERN = re.compile(r"\bwhatsapp\b|\bwhats\s*app\b", re.I)

# A phone number, formatted the way people actually type them in posts:
# optional leading "+", then digits with optional spaces/dashes/dots/parens
# between groups. Matched separately from PHONE_LIKE_RUN (which is only
# used to redact phone-shaped runs out of Topic/Niche/Budget) so the actual
# number text can be captured here.
_PHONE_CANDIDATE_PATTERN = re.compile(r"\+?\d[\d\-\.\s\(\)]{6,16}\d")

# Currency/budget context words that, if found immediately before a
# digit-run, mean it's a price -- not a phone number -- and should not be
# captured as contact info.
_BUDGET_CONTEXT_BEFORE = re.compile(
    r"(budget|price|rate|cost|pay|paying|salary|rs\.?|pkr|inr|usd|₹|\$)\s*[:\-]?\s*$", re.I,
)


def extract_phone_number(post_text: Optional[str]) -> Optional[str]:
    """
    Best-effort extraction of a phone number the poster explicitly included
    in their OWN post text (never a profile, comment, or other field).
    Returns the number as displayed in the post, or None if no phone-shaped
    number is present. Digit-runs that look like a budget/price figure
    (preceded by "budget", "$", "Rs", etc.) are not treated as phone
    numbers.
    """
    if not post_text:
        return None

    for m in _PHONE_CANDIDATE_PATTERN.finditer(post_text):
        candidate = m.group(0)
        digit_count = sum(ch.isdigit() for ch in candidate)
        if not (7 <= digit_count <= 15):
            continue
        context_before = post_text[max(0, m.start() - 20):m.start()]
        if _BUDGET_CONTEXT_BEFORE.search(context_before):
            continue
        return candidate.strip()

    return None


# ---------------------------------------------------------------------------
# BUYER-VS-SELLER FILTER
#
# The sheet is meant for leads -- people looking to HIRE someone -- not for
# other freelancers advertising their own services. This is a best-effort
# keyword classifier over the post's own text; it will misclassify some
# posts (sarcasm, unusual phrasing, mixed posts). When a post's intent can't
# be told apart, it is kept rather than dropped, since silently discarding a
# real lead is worse than including an occasional ambiguous post.
# ---------------------------------------------------------------------------

_SGAP = r"[^.!?\n]{0,40}"  # tolerate "I'm Jay, a passionate video editor" (name/adjectives in between)

_SELLER_PATTERNS = [
    re.compile(rf"\bi\s*(am|'m)\b{_SGAP}\b(freelance\w*|video\s*editor|graphic\s*designer|designer|editor|expert|professional)\b", re.I),
    re.compile(r"\bavailable\s+(for|to)\s+(work|hire|freelance|projects?)\b", re.I),
    re.compile(r"\bhire\s+me\b", re.I),
    re.compile(r"\bmy\s+(portfolio|services?|work|rates?)\b", re.I),
    re.compile(r"\bi\s+(offer|provide|do|specialize\s+in)\b.*\b(editing|design|services?|graphics?|animation)\b", re.I),
    re.compile(r"\b(my\s+)?dms?\s+(are\s+)?(always\s+)?open\b", re.I),
    re.compile(r"\bi\s+can\s+(edit|design|help\s+you\s+with)\b", re.I),
    re.compile(r"\bopen\s+for\s+(work|projects?|freelance)\b", re.I),
    # Self-referential experience only ("I have 5 years experience"). A bare
    # "2 years experience" is just as often a BUYER stating what they require
    # of a candidate, so it is a weak signal -- see _WEAK_SELLER_PATTERNS.
    re.compile(r"\bi\s+(have|has|got)\b[^.!?\n]{0,30}\byears?\s+(of\s+)?experience\b", re.I),
    re.compile(r"\bfiverr\b|\bupwork\b", re.I),
    re.compile(r"\bprojects?\s+delivered\b", re.I),
    re.compile(r"\btop[\s\-]?rated\b", re.I),
    re.compile(r"\bi'?m\s+here\s+to\s+(save|help)\b", re.I),
    re.compile(r"\boffering\b[^.!?\n]{0,40}\b(editing|video\s*editing|design(ing)?)\b", re.I),
    re.compile(r"\bwe\s+(offer|provide)\b", re.I),
    re.compile(r"\bour\s+(agency|studio|team)\b", re.I),
    re.compile(r"\blooking\s+for\s+(new\s+)?clients?\b", re.I),
    re.compile(r"\bneed\s+(more\s+)?clients?\b", re.I),
    re.compile(r"\blooking\s+for\s+(a\s+)?job\b", re.I),
    re.compile(r"\bseeking\s+(a\s+)?job\b", re.I),
    re.compile(r"\bopen\s+to\s+work\b", re.I),
    re.compile(r"\blooking\s+for\s+work\b", re.I),
    # Rhetorical freelancer ad openers: they quote the BUYER's question and
    # then answer it with themselves. Live false positive that reached a CSV:
    # "LOOKING FOR A VIDEO EDITOR? YOU JUST FOUND ONE."
    re.compile(r"\byou\s+(just\s+)?found\s+(one|him|her|me|your)\b", re.I),
    re.compile(r"\blook\s+no\s+further\b", re.I),
    re.compile(r"\bsearch\s+(is\s+over|ends\s+here|no\s+more)\b", re.I),
    re.compile(r"\byour\s+search\s+(is\s+over|ends)\b", re.I),
    re.compile(r"\bi'?m\s+your\s+(guy|girl|person|editor|man)\b", re.I),
    re.compile(r"\bthat'?s\s+me\b", re.I),
]

# Bare role noun -- deliberately permissive on what comes before it (any
# characters, not just \w+ separated by spaces) since real posts use
# punctuation/slashes freely, e.g. "video creator/editor", "3D video
# editor". The trigger word (need/looking for/want/...) still has to occur
# BEFORE the role noun within a short window, so this stays anchored to
# genuine hiring language rather than matching anywhere in the post.
# Tolerates the misspellings people actually type -- "edittor", "editer",
# "editior", "vedio editor" (live: "I need video edittor" was being dropped
# purely because of the double-t).
_EDITOR_WORD = r"edit(?:t?or|t?er|ior|ors)"
_ROLE_NOUN = rf"(?:v[ie]d[ei]o\s*{_EDITOR_WORD}|{_EDITOR_WORD}|designer|creator|freelance\w*|expert)"
_GAP = r"[^.!?\n]{0,60}"  # up to 60 chars, not crossing a sentence boundary

# "need/want/required" in the languages these groups actually post in, in
# native script and in the romanized spellings people type on phones. Used
# only alongside an English role noun (see _BUYER_PATTERNS), so a post still
# has to be visibly about a video editor to qualify. Bengali "।" is a
# sentence terminator, so these are matched without relying on \b, which
# does not behave meaningfully across non-Latin script boundaries.
_NEED_WORDS_OTHER_LANGS = (
    r"চাই|দরকার|প্রয়োজন"              # Bengali: chai / dorkar / proyojon
    r"|चाहिए|चाहिये|ज़रूरत|जरूरत"        # Hindi: chahiye / zarurat
    r"|\bchahiye\b|\bchaiye\b|\bchahiy\b"   # romanized Hindi/Urdu
    r"|\bzaroorat\b|\bzarurat\b|\bdarkar\b"
    r"|\bkailangan\b|\bhanap\b"           # Tagalog: kailangan / hanap
    r"|\bbutuh\b|\bcari\b"                # Indonesian/Malay
)

_BUYER_PATTERNS = [
    re.compile(rf"\b(need|needed|needing)\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(rf"\b(need|needed|needing)\b{_GAP}\bediting\b", re.I),  # "need shorts editing"
    re.compile(rf"\blooking\s+for\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(rf"\bi\s*want\b{_GAP}\b{_ROLE_NOUN}\b", re.I),  # "I want video editor ... now"
    re.compile(rf"\brequired?\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(r"\bhiring\b", re.I),
    re.compile(rf"\bany\s+(good\s+)?{_GAP}?\b{_ROLE_NOUN}\b", re.I),
    re.compile(r"\bwho\s+can\s+(edit|design|help)\b", re.I),
    re.compile(r"\bwant\s+to\s+hire\b", re.I),
    re.compile(r"\burgently\s+(need|require)\b", re.I),
    re.compile(rf"\b(need|needed|needing|want|wanted|looking\s+for)\b{_GAP}\bsomeone\b{_GAP}\bto\s+edit\b", re.I),
    re.compile(r"\bto\s+edit\s+(my|our|the|this|these)?\s*videos?\b", re.I),
    re.compile(r"\blooking\s+to\s+(hire|outsource)\b", re.I),
    re.compile(r"\boutsource\b", re.I),
    re.compile(rf"\brecommend\w*\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(r"\bcan\s+(anyone|someone)\s+(recommend|suggest)\b", re.I),
    re.compile(r"\bany\s+recommendations?\b", re.I),
    # Role noun BEFORE the hiring word -- extremely common on Facebook:
    # "Video Editor Required!", "VIDEO EDITOR NEEDED", "Editor wanted".
    # Every pattern above requires the trigger word first, so these posts
    # were being missed entirely (confirmed live: "Video Editor Required!
    # Interested? WhatsApp: ..." was rejected as 'unknown').
    re.compile(rf"\b{_ROLE_NOUN}\b[^.!?\n]{{0,25}}\b(required|require|needed|need|wanted|want)\b", re.I),
    # Same hiring intent expressed in the poster's own language, in either
    # word order. These groups are heavily Bengali/Hindi/Urdu/Tagalog, and
    # posts routinely mix scripts ("বাংলা ভাষী Video Editor চাই",
    # "video editor chahiye"). The English role noun still has to be
    # present, so this does NOT loosen the requirement that the post be
    # about video editing -- it only recognizes the ask in another language.
    re.compile(rf"\b{_ROLE_NOUN}\b[^.!?\n]{{0,25}}(?:{_NEED_WORDS_OTHER_LANGS})", re.I),
    re.compile(rf"(?:{_NEED_WORDS_OTHER_LANGS})[^.!?\n]{{0,25}}\b{_ROLE_NOUN}\b", re.I),
    # "ISO" = In Search Of -- standard hiring shorthand in these groups
    # (live: "ISO a professional Video editor for a kids channel"). Matched
    # case-sensitively so it can't fire on the word "iso" inside other text.
    re.compile(rf"\bISO\b{_GAP}\b{_ROLE_NOUN}\b"),
    re.compile(rf"\bin\s+search\s+of\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(rf"\b(searching|search)\s+for\b{_GAP}\b{_ROLE_NOUN}\b", re.I),
    re.compile(r"\banyone\s+(who\s+)?(can|could)\s+edit\b", re.I),
]


# Phrases that read as "seller" in isolation but appear just as naturally in
# a GENUINE BUYER's post -- a hirer writes "DM me if interested" and "contact
# me for details" exactly as often as a freelancer does. Treating these as
# hard seller signals caused real customer leads to be classified as
# ambiguous and dropped (confirmed against a live post: "Looking for a
# Long-Term Video Editor ... DM me if interested. Must have 2 years
# experience." was rejected). They now only decide the outcome when there is
# no clear signal either way.
_WEAK_SELLER_PATTERNS = [
    re.compile(r"\bcontact\s+me\s+for\b", re.I),
    re.compile(r"\bdm\s+me\s+(for|if)\b", re.I),
    re.compile(r"\byears?\s+(of\s+)?experience\b", re.I),
]


def _normalize_for_matching(text: Optional[str]) -> Optional[str]:
    """
    Normalize typographic punctuation so the keyword patterns behave the
    same on real Facebook text as on hand-typed test text. Facebook (and
    phone keyboards) emit curly apostrophes, so "I’m a video editor" would
    otherwise slip past a pattern written with a straight "'m".
    """
    if not text:
        return text
    return (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"')
                .replace("–", "-").replace("—", "-"))


def classify_post_intent(post_text: Optional[str]) -> str:
    """
    Best-effort classification of a post's own text as "buyer" (looking to
    hire), "seller" (advertising their own services), or "unknown".

    Signals are tiered rather than pooled. An explicit hiring phrase
    ("looking for a video editor", "hiring", "need someone to edit") and an
    explicit self-promotion phrase ("I am a video editor", "hire me", "my
    portfolio") are both decisive; a post carrying only one of them is
    classified accordingly even if it also contains softer contact-me
    wording, which both buyers and sellers use. Only when neither decisive
    signal is present do the weak seller phrases decide, and a post with
    BOTH decisive signals stays "unknown" (e.g. a job seeker writing
    "looking for a job as a video editor") rather than being guessed at.
    """
    if not post_text:
        return "unknown"

    post_text = _normalize_for_matching(post_text)

    strong_seller = any(p.search(post_text) for p in _SELLER_PATTERNS)
    strong_buyer = any(p.search(post_text) for p in _BUYER_PATTERNS)

    if strong_buyer and not strong_seller:
        return "buyer"
    if strong_seller and not strong_buyer:
        return "seller"
    if strong_buyer and strong_seller:
        return "unknown"  # genuinely mixed -- don't guess

    # No decisive signal either way: fall back to the weak seller phrases.
    if any(p.search(post_text) for p in _WEAK_SELLER_PATTERNS):
        return "seller"
    return "unknown"


_ONSITE_PATTERNS = [
    re.compile(r"\bon[\s\-]?site\b", re.I),
    re.compile(r"\bin[\s\-]?office\b", re.I),
    re.compile(r"\bwork\s+from\s+office\b", re.I),
    re.compile(r"\bfull[\s\-]?time\s+(employee|job|position|staff)\b", re.I),
    re.compile(r"\boffice[\s\-]?based\b", re.I),
    # Non-native phrasings seen live ("office bas job", "office base work").
    re.compile(r"\boffice\s+bas(e|ed)?\b", re.I),
    re.compile(r"\boffice\s+(job|work|timing)\b", re.I),
    re.compile(r"\bnear\s+me\b", re.I),
    re.compile(r"\bwalk[\s\-]?in\s+interview\b", re.I),
    re.compile(r"\bin[\s\-]?person\s+only\b", re.I),
    re.compile(r"\bmust\s+(be\s+)?(available\s+)?(in|at)\s+(person|office)\b", re.I),
    re.compile(r"\bcome\s+to\s+(our\s+)?office\b", re.I),
    re.compile(r"\bphysical\s+presence\s+required\b", re.I),
]

_REMOTE_FREELANCE_PATTERNS = [
    re.compile(r"\bremote(ly)?\b", re.I),
    re.compile(r"\bfreelance\w*\b", re.I),
    re.compile(r"\bwork\s+from\s+home\b", re.I),
    re.compile(r"\bwfh\b", re.I),
    re.compile(r"\bonline\s+(work|job|only)\b", re.I),
    re.compile(r"\bpart[\s\-]?time\b", re.I),
    re.compile(r"\bcontract\s+basis\b", re.I),
    re.compile(r"\bper[\s\-]?project\b", re.I),
    re.compile(r"\bgig\b", re.I),
]


def classify_work_arrangement(post_text: Optional[str]) -> str:
    """
    Best-effort classification of a post's own text as "remote_freelance",
    "onsite" (in-person/full-time/office-based), or "unknown" (arrangement
    not mentioned -- most posts in a freelance group are implicitly remote).
    """
    if not post_text:
        return "unknown"

    is_onsite = any(p.search(post_text) for p in _ONSITE_PATTERNS)
    is_remote = any(p.search(post_text) for p in _REMOTE_FREELANCE_PATTERNS)

    if is_onsite and not is_remote:
        return "onsite"
    if is_remote:
        return "remote_freelance"
    return "unknown"


_VIDEO_EDITING_CONTEXT_PATTERN = re.compile(
    r"\bvideo\b|\bedit(?:s|or|ors|ing)?\b|\breels?\b|\btik\s*tok\b|\byoutube\b|\bshorts?\b"
    r"|\bdavinci\b|\bpremiere\b|\bafter\s*effects\b|\bmotion\s*graphics?\b|\bcontent\s*creator\b",
    re.I,
)


def is_qualifying_video_editing_lead(post_text: Optional[str]) -> bool:
    """
    Stricter combined gate applied on top of the individual classifiers:
    a post only qualifies as a lead worth saving if it (a) clearly reads as
    someone looking to hire, (b) actually mentions video-editing context,
    and (c) doesn't look like an on-site/in-person/full-time role. Each
    check can be individually disabled via its REQUIRE_*/EXCLUDE_* config
    constant. Ambiguous text fails (a) and (b) when strict mode is on --
    that's a deliberate precision-over-recall trade-off for a clean lead
    list, unlike the underlying classifiers, which default to keeping
    ambiguous posts.
    """
    intent = classify_post_intent(post_text)
    if REQUIRE_CLEAR_BUYER_INTENT:
        if intent != "buyer":
            return False
    elif EXCLUDE_SELLER_POSTS and intent == "seller":
        return False

    if REQUIRE_VIDEO_EDITING_CONTEXT and not (post_text and _VIDEO_EDITING_CONTEXT_PATTERN.search(post_text)):
        return False

    if EXCLUDE_ONSITE_POSTS and classify_work_arrangement(post_text) == "onsite":
        return False

    return True


def explain_lead_disqualification(post_text: Optional[str]) -> str:
    """
    Human-readable reason(s) a post failed is_qualifying_video_editing_lead,
    for diagnostic logging -- so a real run's log can be audited to see
    exactly why a post that looked like a genuine lead was rejected,
    instead of just knowing that it was.
    """
    intent = classify_post_intent(post_text)
    reasons = []

    if REQUIRE_CLEAR_BUYER_INTENT:
        if intent != "buyer":
            reasons.append(f"intent classified as '{intent}', not 'buyer'")
    elif EXCLUDE_SELLER_POSTS and intent == "seller":
        reasons.append("intent classified as 'seller'")

    if REQUIRE_VIDEO_EDITING_CONTEXT and not (post_text and _VIDEO_EDITING_CONTEXT_PATTERN.search(post_text)):
        reasons.append("no video-editing context keyword found")

    if EXCLUDE_ONSITE_POSTS and classify_work_arrangement(post_text) == "onsite":
        reasons.append("classified as onsite/in-person, not remote/freelance")

    return "; ".join(reasons) if reasons else "unknown (all checks passed but flagged anyway)"


# ---------------------------------------------------------------------------
# POST EXTRACTION
# ---------------------------------------------------------------------------

# Confirmed via live DOM inspection (Aug 2026): a post's own message text is
# rendered inside [data-ad-rendering-role="story_message"], and the author
# name inside [data-ad-rendering-role="profile_name"]. The older
# div[data-ad-preview='message'] selector this tool used no longer matches
# anything.
#
# Critically, div[role='article'] does NOT identify posts on current
# Facebook -- it matches COMMENTS (aria-label="Comment by ...") and grey
# loading skeletons. Selecting posts that way returned only comments and
# placeholders, which is why genuine leads were being missed entirely.
# Posts are located instead by finding each story_message and walking up to
# the nearest ancestor that also carries the post's permalink link.
POST_MESSAGE_SELECTOR = '[data-ad-rendering-role="story_message"]'

_FIND_POST_CONTAINERS_JS = """
const MSG = '[data-ad-rendering-role="story_message"]';
const LINK = 'a[href*="/posts/"], a[href*="/permalink/"], a[href*="story_fbid"]';
const msgs = Array.from(document.querySelectorAll(MSG));
const out = [];
const seen = new Set();
for (const m of msgs) {
  let cur = m;
  let container = null;
  let widestSinglePost = m;
  for (let i = 0; i < 25 && cur; i++) {
    // Never walk up past the point where this ancestor starts covering a
    // SECOND post -- otherwise every post in the feed collapses into one
    // giant shared container and only a single "post" is ever seen.
    if (cur.querySelectorAll(MSG).length > 1) break;
    widestSinglePost = cur;
    if (cur.querySelector(LINK)) { container = cur; break; }
    cur = cur.parentElement;
  }
  // No permalink inside this post's own subtree (some posts don't render a
  // static permalink href) -- fall back to the widest ancestor that still
  // covers only this one post, so the post is still processed and its URL
  // can be resolved by clicking its timestamp instead of being dropped.
  if (!container) container = widestSinglePost;
  if (container && !seen.has(container)) {
    seen.add(container);
    out.push(container);
  }
}
if (out.length) return out;

// FALLBACK: not every group renders story_message (confirmed live -- one
// group showed 0 story_message nodes but 2 role="article" posts). Fall
// back to outermost role="article" elements, which is the older, broader
// post contract. Nested articles are COMMENTS on a post, so keep only the
// outermost ones. Skeleton placeholders are filtered later by the
// rendered-text check in find_rendered_post_containers.
const arts = Array.from(document.querySelectorAll('div[role="article"]'));
return arts.filter(a => !arts.some(b => b !== a && b.contains(a)));
"""


def find_post_containers(driver) -> List:
    """
    Locate DOM elements that represent individual POSTS (not comments, not
    loading skeletons) in the currently rendered feed.

    Facebook's markup changes frequently, so rather than a brittle class
    name this anchors on the stable-ish data-ad-rendering-role contract:
    every real post renders its message as story_message. The returned
    element is the nearest ancestor of that message which also contains the
    post's permalink link, i.e. the whole post.
    """
    try:
        containers = driver.execute_script(_FIND_POST_CONTAINERS_JS) or []
    except Exception as exc:
        log.error(
            "Failed to query post containers -- Facebook's page structure may "
            "have changed and selectors likely need updating. Error: %s", exc
        )
        return []

    if not containers:
        log.warning(
            "No post containers found via %s. Either the feed hasn't "
            "rendered yet, or Facebook's DOM structure changed and selectors "
            "need updating.", POST_MESSAGE_SELECTOR
        )
    return containers


# Facebook renders div[role='article'] SKELETON PLACEHOLDERS while a group
# feed is still loading -- grey "glimmer" boxes carrying
# data-visualcompletion="loading-state" / aria-label="Loading..." and no
# text at all (confirmed via live testing + screenshot: the feed showed
# only grey bars while four such containers were already in the DOM).
# They are not posts. Reading the feed the instant containers appear means
# reading skeletons, whose empty text the rest of this module would
# misread as "no more posts here" and give up on the entire group -- which
# is exactly why genuine leads were being missed. Facebook can also swap a
# real post node mid-read via React reconciliation, surfacing as a
# stale-element error.
#
# So: every container read in this module MUST go through
# find_rendered_post_containers(), which filters skeletons out and waits
# for real content, rather than a bare find_post_containers() call. This
# applies uniformly to every configured group -- there is no per-group
# special-casing anywhere in this tool.
CONTENT_RENDER_WAIT_ROUNDS = 4
CONTENT_RENDER_POLL_SECONDS = 0.4


def is_skeleton_placeholder(container) -> bool:
    """
    True if this container is one of Facebook's grey loading skeletons
    rather than a real post: it advertises itself as a loading state, or it
    has no rendered message text yet.
    """
    from selenium.webdriver.common.by import By

    try:
        if container.find_elements(By.CSS_SELECTOR, "[data-visualcompletion='loading-state']"):
            return True
    except Exception:
        pass

    try:
        if (container.get_attribute("aria-label") or "").strip().lower().startswith("loading"):
            return True
    except Exception:
        pass

    return not extract_post_text(container)


def find_rendered_post_containers(driver) -> List:
    """
    Locate post containers that have ACTUALLY rendered, filtering out
    Facebook's loading skeletons, and waiting for real content to appear
    before giving up.

    Fresh element handles are re-queried on every poll (never held across a
    wait), so a mid-render React swap shows up as a normal empty read on
    the next poll rather than a stale-element exception. Returns only real,
    text-bearing post containers; an empty list means the feed genuinely
    has no more rendered posts, which callers may trust as a stop signal.
    """
    real: List = []
    for _ in range(CONTENT_RENDER_WAIT_ROUNDS):
        containers = find_post_containers(driver)
        real = [c for c in containers if not is_skeleton_placeholder(c)]
        if real:
            return real
        time.sleep(CONTENT_RENDER_POLL_SECONDS)

    log.warning(
        "No post content rendered after %.1fs of waiting (only loading "
        "skeletons or empty containers were present) -- Facebook may be "
        "loading slowly or this feed may be empty.",
        CONTENT_RENDER_WAIT_ROUNDS * CONTENT_RENDER_POLL_SECONDS,
    )
    return real


def extract_post_url_from_container(container) -> Optional[str]:
    """
    Best-effort passive check: is there already a real permalink-shaped href
    sitting in this container's links? On current Facebook markup this
    usually returns None (see extract_post_url_via_click below), but some
    post types (e.g. photo posts) do carry a real static permalink-shaped
    href, so this is tried first as a fast path before falling back to a
    click.
    """
    from selenium.webdriver.common.by import By

    try:
        anchors = container.find_elements(By.TAG_NAME, "a")
    except Exception as exc:
        log.debug("Could not read links from a post container: %s", exc)
        return None

    for a in anchors:
        try:
            href = a.get_attribute("href")
        except Exception:
            continue
        normalized = normalize_and_validate_post_url(href) if href else None
        if normalized:
            return normalized

    return None


def _post_fingerprint(container) -> Optional[str]:
    """
    A short, stable-ish text identifier for a post container.

    Clicking a post's timestamp (see extract_post_url_via_click) navigates
    away and back, which invalidates every previously-fetched Selenium
    element handle for the feed. This fingerprint lets process_posts tell
    "a post I already handled" apart from "a new post" after re-querying the
    DOM, without relying on a stale element reference.
    """
    try:
        text = (container.text or "").strip()
    except Exception:
        return None
    if not text:
        return None
    # Fingerprints get logged, and Facebook post text routinely contains
    # characters the Windows console codepage can't encode -- normalize to
    # an ASCII-safe form so a fingerprint can never crash a run.
    return text[:80].encode("ascii", "replace").decode("ascii")


def _find_timestamp_anchor(container):
    """
    Locate the clickable timestamp element inside a post container.

    Confirmed via live DOM inspection: Facebook's current markup does not
    expose the real post permalink as a static href on this element -- its
    href resolves only to the bare group URL (e.g. ".../groups/12345/"),
    with tracking query params and no post identifier. The real permalink
    is only revealed by actually clicking it (Facebook assembles the
    destination client-side). This identifies that element as the first
    <a> in the container whose href path is exactly the bare group root --
    distinct from the author link (points to a user/profile URL) and from
    photo/video links.
    """
    from selenium.webdriver.common.by import By

    try:
        anchors = container.find_elements(By.TAG_NAME, "a")
    except Exception:
        return None

    for a in anchors:
        try:
            href = a.get_attribute("href")
        except Exception:
            continue
        if not href:
            continue
        path = urlparse(href).path
        if re.match(r"^/groups/[^/]+/?$", path):
            return a
    return None


def extract_post_url_via_click(driver, container) -> Optional[str]:
    """
    Click the post's timestamp so Facebook's own UI navigates to the real
    permalink, capture and validate the resulting URL, then return to the
    group feed.

    This performs an ordinary left click a real user would make on an
    already-visible, already-loaded element -- it does not defeat a CAPTCHA,
    2FA, or any anti-bot mechanism, and it does not hide that this is
    automation. It is necessary because (confirmed via live testing) the
    visible timestamp's static href does not carry the real permalink; only
    the click does.
    """
    from selenium.common.exceptions import WebDriverException

    anchor = _find_timestamp_anchor(container)
    if anchor is None:
        return None

    origin_url = driver.current_url

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", anchor)
        anchor.click()
    except WebDriverException as exc:
        log.debug("Could not click a post's timestamp element: %s", exc)
        return None

    new_url = None
    waited = 0.0
    while waited < CLICK_NAV_TIMEOUT_SECONDS:
        time.sleep(0.25)
        waited += 0.25
        current = driver.current_url
        if current != origin_url:
            new_url = current
            break

    if new_url is None:
        log.debug("Clicking the timestamp did not navigate within %ss; skipping this post.",
                   CLICK_NAV_TIMEOUT_SECONDS)

    result = normalize_and_validate_post_url(new_url) if new_url else None

    try:
        driver.back()
        time.sleep(1.5)
    except WebDriverException as exc:
        log.warning("Could not navigate back to the group feed after a click: %s", exc)

    return result


def extract_post_timestamp_from_container(container) -> Optional[str]:
    """
    Pull the raw, displayed timestamp text for a post, without interpreting
    it yet. Tries the common places Facebook renders it: an <abbr> title,
    an aria-label on the timestamp link, or visible link text near the top
    of the post that looks like a short relative time.

    Known limitation (confirmed via live testing): current Facebook markup
    renders the visible "2h" / "Just now" text as scrambled Unicode
    combining-mark characters that only look correct visually via CSS, not
    in the DOM text order -- so this frequently returns None. Per this
    tool's design, a post whose timestamp can't be read this way is skipped
    and logged rather than guessed at; it is not silently assigned "now".
    """
    from selenium.webdriver.common.by import By

    try:
        abbrs = container.find_elements(By.TAG_NAME, "abbr")
        for abbr in abbrs:
            title = abbr.get_attribute("title")
            if title:
                return title.strip()
    except Exception:
        pass

    try:
        links = container.find_elements(By.TAG_NAME, "a")
    except Exception:
        return None

    time_like = re.compile(
        r"^\s*(\d+\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|w)\b.*|"
        r"just now|yesterday.*|\d+\s+(minute|hour|day|week)s?\s+ago)\s*$",
        re.I,
    )

    for link in links[:12]:  # timestamp link is always near the top of a post
        try:
            aria = link.get_attribute("aria-label")
            text = link.text
        except Exception:
            continue
        for candidate in (aria, text):
            if candidate and time_like.match(candidate.strip()):
                return candidate.strip()

    return None


def process_posts(driver, cutoff: datetime, seen_urls: Set[str], max_new_leads: Optional[int] = None,
                  known_signatures: Optional[List[dict]] = None) -> "ExtractionResult":
    """
    Extract, filter, and deduplicate posts from the currently loaded page.

    Getting the real permalink URL usually requires clicking each post's
    timestamp (see extract_post_url_via_click) since Facebook does not
    expose it as a static href. Each click navigates away and back, which
    invalidates previously-fetched element handles -- so this re-queries
    the DOM fresh after every click and tracks which posts were already
    handled via a short text fingerprint (_post_fingerprint) rather than
    relying on stale element references. MAX_POSTS_PER_RUN is only a hard
    safety ceiling; in normal operation CONSECUTIVE_OLD_POSTS_TO_STOP is
    what ends a group's scan, once posts older than DAYS_BACK start
    appearing consistently -- so a run covers a group's full available
    post history within the DAYS_BACK window, scrolling for more as needed
    (up to CONSECUTIVE_EMPTY_SCROLLS_TO_STOP retries) rather than stopping
    after the first screenful.

    max_new_leads, if given, stops extraction immediately (mid-page, before
    MAX_POSTS_PER_RUN or scrolling for more) once that many qualifying leads
    have been collected -- used to enforce MAX_LEADS_PER_RUN.
    """
    result = ExtractionResult()
    processed_fingerprints: Set[str] = set()
    attempts = 0
    consecutive_failures = 0
    empty_scroll_rounds = 0
    consecutive_old_posts = 0

    # A hidden/minimized window makes Facebook render nothing -- check once
    # up front so the run warns instead of silently reporting "no leads".
    ensure_page_visible(driver)

    # Always begin at the very top of the feed. Facebook virtualizes the
    # group feed, so any scrolling done before this point has already
    # discarded the newest posts from the DOM -- exactly the posts a
    # 2-day scan cares about most (confirmed live: a pre-scroll left the
    # page in empty space below the feed and the group yielded 1 post).
    try:
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1.5)
    except Exception as exc:
        log.debug("Could not scroll to top before scanning: %s", exc)

    while attempts < MAX_POSTS_PER_RUN:
        containers = find_rendered_post_containers(driver)
        result.posts_processed = max(result.posts_processed, len(containers))

        container = None
        for c in containers:
            fp = _post_fingerprint(c)
            if fp and fp not in processed_fingerprints:
                container = c
                break

        if container is None:
            if empty_scroll_rounds < CONSECUTIVE_EMPTY_SCROLLS_TO_STOP:
                empty_scroll_rounds += 1
                log.info("Ran out of loaded posts; scrolling for more (attempt %d/%d).",
                          empty_scroll_rounds, CONSECUTIVE_EMPTY_SCROLLS_TO_STOP)
                # Escalate: this path runs after nearly every post (Facebook
                # virtualizes the feed), so start cheap -- a full
                # SCROLL_ROUNDS sweep every time cost ~12s and dominated run
                # time (~12 min on one group in live testing). Only if
                # several light scrolls turn up nothing do we spend a full
                # sweep before concluding the group is exhausted.
                if empty_scroll_rounds <= 2:
                    scroll_once(driver)
                else:
                    scroll_to_load_posts(driver)
                continue
            log.info("No further unprocessed posts available after %d scroll attempts; "
                      "treating this group as fully scanned.", CONSECUTIVE_EMPTY_SCROLLS_TO_STOP)
            break
        empty_scroll_rounds = 0

        fp = _post_fingerprint(container)
        processed_fingerprints.add(fp)
        attempts += 1

        try:
            # Read whatever we can from the still-attached container BEFORE
            # navigating away -- post text and any (rare) static permalink
            # href are unaffected by the click/back round-trip below.
            # Facebook can swap this DOM node out from under us via React
            # reconciliation even without scrolling (confirmed via live
            # testing) -- if that happens mid-read, re-acquire a fresh
            # container with the same fingerprint text and retry once
            # rather than losing this post to a stale-element error.
            try:
                post_text = extract_post_text(container)
                raw_ts = extract_post_timestamp_from_container(container)
                url = extract_post_url_from_container(container)
            except Exception as stale_exc:
                if "stale element" not in str(stale_exc).lower():
                    raise
                log.debug("Container went stale mid-read, re-acquiring by fingerprint: %s", stale_exc)
                refreshed = None
                for c in find_rendered_post_containers(driver):
                    if _post_fingerprint(c) == fp:
                        refreshed = c
                        break
                if refreshed is None:
                    result.invalid_urls_skipped += 1
                    consecutive_failures += 1
                    continue
                container = refreshed
                post_text = extract_post_text(container)
                raw_ts = extract_post_timestamp_from_container(container)
                url = extract_post_url_from_container(container)

            if not url:
                url = extract_post_url_via_click(driver, container)

            if not url:
                result.invalid_urls_skipped += 1
                consecutive_failures += 1
                continue
            consecutive_failures = 0

            if url in seen_urls or url in result.seen_this_run:
                result.duplicates_skipped += 1
                continue

            post_date = parse_facebook_timestamp(raw_ts) if raw_ts else None

            if post_date is None:
                log.info("Skipping post (timestamp could not be reliably determined): %s", url)
                result.unparseable_timestamps_skipped += 1
                continue

            if post_date < cutoff:
                result.old_posts_skipped += 1
                consecutive_old_posts += 1
                if consecutive_old_posts >= CONSECUTIVE_OLD_POSTS_TO_STOP:
                    log.info("Hit %d consecutive posts older than the %d-day window; "
                              "treating this group's recent history as fully covered and stopping.",
                              consecutive_old_posts, DAYS_BACK)
                    break
                continue
            consecutive_old_posts = 0

            if not is_qualifying_video_editing_lead(post_text):
                snippet = (post_text or "").replace("\n", " ").strip()[:600]
                log.info("Skipping post (not a clear remote/freelance video-editing lead) [%s]: %s | text: %r",
                          explain_lead_disqualification(post_text), url, snippet)
                result.not_qualifying_lead_skipped += 1
                continue

            phone_number = extract_phone_number(post_text)

            # Cross-group dedup: the same person cross-posting the same
            # request into several groups is ONE lead, even though each
            # copy has its own post URL.
            signature = lead_signature(post_text, phone_number)
            prior_signatures = list(known_signatures or []) + result.signatures_this_run
            if is_duplicate_lead(signature, prior_signatures):
                log.info("Skipping post (same lead already saved from another group): %s", url)
                result.cross_group_duplicates_skipped += 1
                result.seen_this_run.add(url)  # don't re-examine this URL next run
                continue

            result.collected.append(CollectedPost(
                url=url,
                post_date=post_date,
                topic=extract_topic(post_text),
                niche=resolve_niche(post_text),
                budget=extract_budget(post_text),
                phone_number=phone_number,
            ))
            result.seen_this_run.add(url)
            result.signatures_this_run.append(signature)

            if max_new_leads is not None and len(result.collected) >= max_new_leads:
                log.info("Reached the cap of %d new lead(s) for this run; stopping immediately.", max_new_leads)
                break

        except Exception as exc:
            # One bad post must never take down the whole run.
            log.error("Failed to process a post, skipping it and continuing: %s", exc)
            consecutive_failures += 1
            continue

        if consecutive_failures >= 5:
            log.warning("Stopping early after 5 consecutive posts with no extractable permalink.")
            break

    return result


@dataclass
class ExtractionResult:
    posts_processed: int = 0
    old_posts_skipped: int = 0
    duplicates_skipped: int = 0
    invalid_urls_skipped: int = 0
    unparseable_timestamps_skipped: int = 0
    not_qualifying_lead_skipped: int = 0
    cross_group_duplicates_skipped: int = 0  # same lead already saved from another group
    collected: List[CollectedPost] = None
    seen_this_run: Set[str] = None
    signatures_this_run: List[dict] = None

    def __post_init__(self):
        if self.collected is None:
            self.collected = []
        if self.seen_this_run is None:
            self.seen_this_run = set()
        if self.signatures_this_run is None:
            self.signatures_this_run = []


# ---------------------------------------------------------------------------
# LOCAL DEDUPLICATION STORE
# ---------------------------------------------------------------------------

def load_seen_urls() -> Set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception as exc:
        log.error("Could not read %s (starting with an empty dedup set): %s", SEEN_FILE, exc)
        return set()


def save_seen_urls(urls: Set[str]) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(urls), f, indent=2)
    except Exception as exc:
        log.error("Could not write %s -- collected URLs are still in memory/log this run: %s", SEEN_FILE, exc)


# ---------------------------------------------------------------------------
# CROSS-GROUP LEAD DEDUPLICATION
#
# The same person often posts the same request into several groups. Each
# copy has its own post URL, so URL dedup treats them as separate leads.
# These helpers compare the LEAD itself -- the poster's own text plus any
# phone number they gave -- so a cross-posted request is saved once.
# ---------------------------------------------------------------------------

def _normalize_lead_text(text: Optional[str]) -> str:
    """Reduce a post to comparable content: no case, punctuation, URLs or emoji."""
    if not text:
        return ""
    t = _normalize_for_matching(text).lower()
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)  # keeps Bengali/Hindi/etc.
    t = re.sub(r"\s+", " ", t).strip()
    return t[:LEAD_SIGNATURE_CHARS]


def lead_signature(post_text: Optional[str], phone_number: Optional[str] = None) -> dict:
    """A comparable identity for one lead: its normalized text and phone."""
    return {
        "text": _normalize_lead_text(post_text),
        "phone": re.sub(r"\D", "", phone_number or ""),  # digits only, so formatting doesn't matter
    }


def is_duplicate_lead(signature: dict, known_signatures: List[dict]) -> bool:
    """
    True if this lead is one already saved -- from ANY group.

    Same phone number means the same person, so that alone is decisive.
    Otherwise the post text must read almost identically
    (LEAD_SIMILARITY_THRESHOLD), and only for posts long enough to be
    distinctive: two different people each writing "Hiring Video Editors"
    are two real leads, not a repost.
    """
    import difflib

    phone = signature.get("phone") or ""
    text = signature.get("text") or ""

    if phone:
        for known in known_signatures:
            if known.get("phone") and known["phone"] == phone:
                return True

    if len(text) < LEAD_MIN_CHARS_FOR_SIMILARITY:
        return False

    for known in known_signatures:
        known_text = known.get("text") or ""
        if len(known_text) < LEAD_MIN_CHARS_FOR_SIMILARITY:
            continue
        if known_text == text:
            return True
        if difflib.SequenceMatcher(None, known_text, text).ratio() >= LEAD_SIMILARITY_THRESHOLD:
            return True

    return False


def load_lead_signatures() -> List[dict]:
    if not os.path.exists(LEAD_SIGNATURE_FILE):
        return []
    try:
        with open(LEAD_SIGNATURE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    except Exception as exc:
        log.error("Could not read %s (cross-group dedup starts empty): %s", LEAD_SIGNATURE_FILE, exc)
        return []


def save_lead_signatures(signatures: List[dict]) -> None:
    try:
        with open(LEAD_SIGNATURE_FILE, "w", encoding="utf-8") as f:
            json.dump(signatures, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        log.error("Could not write %s -- cross-group dedup may repeat a lead next run: %s",
                  LEAD_SIGNATURE_FILE, exc)


# ---------------------------------------------------------------------------
# DAILY LEAD QUOTA
# ---------------------------------------------------------------------------

def load_daily_quota_used() -> int:
    """
    How many leads have already been saved today. Resets to 0 automatically
    the first time this is called on a new calendar day (compared to the
    date stored in DAILY_QUOTA_FILE) -- no separate reset step needed.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if not os.path.exists(DAILY_QUOTA_FILE):
        return 0
    try:
        with open(DAILY_QUOTA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.warning("Could not read %s, treating today's quota as 0: %s", DAILY_QUOTA_FILE, exc)
        return 0
    if data.get("date") != today:
        return 0  # new calendar day -- quota resets
    return int(data.get("count", 0))


def save_daily_quota_used(count: int) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(DAILY_QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today, "count": count}, f, indent=2)
    except Exception as exc:
        log.error("Could not write %s -- daily quota tracking may be inaccurate next run: %s", DAILY_QUOTA_FILE, exc)


# ---------------------------------------------------------------------------
# GOOGLE SHEETS
# ---------------------------------------------------------------------------

def get_sheets_worksheet():
    """
    Authenticate with the Google service account and return the target
    worksheet, creating the tab (with headers) if it does not exist yet.
    """
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(
            f"{SERVICE_ACCOUNT_FILE} not found. See README.md for how to create "
            f"a Google service account and download its JSON key."
        )

    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, GOOGLE_SCOPES)
    client = gspread.authorize(creds)

    spreadsheet = client.open_by_key(SHEET_KEY) if SHEET_KEY else client.open(SHEET_NAME)

    try:
        worksheet = spreadsheet.worksheet(TAB_NAME)
    except gspread.exceptions.WorksheetNotFound:
        log.info("Tab '%s' not found in '%s' -- creating it.", TAB_NAME, SHEET_NAME)
        worksheet = spreadsheet.add_worksheet(title=TAB_NAME, rows=1000, cols=len(SHEET_HEADERS))
        worksheet.append_row(SHEET_HEADERS)

    # Ensure headers exist even if the tab already existed but was empty.
    first_row = worksheet.row_values(1)
    if not first_row:
        worksheet.append_row(SHEET_HEADERS)

    return worksheet


_URL_COLUMN = SHEET_HEADERS.index("URL") + 1  # 1-based column index gspread expects


def get_existing_sheet_urls(worksheet) -> Set[str]:
    """Read the URL column so we never insert a duplicate that's already in the sheet."""
    try:
        values = worksheet.col_values(_URL_COLUMN)
    except Exception as exc:
        log.warning("Could not read existing sheet URLs, proceeding without that extra check: %s", exc)
        return set()
    return set(values[1:])  # skip header


def append_posts_to_sheet(worksheet, posts: List[CollectedPost]) -> int:
    """Append rows for newly collected posts. Returns the number of rows written."""
    if not posts:
        return 0

    try:
        existing_data_rows = len(worksheet.col_values(_URL_COLUMN)) - 1  # minus header
    except Exception:
        existing_data_rows = 0
    start_number = max(existing_data_rows, 0) + 1

    rows = [
        [
            p.topic or "",
            p.niche or "",
            p.url,
            p.budget or "",
            start_number + i,
            p.post_date.strftime("%Y-%m-%d %H:%M:%S"),
            p.phone_number or "",
        ]
        for i, p in enumerate(posts)
    ]
    worksheet.append_rows(rows, value_input_option="RAW")
    return len(rows)


# ---------------------------------------------------------------------------
# LOCAL BACKUP (used if Google Sheets write fails, so nothing is lost)
# ---------------------------------------------------------------------------

def write_local_backup(posts: List[CollectedPost]) -> str:
    backup_file = f"fb_urls_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        with open(backup_file, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "url": p.url,
                        "post_date": p.post_date.isoformat(),
                        "topic": p.topic,
                        "niche": p.niche,
                        "budget": p.budget,
                        "phone_number": p.phone_number,
                    }
                    for p in posts
                ],
                f, indent=2,
            )
        log.info("Collected URLs preserved locally in %s", backup_file)
    except Exception as exc:
        log.error("Even the local backup write failed -- URLs only exist in this run's log: %s", exc)
    return backup_file


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    log.info("Script started.")
    now = datetime.now()
    cutoff = now - timedelta(days=DAYS_BACK)

    quota_used = load_daily_quota_used()
    remaining_quota = MAX_LEADS_PER_RUN - quota_used
    log.info("Daily lead quota: %d/%d used today.", quota_used, MAX_LEADS_PER_RUN)
    if remaining_quota <= 0:
        log.info("Daily lead limit already reached (%d/%d) -- skipping this run. "
                  "Resets automatically on the next run after today.", quota_used, MAX_LEADS_PER_RUN)
        log.info("Script completed.")
        return 0

    driver = None
    try:
        driver = attach_to_user_chrome()
    except Exception:
        log.error("Script completed with errors (could not attach to browser).")
        return 1

    try:
        if not detect_group_page(driver):
            log.error("Script completed: no valid Facebook Group page detected.")
            return 1

        # No pre-scroll: the feed is virtualized, so scrolling before
        # scanning discards the newest posts. process_posts starts at the
        # top and scrolls incrementally as it needs more.

        seen_urls = load_seen_urls()
        log.info("Loaded %d previously seen URL(s) from %s.", len(seen_urls), SEEN_FILE)
        lead_signatures = load_lead_signatures()
        log.info("Loaded %d saved lead signature(s) for cross-group dedup.", len(lead_signatures))

        result = process_posts(driver, cutoff, seen_urls, max_new_leads=remaining_quota,
                               known_signatures=lead_signatures)

        log.info("Posts processed: %d", result.posts_processed)
        log.info("New URLs found: %d", len(result.collected))
        log.info("Old posts skipped (outside %d-day window): %d", DAYS_BACK, result.old_posts_skipped)
        log.info("Duplicate URLs skipped: %d", result.duplicates_skipped)
        log.info("Cross-group duplicate leads skipped (same lead in another group): %d",
                 result.cross_group_duplicates_skipped)
        log.info("Invalid/non-post URLs skipped: %d", result.invalid_urls_skipped)
        log.info("Posts skipped (timestamp unparseable): %d", result.unparseable_timestamps_skipped)
        log.info("Posts skipped (not a clear remote/freelance video-editing lead): %d", result.not_qualifying_lead_skipped)

    finally:
        try:
            driver.quit()
        except Exception:
            pass  # Never fail the run just because cleanup of the attached session had trouble.

    if not result.collected:
        log.info("No new posts to write. Script completed.")
        return 0

    try:
        worksheet = get_sheets_worksheet()
        existing_sheet_urls = get_existing_sheet_urls(worksheet)

        to_write = [p for p in result.collected if p.url not in existing_sheet_urls]
        skipped_already_in_sheet = len(result.collected) - len(to_write)
        if skipped_already_in_sheet:
            log.info("Skipped %d URL(s) already present in the Google Sheet.", skipped_already_in_sheet)

        written = append_posts_to_sheet(worksheet, to_write)
        log.info("URLs written to Google Sheets: %d", written)

        save_daily_quota_used(quota_used + written)
        log.info("Daily lead quota: %d/%d used today.", quota_used + written, MAX_LEADS_PER_RUN)

    except Exception as exc:
        log.error("Google Sheets write failed: %s", exc)
        write_local_backup(result.collected)
        # Still update the local seen-set so we don't re-process these next run's DOM twice,
        # but the operator can see them in the backup file and copy them in manually.
        save_seen_urls(seen_urls | result.seen_this_run)
        log.error("Script completed with errors (Google Sheets failure).")
        return 1

    save_seen_urls(seen_urls | result.seen_this_run)
    if result.signatures_this_run:
        save_lead_signatures(lead_signatures + result.signatures_this_run)
    log.info("Script completed.")
    return 0



# LeadForge: editable local settings
import json as _lf_json
from pathlib import Path as _lf_Path
_lf_settings = _lf_Path(__file__).parent / "dashboard.config.json"
if _lf_settings.exists():
    for _lf_key, _lf_value in _lf_json.loads(_lf_settings.read_text(encoding="utf-8")).items():
        if _lf_key in ('DAYS_BACK', 'SHEET_NAME', 'TAB_NAME', 'SHEET_KEY', 'MAX_LEADS_PER_RUN', 'SCROLL_ROUNDS', 'CHROME_DEBUGGER_ADDRESS', 'MAX_POSTS_PER_RUN', 'EXCLUDE_SELLER_POSTS', 'EXCLUDE_ONSITE_POSTS', 'REQUIRE_CLEAR_BUYER_INTENT', 'REQUIRE_VIDEO_EDITING_CONTEXT', 'GROQ_MODEL', 'GROQ_ENABLED'):
            if type(_lf_value) is not type(globals()[_lf_key]):
                raise ValueError(f"Invalid type for {_lf_key} in dashboard.config.json")
            globals()[_lf_key] = _lf_value

if __name__ == "__main__":
    sys.exit(main())
