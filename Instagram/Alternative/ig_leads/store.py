"""The running record of every lead ever found.

One CSV, appended to by every run, holding leads only. It does two jobs:

1. **It is the deliverable.** Terminal output scrolls away; this does not.
2. **It stops the scraper paying twice for the same account.** Known leads are
   dropped from the candidate list *before* any request is spent on them, which
   is the cheapest possible filter — a hashtag returns roughly the same 30
   accounts each time it is searched, so without this a second run through the
   same niche spends most of its budget re-confirming yesterday's results.

Appended rather than rewritten, so a crash mid-run cannot destroy the history,
and `first_seen` records when each account was first found.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from ig_leads.models import CSV_FIELDS, Account

LOGGER = logging.getLogger(__name__)

DEFAULT_LEADS_DIR = "leads"
MASTER_FILENAME = "all_leads.csv"

# `first_seen` is added on top of the normal account columns.
STORE_FIELDS = ["first_seen"] + CSV_FIELDS


class LeadStore:
    """Append-only CSV of leads, keyed by username."""

    def __init__(self, directory: str | Path = DEFAULT_LEADS_DIR,
                 filename: str = MASTER_FILENAME):
        self.directory = Path(directory)
        self.path = self.directory / filename
        self.known: set[str] = set()
        self.load()

    def load(self) -> int:
        """Usernames already recorded. A broken file must not lose the run."""
        self.known = set()
        if not self.path.exists():
            return 0
        try:
            with self.path.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    username = (row.get("username") or "").strip()
                    if username:
                        self.known.add(username.lower())
        except (OSError, csv.Error) as e:
            LOGGER.warning(
                "Could not read %s (%s) — treating every lead as new. "
                "The file is not modified.", self.path, type(e).__name__,
            )
            return 0
        return len(self.known)

    def __contains__(self, username: str) -> bool:
        return (username or "").lower() in self.known

    def __len__(self) -> int:
        return len(self.known)

    def filter_new(self, accounts: list[Account]) -> list[Account]:
        return [a for a in accounts if a.username.lower() not in self.known]

    def append(self, accounts: list[Account]) -> int:
        """Add leads not already recorded. Returns how many were written."""
        fresh = [
            a for a in accounts
            if a.is_lead and a.username.lower() not in self.known
        ]
        if not fresh:
            return 0

        self.directory.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

        with self.path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=STORE_FIELDS, extrasaction="ignore",
                lineterminator="\n",
            )
            if new_file:
                writer.writeheader()
            for account in fresh:
                writer.writerow({"first_seen": stamp, **account.to_dict()})
                self.known.add(account.username.lower())

        LOGGER.info("Appended %d new leads to %s", len(fresh), self.path)
        return len(fresh)
