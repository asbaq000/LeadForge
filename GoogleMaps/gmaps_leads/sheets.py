"""Push leads into Google Sheets via a service account.

Each client type gets its own tab — a Dentist tab, a Gyms tab, and so on — so
the sheet reads like a set of call lists rather than one undifferentiated pile.

Nothing is ever written twice. Before appending, every tab in the spreadsheet
is read and indexed by Place ID *and* by phone number, so a business already
sitting on the Dentist tab is not re-added when a later "dental clinic" run
turns it up again. The sheet is append-only otherwise: notes you type into
spare columns survive every run.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .models import (
    NAME_COL,
    PHONE_COL,
    PLACE_ID_COL,
    SHEET_COLUMNS,
    Lead,
    dedupe_key,
)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Google rejects these in a tab title, and a stray apostrophe breaks the
# quoted A1 ranges we build for the batch read.
_BAD_TAB_CHARS = re.compile(r"['\\\[\]*?:/]")

_SHEET_KEY_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)")


class SheetsError(RuntimeError):
    pass


def sheet_key_from_url(value: str) -> str:
    """Accept a full Sheets URL or a bare key, return the key."""
    value = (value or "").strip()
    if not value:
        return ""
    m = _SHEET_KEY_RE.search(value)
    if m:
        return m.group(1)
    # Already a key: a long opaque string with no slashes.
    return value if "/" not in value else ""


def tab_title(niche: str) -> str:
    """Turn a client type into a tab name: 'dental clinic' -> 'Dental Clinic'."""
    clean = _BAD_TAB_CHARS.sub(" ", (niche or "Leads").strip())
    clean = re.sub(r"\s+", " ", clean).strip() or "Leads"
    # Title-case, but leave acronyms the user typed in caps alone.
    words = [w if w.isupper() else w.capitalize() for w in clean.split(" ")]
    return " ".join(words)[:95]


class SheetWriter:
    """Writes leads into one spreadsheet, one tab per client type."""

    def __init__(
        self,
        credentials_path: str | None = None,
        sheet_name: str = "Maps Leads",
        sheet_key: str | None = None,
        share_with: str | None = None,
    ):
        self.credentials_path = credentials_path or os.getenv(
            "GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"
        )
        self.sheet_name = sheet_name
        self.sheet_key = sheet_key_from_url(sheet_key or "") or None
        self.share_with = share_with
        self._client = None
        self._sh = None
        self._tabs: dict[str, object] = {}
        # Place ids and phone keys already in the spreadsheet, across all tabs.
        self._seen_ids: set[str] = set()
        self._seen_keys: set[str] = set()
        self._index_loaded = False

    # --- connection --------------------------------------------------------

    def connect(self) -> "SheetWriter":
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise SheetsError(
                "Google Sheets support needs gspread and google-auth. "
                "Run: pip install -r requirements.txt"
            ) from exc

        cred_file = Path(self.credentials_path)
        if not cred_file.exists():
            raise SheetsError(
                f"Service account key not found at {cred_file.resolve()}.\n"
                "See the README, section 'Connecting Google Sheets'."
            )

        creds = Credentials.from_service_account_file(str(cred_file), scopes=_SCOPES)
        self._client = gspread.authorize(creds)

        if self.sheet_key:
            try:
                self._sh = self._client.open_by_key(self.sheet_key)
            except Exception as exc:
                who = self.service_account_email or "the service account"
                raise SheetsError(
                    f"Could not open the spreadsheet ({exc}). "
                    f"Share it with {who} as an Editor, then try again."
                ) from exc
        else:
            try:
                self._sh = self._client.open(self.sheet_name)
            except Exception:
                self._sh = self._client.create(self.sheet_name)
                if self.share_with:
                    # A sheet created by a service account is invisible to you
                    # until it is shared with your own Google account.
                    self._sh.share(self.share_with, perm_type="user", role="writer")
        return self

    @property
    def service_account_email(self) -> str:
        """The robot account that has to be given Editor access on the sheet."""
        try:
            import json

            data = json.loads(Path(self.credentials_path).read_text(encoding="utf-8"))
            return data.get("client_email", "")
        except Exception:
            return ""

    @property
    def url(self) -> str:
        return self._sh.url if self._sh else ""

    @property
    def title(self) -> str:
        return self._sh.title if self._sh else ""

    # --- tabs --------------------------------------------------------------

    def worksheet_for(self, niche: str):
        """Get (or create) the tab for one client type, header in place."""
        if not self._sh:
            raise SheetsError("Call connect() first.")
        title = tab_title(niche)
        if title in self._tabs:
            return self._tabs[title]

        try:
            ws = self._sh.worksheet(title)
        except Exception:
            ws = self._sh.add_worksheet(
                title=title, rows=1000, cols=len(SHEET_COLUMNS)
            )
        self._ensure_header(ws)
        self._tabs[title] = ws
        return ws

    def _ensure_header(self, ws) -> None:
        try:
            existing = ws.row_values(1)
        except Exception:
            existing = []
        if existing[: len(SHEET_COLUMNS)] == SHEET_COLUMNS:
            return
        ws.update(values=[SHEET_COLUMNS], range_name="A1", value_input_option="RAW")
        try:
            ws.freeze(rows=1)
            last = chr(ord("A") + len(SHEET_COLUMNS) - 1)
            ws.format(
                f"A1:{last}1",
                {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.92, "green": 0.94, "blue": 0.98},
                },
            )
        except Exception:
            # Formatting is cosmetic; never fail a run over it.
            pass

    def tab_names(self) -> list[str]:
        if not self._sh:
            return []
        return [ws.title for ws in self._sh.worksheets()]

    # --- dedupe index ------------------------------------------------------

    def load_index(self, force: bool = False) -> int:
        """Read every tab and remember what is already in the spreadsheet.

        One batch call for the whole spreadsheet, so this costs the same
        whether you have two tabs or twenty.
        """
        if self._index_loaded and not force:
            return len(self._seen_ids)
        if not self._sh:
            raise SheetsError("Call connect() first.")

        self._seen_ids.clear()
        self._seen_keys.clear()

        sheets = self._sh.worksheets()
        ranges = [f"'{ws.title}'!A2:{chr(ord('A') + len(SHEET_COLUMNS) - 1)}"
                  for ws in sheets]
        blocks: list[list[list[str]]] = []
        try:
            resp = self._sh.values_batch_get(ranges)
            blocks = [vr.get("values", []) for vr in resp.get("valueRanges", [])]
        except Exception:
            for ws in sheets:
                try:
                    blocks.append(ws.get_all_values()[1:])
                except Exception:
                    blocks.append([])

        for rows in blocks:
            for row in rows:
                if len(row) >= PLACE_ID_COL and row[PLACE_ID_COL - 1].strip():
                    self._seen_ids.add(row[PLACE_ID_COL - 1].strip())
                name = row[NAME_COL - 1] if len(row) >= NAME_COL else ""
                phone = row[PHONE_COL - 1] if len(row) >= PHONE_COL else ""
                key = dedupe_key(name, phone)
                if key:
                    self._seen_keys.add(key)

        self._index_loaded = True
        return len(self._seen_ids)

    def is_duplicate(self, lead: Lead) -> bool:
        if lead.place_id in self._seen_ids:
            return True
        key = dedupe_key(lead.name, lead.phone, lead.address)
        return bool(key and key in self._seen_keys)

    # --- writing -----------------------------------------------------------

    def append_leads(self, leads: list[Lead], niche: str = "") -> list[Lead]:
        """Append the leads not already in the spreadsheet, to the niche's tab.

        Returns the leads actually written, so the caller can mark exactly
        those as pushed.
        """
        if not self._sh:
            raise SheetsError("Call connect() first.")
        if not leads:
            return []

        self.load_index()
        fresh: list[Lead] = []
        for lead in leads:
            if self.is_duplicate(lead):
                continue
            fresh.append(lead)
            # Index as we go: two leads in this same batch can collide too.
            self._seen_ids.add(lead.place_id)
            key = dedupe_key(lead.name, lead.phone, lead.address)
            if key:
                self._seen_keys.add(key)
        if not fresh:
            return []

        ws = self.worksheet_for(niche or (fresh[0].niche or "Leads"))
        rows = [lead.to_row() for lead in fresh]
        # Chunked so a large run does not trip the Sheets payload limit.
        for i in range(0, len(rows), 500):
            ws.append_rows(
                rows[i : i + 500],
                value_input_option="RAW",
                insert_data_option="INSERT_ROWS",
            )
        return fresh
