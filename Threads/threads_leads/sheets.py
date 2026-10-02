"""Google Sheets output (plus a local CSV mirror so a Sheets outage never loses leads)."""

from __future__ import annotations

import csv
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound

from .config import Settings

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

HEADERS = [
    "Captured At (UTC)",
    "Score",
    "Level",
    "Author",
    "Name",
    "Followers",
    "Verified",
    "Post Text",
    "Post URL",
    "Profile URL",
    "Posted At (UTC)",
    "Topics",
    "Signals",
    "Matched Terms",
    "Emails",
    "Links",
    "Likes",
    "Replies",
    "Reposts",
    "Matched Query",
    "Suggested DM Opener",
    "Status",
    "Owner",
    "Notes",
    "Post ID",
]

POST_ID_COL = len(HEADERS)  # 1-indexed column of "Post ID"


def row_from_lead(lead: dict) -> list:
    """Order a lead dict into the sheet's column layout."""
    return [
        lead.get("captured_at", ""),
        lead.get("score", 0),
        lead.get("level", ""),
        lead.get("username", ""),
        lead.get("full_name", ""),
        lead.get("followers", 0),
        "YES" if lead.get("is_verified") else "",
        lead.get("text", ""),
        lead.get("permalink", ""),
        lead.get("profile_url", ""),
        lead.get("posted_at", ""),
        ", ".join(lead.get("topics", [])),
        ", ".join(lead.get("signals", [])),
        " | ".join(lead.get("matched_terms", [])),
        ", ".join(lead.get("emails", [])),
        ", ".join(lead.get("links", [])),
        lead.get("like_count", 0),
        lead.get("reply_count", 0),
        lead.get("repost_count", 0),
        lead.get("matched_query", ""),
        lead.get("opener", ""),
        "New",
        "",
        "",
        lead.get("post_id", ""),
    ]


class SheetWriter:
    def __init__(self, settings: Settings, log=print):
        self.s = settings
        self.log = log
        self.ws = None
        self._connect()

    def _connect(self) -> None:
        if not self.s.sheet_id:
            raise RuntimeError("GOOGLE_SHEET_ID is not set in .env")
        key_path = Path(self.s.service_account_json)
        if not key_path.exists():
            raise FileNotFoundError(
                f"Service-account key not found at {key_path}. "
                "See README section 'Google Sheets setup'."
            )

        creds = Credentials.from_service_account_file(str(key_path), scopes=SCOPES)
        client = gspread.authorize(creds)

        try:
            sh = client.open_by_key(self.s.sheet_id)
        except SpreadsheetNotFound as exc:
            raise RuntimeError(
                f"Spreadsheet {self.s.sheet_id} not found or not shared with the service "
                f"account ({creds.service_account_email}). Share the sheet with that address "
                "as an Editor."
            ) from exc
        except APIError as exc:
            raise RuntimeError(
                f"Google API rejected the request: {exc}. Most often this means the sheet "
                f"is not shared with {creds.service_account_email}."
            ) from exc

        try:
            self.ws = sh.worksheet(self.s.sheet_tab)
        except WorksheetNotFound:
            self.ws = sh.add_worksheet(
                title=self.s.sheet_tab, rows=2000, cols=len(HEADERS)
            )
            self.log(f"  created tab '{self.s.sheet_tab}'")

        self._ensure_header()

    def _ensure_header(self) -> None:
        first_row = self.ws.row_values(1)
        if first_row[: len(HEADERS)] == HEADERS:
            return
        if not first_row:
            self.ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
            try:
                self.ws.freeze(rows=1)
                self.ws.format(
                    f"A1:{chr(64 + len(HEADERS))}1",
                    {"textFormat": {"bold": True}},
                )
            except APIError:
                pass
            self.log("  header row written")
        else:
            self.log("  ! existing header differs from expected layout - appending anyway")

    def existing_post_ids(self) -> list[str]:
        try:
            values = self.ws.col_values(POST_ID_COL)
        except APIError:
            return []
        return [v.strip() for v in values[1:] if v and v.strip()]

    def append(self, leads: list[dict]) -> int:
        if not leads:
            return 0
        rows = [row_from_lead(lead) for lead in leads]
        self.ws.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
        return len(rows)


def append_csv_backup(path: Path, leads: list[dict]) -> None:
    """Mirror every exported lead locally. Cheap insurance."""
    if not leads:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(HEADERS)
        for lead in leads:
            writer.writerow(row_from_lead(lead))
