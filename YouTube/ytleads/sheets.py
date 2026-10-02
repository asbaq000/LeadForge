"""Google Sheets writer.

Layout: one "All Leads" master tab, plus one tab per content category, so each
content type gets its own table exactly as specified.

Deduplication is belt-and-braces. The DB flags what has been synced, and on top
of that every tab's channel-id column is read before writing so a row can never
land twice -- even if the DB is deleted, the sheet is edited by hand, or two
runs overlap.
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
    like creating every category tab up front blows straight through that.
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
    "Channel Name",       # A
    "Subscribers",        # B
    "Channel ID",         # C  <- dedupe key
    "Channel URL",
    "Content Type",
    "Email",
    "Instagram",
    "Facebook",
    "Twitter/X",
    "TikTok",
    "LinkedIn",
    "Discord",
    "Telegram",
    "Website",
    "Other Links",
    "Videos",
    "Total Views",
    "Last Upload",
    "Days Since Upload",
    "Median Gap (days)",
    "Country",
    "Created",
    "Found Via",
    "First Seen",
)

ID_COL = 3  # 1-indexed column of "Channel ID"

# Pixel widths, same order as HEADERS -- set once per new tab so long values
# (emails, URLs) don't get truncated and short ones (Videos, Country) don't
# waste space.
COLUMN_WIDTHS: tuple[int, ...] = (
    220, 90, 170, 250, 150, 220, 180, 180, 150, 150,
    180, 150, 150, 200, 220, 80, 110, 100, 90, 110,
    90, 100, 160, 100,
)

# 1-indexed columns that read as numbers/counters -- right-aligned so they
# line up like a real spreadsheet instead of ragged left-aligned text.
RIGHT_ALIGN_COLS = (2, 16, 17, 19, 20)  # Subscribers, Videos, Total Views, Days Since Upload, Median Gap


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

CATEGORY_LABELS: dict[str, str] = {
    "documentary": "Documentary",
    "true_crime": "True Crime",
    "vlog": "Vlogging",
    "travel": "Travel",
    "gaming": "Gaming",
    "tech": "Tech Review",
    "software_dev": "Software & Coding",
    "ai_tools": "AI & Tools",
    "finance": "Finance & Investing",
    "business_marketing": "Business & Marketing",
    "real_estate": "Real Estate",
    "fitness": "Fitness",
    "health_nutrition": "Health & Nutrition",
    "food_cooking": "Food & Cooking",
    "education": "Education",
    "youtube_growth": "YouTube Growth",
    "science": "Science",
    "news_commentary": "News & Commentary",
    "podcast_interview": "Podcast & Interview",
    "reaction": "Reaction",
    "comedy_entertainment": "Comedy & Entertainment",
    "beauty_fashion": "Beauty & Fashion",
    "automotive": "Automotive",
    "diy_crafts": "DIY & Crafts",
    "gardening_outdoors": "Gardening & Outdoors",
    "sports": "Sports",
    "music": "Music",
    "photography_film": "Photography & Film",
    "motivation_selfhelp": "Motivation & Self-Help",
    "kids_family": "Kids & Family",
    "pets_animals": "Pets & Animals",
    "books_writing": "Books & Writing",
    "other": "Other",
}


def tab_name(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " ").title())


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
            ws = _retry(lambda: self.sheet.add_worksheet(title=name, rows=1000, cols=len(HEADERS)))
            self._init_tab(ws)
        self._tabs[name] = ws
        return ws

    def _init_tab(self, ws: gspread.Worksheet) -> None:
        _retry(lambda: ws.update(values=[list(HEADERS)], range_name="A1"))
        try:
            _retry(lambda: ws.freeze(rows=1))
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

    # -- capacity ------------------------------------------------------------
    def _ensure_capacity(self, ws: gspread.Worksheet, needed_rows: int) -> None:
        """Direct range writes (values.update), unlike append_rows, don't
        grow the grid on their own -- expand ahead of a write that would
        land past the tab's current row_count (created at 1000)."""
        if ws.row_count < needed_rows:
            try:
                _retry(lambda: ws.add_rows(needed_rows - ws.row_count + 200))
            except APIError:
                pass

    # -- date sections -------------------------------------------------------
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

    def sort_by_subscribers(self, name: str) -> None:
        """Unused today. Note if this gets wired up later: Subscribers is now
        display text ('55.6k'), so this would sort alphabetically, not
        numerically -- sort on a hidden raw-number column instead."""
        ws = self._tabs.get(name)
        if ws is None:
            return
        try:
            _retry(lambda: ws.sort((2, "des"), range=f"A2:{gspread.utils.rowcol_to_a1(ws.row_count, len(HEADERS))}"))
        except APIError:
            pass


def row_from_lead(lead: sqlite3.Row | dict[str, Any], for_sheet: bool = False) -> list[Any]:
    """for_sheet=True renders Subscribers as '55.6k' for human reading in
    Google Sheets. CSV keeps the raw number -- it's meant for other tools to
    filter/sort on, not to look at directly."""
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
        format_subs(subs) if for_sheet else subs,
        val("channel_id"),
        val("url"),
        tab_name(str(val("category", "other")) or "other"),
        val("email"),
        val("instagram"),
        val("facebook"),
        val("twitter"),
        val("tiktok"),
        val("linkedin"),
        val("discord"),
        val("telegram"),
        val("website"),
        val("other_links"),
        val("video_count", 0),
        val("view_count", 0),
        val("last_upload"),
        val("days_since_upload"),
        val("median_gap_days"),
        val("country"),
        (str(val("created_at")) or "")[:10],
        val("discovered_via"),
        (str(val("first_seen")) or "")[:10],
    ]


def push(
    writer: SheetsWriter,
    leads: Iterable[sqlite3.Row | dict[str, Any]],
    master_tab: str = "Youtube Leads",
    tab_per_category: bool = True,
    date_key: str | None = None,
    date_label: str | None = None,
) -> dict[str, int]:
    """Write leads to the master tab and their per-category tabs.

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

    if tab_per_category:
        buckets: dict[str, list[list[Any]]] = {}
        for lead in leads:
            g = lead.__getitem__ if isinstance(lead, sqlite3.Row) else lead.get
            try:
                cat = g("category") or "other"
            except (IndexError, KeyError):
                cat = "other"
            buckets.setdefault(tab_name(str(cat)), []).append(row_from_lead(lead, for_sheet=True))
        for name, rows in buckets.items():
            n = writer.append(name, rows)
            if n:
                written[name] = n

    return written
