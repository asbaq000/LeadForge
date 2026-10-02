"""Google Sheets writer + the canonical column layout shared with the CSV.

Layout: one master tab with every podcaster, optionally one tab per genre.

Column order is deliberate and outreach-first: who they are, how to reach
them, where the show lives, then the numbers that qualify them, then the
bookkeeping fields you only look at when something seems wrong. You should
be able to write a cold email from the first ten columns alone.

Deduplication is belt-and-braces. The DB flags what has been synced, and on
top of that every tab's channel-id column is read before writing so a row
can never land twice -- even if the DB is deleted, the sheet is edited by
hand, or two runs overlap.
"""

from __future__ import annotations

import random
import sqlite3
import time
from typing import Any, Callable, Iterable, Sequence, TypeVar

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MAX_RETRIES = 6
T = TypeVar("T")


def _status_of(exc: APIError) -> int | None:
    try:
        return exc.response.status_code
    except AttributeError:
        return None


def _retry(fn: Callable[[], T]) -> T:
    """Google's Sheets API defaults to 60 write requests/min/user -- a burst
    like creating every genre tab up front blows straight through that.
    Retry 429/5xx with backoff instead of letting one rate-limit kill a run
    that already spent YouTube quota finding these leads."""
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except APIError as exc:
            status = _status_of(exc)
            if status == 429 or (status and status >= 500):
                if attempt == MAX_RETRIES - 1:
                    raise
                retry_after = None
                try:
                    retry_after = exc.response.headers.get("Retry-After")
                except AttributeError:
                    pass
                wait = (float(retry_after)
                        if retry_after and retry_after.replace(".", "", 1).isdigit()
                        else delay)
                time.sleep(wait + random.uniform(0, 0.5))
                delay = min(delay * 2, 30.0)
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


HEADERS: tuple[str, ...] = (
    # -- who they are --
    "Podcast / Channel",      # 1
    "Host",                   # 2
    "Subscribers",            # 3
    "Genre",                  # 4
    "Format",                 # 5
    "Language",               # 6
    "Country",                # 7
    # -- how to reach them --
    "Email",                  # 8
    "Guest / Booking Form",   # 9
    "Website",                # 10
    "Instagram",              # 11
    "Twitter/X",              # 12
    "LinkedIn",               # 13
    "TikTok",                 # 14
    "Facebook",               # 15
    # -- where the show lives --
    "Spotify",                # 16
    "Apple Podcasts",         # 17
    "Other Platform",         # 18
    "RSS Feed",               # 19
    "Membership",             # 20
    "Discord",                # 21
    "Telegram",               # 22
    "Other Links",            # 23
    # -- what the show looks like --
    "Median Episode (min)",   # 24
    "Longest Episode (min)",  # 25
    "Avg Views / Episode",    # 26
    "Shorts Share",           # 27
    "Episodes / Month",       # 28
    "Last Upload",            # 29
    "Days Since Upload",      # 30
    "Median Gap (days)",      # 31
    "Total Videos",           # 32
    "Total Views",            # 33
    # -- why we believe it's a podcast --
    "Podcast Score",          # 34
    "Confidence",             # 35
    "Podcast Signals",        # 36
    # -- bookkeeping --
    "Channel ID",             # 37  <- dedupe key
    "Channel URL",            # 38
    "Created",                # 39
    "Found Via",              # 40
    "First Seen",             # 41
)

ID_COL = 37  # 1-indexed column of "Channel ID"

# Pixel widths, same order as HEADERS -- set once per new tab so long values
# (emails, URLs) don't get truncated and short ones (Country, Videos) don't
# waste space.
COLUMN_WIDTHS: tuple[int, ...] = (
    230, 160, 100, 190, 110, 100, 80,
    230, 210, 210, 180, 150, 190, 150, 180,
    210, 210, 210, 200, 190, 150, 150, 220,
    130, 140, 130, 100, 120, 100, 110, 120, 100, 110,
    110, 100, 300,
    170, 250, 100, 170, 100,
)

# 1-indexed columns that read as numbers/counters -- right-aligned so they
# line up like a real spreadsheet instead of ragged left-aligned text.
RIGHT_ALIGN_COLS = (3, 24, 25, 26, 27, 28, 30, 31, 32, 33, 34)


def format_subs(n: Any) -> str:
    """1234 -> '1.2k', 55600 -> '55.6k', 1000000 -> '1m'. Below 1000, the
    bare number reads fine on its own and formatting it would just be noise."""
    try:
        n = round(float(n))
    except (TypeError, ValueError):
        return str(n)
    if n < 1000:
        return str(n)
    unit, div = ("m", 1_000_000) if n >= 1_000_000 else ("k", 1_000)
    val = round(n / div, 1)
    if val >= 1000 and unit == "k":  # e.g. 999,999 rounds to 1000.0k -- bump a tier
        unit, div = "m", 1_000_000
        val = round(n / div, 1)
    return f"{int(val)}{unit}" if val == int(val) else f"{val}{unit}"


GENRE_LABELS: dict[str, str] = {
    "business_entrepreneurship": "Business & Entrepreneurship",
    "marketing_sales": "Marketing & Sales",
    "finance_investing": "Finance & Investing",
    "crypto_web3": "Crypto & Web3",
    "tech_ai": "Tech & AI",
    "software_dev": "Software & Development",
    "health_fitness": "Health & Fitness",
    "health_medicine": "Health & Medicine",
    "mental_health": "Mental Health",
    "self_improvement": "Self-Improvement",
    "spirituality_religion": "Spirituality & Religion",
    "relationships_dating": "Relationships & Dating",
    "parenting_family": "Parenting & Family",
    "true_crime": "True Crime",
    "news_politics": "News & Politics",
    "history": "History",
    "science": "Science",
    "education_language": "Education & Language",
    "comedy": "Comedy",
    "pop_culture_film": "Pop Culture & Film",
    "music": "Music",
    "gaming": "Gaming",
    "sports": "Sports",
    "travel_lifestyle": "Travel & Lifestyle",
    "food_drink": "Food & Drink",
    "real_estate": "Real Estate",
    "career_hr": "Career & Work",
    "creator_economy": "Creator Economy",
    "society_culture": "Society & Culture",
    "interview_general": "General Interview",
    "other": "Other",
    "pending_classification": "Pending Classification",
}

FORMAT_LABELS: dict[str, str] = {
    "interview": "Interview",
    "solo": "Solo",
    "co_hosted": "Co-hosted",
    "panel": "Panel",
    "clips": "Clips",
    "narrative": "Narrative",
    "video_first": "Video Podcast",
    "other": "Other",
}


def tab_name(genre: str) -> str:
    return GENRE_LABELS.get(genre, genre.replace("_", " ").title())


def format_label(fmt: str) -> str:
    return FORMAT_LABELS.get(fmt or "other", (fmt or "other").replace("_", " ").title())


class SheetsWriter:
    def __init__(self, credentials_file: str, spreadsheet_id: str):
        creds = Credentials.from_service_account_file(str(credentials_file), scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.sheet = self.gc.open_by_key(spreadsheet_id)
        self._tabs: dict[str, gspread.Worksheet] = {}
        self._existing: dict[str, set[str]] = {}

    # -- tabs --------------------------------------------------------------
    def tab(self, name: str) -> gspread.Worksheet:
        if name in self._tabs:
            return self._tabs[name]
        try:
            ws = _retry(lambda: self.sheet.worksheet(name))
        except WorksheetNotFound:
            ws = _retry(lambda: self.sheet.add_worksheet(
                title=name, rows=1000, cols=len(HEADERS)
            ))
            self._init_tab(ws)
        self._tabs[name] = ws
        return ws

    def _init_tab(self, ws: gspread.Worksheet) -> None:
        _retry(lambda: ws.update(values=[list(HEADERS)], range_name="A1"))
        try:
            _retry(lambda: ws.freeze(rows=1, cols=1))
            last_col = gspread.utils.rowcol_to_a1(1, len(HEADERS))
            _retry(lambda: ws.format(
                f"A1:{last_col}",
                {
                    "textFormat": {"bold": True, "foregroundColor":
                        {"red": 1.0, "green": 1.0, "blue": 1.0}},
                    "backgroundColor": {"red": 0.14, "green": 0.16, "blue": 0.20},
                    "horizontalAlignment": "LEFT",
                    "verticalAlignment": "MIDDLE",
                },
            ))
            for col in RIGHT_ALIGN_COLS:
                a1 = gspread.utils.rowcol_to_a1(1, col).rstrip("0123456789")
                _retry(lambda a1=a1: ws.format(f"{a1}2:{a1}1000",
                                               {"horizontalAlignment": "RIGHT"}))
        except APIError:
            pass  # cosmetics only -- never fail a run over formatting
        self._pretty_layout(ws)

    def _pretty_layout(self, ws: gspread.Worksheet) -> None:
        """Column widths + alternating row banding, so the sheet reads like a
        real table instead of a raw data dump. Best-effort -- a run's data
        must never fail over cosmetics."""
        sheet_id = ws.id
        requests: list[dict[str, Any]] = [
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                              "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
            for i, width in enumerate(COLUMN_WIDTHS)
        ]
        requests.append({
            "addBanding": {
                "bandedRange": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 1,
                              "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
                    "rowProperties": {
                        "firstBandColor": {"red": 1.0, "green": 1.0, "blue": 1.0},
                        "secondBandColor": {"red": 0.95, "green": 0.96, "blue": 0.98},
                    },
                }
            }
        })
        try:
            _retry(lambda: self.sheet.batch_update({"requests": requests}))
        except APIError:
            pass

    def ensure_header(self, ws: gspread.Worksheet) -> None:
        first = _retry(lambda: ws.row_values(1))
        if not first:
            self._init_tab(ws)

    # -- dedupe ------------------------------------------------------------
    def existing_ids(self, name: str) -> set[str]:
        """Channel IDs already present in a tab (cached per session)."""
        if name in self._existing:
            return self._existing[name]
        ws = self.tab(name)
        self.ensure_header(ws)
        try:
            col = _retry(lambda: ws.col_values(ID_COL))[1:]  # drop header
        except APIError:
            col = []
        ids = {c.strip() for c in col if c and c.strip()}
        self._existing[name] = ids
        return ids

    # -- capacity ----------------------------------------------------------
    def _ensure_capacity(self, ws: gspread.Worksheet, needed_rows: int) -> None:
        """Direct range writes (values.update), unlike append_rows, don't
        grow the grid on their own -- expand ahead of a write that would
        land past the tab's current row_count (created at 1000)."""
        if ws.row_count < needed_rows:
            try:
                _retry(lambda: ws.add_rows(needed_rows - ws.row_count + 200))
            except APIError:
                pass

    # -- date sections -----------------------------------------------------
    def ensure_date_section(self, name: str, date_key: str, date_label: str) -> bool:
        """Starts today's section with a blank spacer row + a merged, styled
        date/time header -- but only the first time it's called for a given
        calendar day. A second run later the same day (a manual re-run, a
        catch-up `export`) finds today's header already there and just
        appends underneath it instead of stacking a duplicate one.

        date_key is the plain date (e.g. "August 13, 2026") and must be a
        prefix of date_label (which adds a time, e.g. "... - 09:04 AM").
        Passed as two pieces rather than parsed back out of date_label so
        matching doesn't depend on the display format's exact punctuation.
        Column A survives Sheets' merge (only the top-left cell's value
        does), so checking it directly needs no separate marker column.
        """
        ws = self.tab(name)
        self.ensure_header(ws)
        try:
            col_a = _retry(lambda: ws.col_values(1))
        except APIError:
            col_a = []
        if any(v.startswith(date_key) for v in col_a):
            return False
        blank_row = [""] * len(HEADERS)
        date_row = [date_label] + [""] * (len(HEADERS) - 1)
        # A direct range write, not append_rows: values.append does "table
        # detection" that stops at the first blank row, which would insert
        # this section BEFORE an earlier day's blank/header rows instead of
        # after them. Writing to an explicit range computed from the true
        # row count is the only way to guarantee landing at the real end.
        start = len(col_a) + 1
        end = start + 1
        self._ensure_capacity(ws, end)
        a1_range = f"A{start}:{gspread.utils.rowcol_to_a1(end, len(HEADERS))}"
        _retry(lambda: ws.update(values=[blank_row, date_row], range_name=a1_range))
        self._style_date_row(ws, end)
        return True

    def _style_date_row(self, ws: gspread.Worksheet, row_index: int) -> None:
        """Merge the date row into one cell and style it like a section
        header. Cosmetic only -- never worth failing a run over."""
        sheet_id = ws.id
        rng = {"sheetId": sheet_id, "startRowIndex": row_index - 1, "endRowIndex": row_index,
               "startColumnIndex": 0, "endColumnIndex": len(HEADERS)}
        requests = [
            {"mergeCells": {"range": rng, "mergeType": "MERGE_ALL"}},
            {"repeatCell": {
                "range": rng,
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": 0.85, "green": 0.90, "blue": 0.98},
                    "textFormat": {"bold": True, "fontSize": 11,
                                   "foregroundColor": {"red": 0.10, "green": 0.20, "blue": 0.40}},
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,"
                          "horizontalAlignment,verticalAlignment)",
            }},
        ]
        try:
            _retry(lambda: self.sheet.batch_update({"requests": requests}))
        except APIError:
            pass

    # -- writes ------------------------------------------------------------
    def append(self, name: str, rows: Sequence[Sequence[Any]]) -> int:
        """Append rows that are not already in the tab. Returns rows written."""
        if not rows:
            return 0
        known = self.existing_ids(name)
        fresh: list[Sequence[Any]] = []
        for row in rows:
            cid = str(row[ID_COL - 1]).strip()
            if cid and cid not in known:
                known.add(cid)
                fresh.append(row)
        if not fresh:
            return 0
        ws = self.tab(name)
        # Direct range write, not append_rows -- see the comment in
        # ensure_date_section: once a blank spacer row exists anywhere in
        # the tab, values.append's table-detection would insert new rows
        # BEFORE it instead of at the true end.
        try:
            col_a = _retry(lambda: ws.col_values(1))
        except APIError:
            col_a = []
        start = len(col_a) + 1
        end = start + len(fresh) - 1
        self._ensure_capacity(ws, end)
        a1_range = f"A{start}:{gspread.utils.rowcol_to_a1(end, len(HEADERS))}"
        _retry(lambda: ws.update(values=list(fresh), range_name=a1_range))
        return len(fresh)


def _pct(value: Any) -> str:
    """0.42 -> '42%'. Blank for missing, because '0%' and 'unknown' are very
    different answers to "does this show cut clips already"."""
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return ""


def row_from_lead(lead: sqlite3.Row | dict[str, Any], for_sheet: bool = False) -> list[Any]:
    """One lead -> one row, in HEADERS order.

    for_sheet=True renders Subscribers as '55.6k' for human reading in
    Google Sheets. CSV keeps the raw number -- it's meant for other tools to
    filter and sort on, not to look at directly.
    """
    g = lead.__getitem__ if isinstance(lead, sqlite3.Row) else lead.get

    def val(key: str, default: Any = "") -> Any:
        try:
            v = g(key)
        except (IndexError, KeyError):
            return default
        return default if v is None else v

    subs = val("subscribers", 0)
    return [
        val("title"),
        val("host_name"),
        format_subs(subs) if for_sheet else subs,
        tab_name(str(val("genre", "other")) or "other"),
        format_label(str(val("podcast_format", "other"))),
        val("language"),
        val("country"),

        val("email"),
        val("booking_link"),
        val("website"),
        val("instagram"),
        val("twitter"),
        val("linkedin"),
        val("tiktok"),
        val("facebook"),

        val("spotify"),
        val("apple_podcasts"),
        val("other_platform"),
        val("rss_feed"),
        val("membership_link"),
        val("discord"),
        val("telegram"),
        val("other_links"),

        val("median_episode_minutes"),
        val("longest_episode_minutes"),
        val("avg_episode_views"),
        _pct(val("shorts_ratio", None)),
        val("episodes_per_month"),
        val("last_upload"),
        val("days_since_upload"),
        val("median_gap_days"),
        val("video_count", 0),
        val("view_count", 0),

        val("podcast_score"),
        val("podcast_confidence"),
        val("podcast_signals"),

        val("channel_id"),
        val("url"),
        (str(val("created_at")) or "")[:10],
        val("discovered_via"),
        (str(val("first_seen")) or "")[:10],
    ]


def push(
    writer: SheetsWriter,
    leads: Iterable[sqlite3.Row | dict[str, Any]],
    master_tab: str = "Podcaster Leads",
    tab_per_genre: bool = False,
    date_key: str | None = None,
    date_label: str | None = None,
) -> dict[str, int]:
    """Write leads to the master tab and, optionally, their per-genre tabs.

    date_key/date_label (both required together) get today's leads a
    blank-row + merged-date-header section in the master tab, once per
    calendar day -- see SheetsWriter.ensure_date_section.
    """
    leads = list(leads)
    if not leads:
        return {}

    written: dict[str, int] = {}
    if date_key and date_label:
        writer.ensure_date_section(master_tab, date_key, date_label)
    master_rows = [row_from_lead(l, for_sheet=True) for l in leads]
    n = writer.append(master_tab, master_rows)
    if n:
        written[master_tab] = n

    if tab_per_genre:
        buckets: dict[str, list[list[Any]]] = {}
        for lead in leads:
            g = lead.__getitem__ if isinstance(lead, sqlite3.Row) else lead.get
            try:
                genre = g("genre") or "other"
            except (IndexError, KeyError):
                genre = "other"
            buckets.setdefault(tab_name(str(genre)), []).append(
                row_from_lead(lead, for_sheet=True)
            )
        for name, rows in buckets.items():
            n = writer.append(name, rows)
            if n:
                written[name] = n

    return written
