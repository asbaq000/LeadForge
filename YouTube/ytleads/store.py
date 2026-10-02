"""SQLite persistence.

Two jobs, both load-bearing:
  1. Remember every channel we have ever touched so we never spend quota on it
     twice -- including the ones we rejected.
  2. Hold the canonical lead rows, so the Google Sheet can be rebuilt or
     resynced without re-scraping anything.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "leads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    channel_id        TEXT PRIMARY KEY,
    title             TEXT,
    handle            TEXT,
    url               TEXT,
    subscribers       INTEGER,
    video_count       INTEGER,
    view_count        INTEGER,
    country           TEXT,
    created_at        TEXT,
    category          TEXT,
    category_score    REAL,
    last_upload       TEXT,
    days_since_upload REAL,
    median_gap_days   REAL,
    uploads_sampled   INTEGER,
    email             TEXT,
    instagram         TEXT,
    facebook          TEXT,
    twitter           TEXT,
    tiktok            TEXT,
    linkedin          TEXT,
    discord           TEXT,
    telegram          TEXT,
    website           TEXT,
    other_links       TEXT,
    description       TEXT,
    status            TEXT NOT NULL,
    reason            TEXT,
    discovered_via    TEXT,
    first_seen        TEXT,
    last_checked      TEXT,
    synced            INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_channels_status ON channels(status);
CREATE INDEX IF NOT EXISTS idx_channels_synced ON channels(synced);
CREATE INDEX IF NOT EXISTS idx_channels_category ON channels(category);

CREATE TABLE IF NOT EXISTS quota (
    day   TEXT PRIMARY KEY,
    units INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seeds_done (
    seed  TEXT NOT NULL,
    page  INTEGER NOT NULL,
    day   TEXT NOT NULL,
    PRIMARY KEY (seed, page, day)
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    ended_at   TEXT,
    stats      TEXT
);
"""

# Statuses that mean "we looked, it did not qualify" -- still never revisited.
QUALIFIED = "qualified"
REJECT_STATUSES = (
    "rejected_subs",
    "rejected_inactive",
    "rejected_no_contact",
    "rejected_few_videos",
    "rejected_duplicate_contact",
    "excluded_animation",
    "excluded_motion_graphics",
    "error",
)

LEAD_FIELDS: tuple[str, ...] = (
    "channel_id", "title", "handle", "url", "subscribers", "video_count",
    "view_count", "country", "created_at", "category", "category_score",
    "last_upload", "days_since_upload", "median_gap_days", "uploads_sampled",
    "email", "instagram", "facebook", "twitter", "tiktok", "linkedin",
    "discord", "telegram", "website", "other_links", "description",
    "status", "reason", "discovered_via",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- dedupe ------------------------------------------------------------
    def known_ids(self) -> set[str]:
        """Every channel we have ever evaluated, qualified or not."""
        cur = self.conn.execute("SELECT channel_id FROM channels")
        return {r[0] for r in cur}

    def email_owner(self, email: str, exclude: str = "") -> str | None:
        """Channel that already claimed this email, if any.

        Creators routinely run several channels off one business address.
        Without this, one person lands in the outreach list two or three times.
        """
        if not email:
            return None
        row = self.conn.execute(
            "SELECT channel_id FROM channels "
            "WHERE email = ? AND status = ? AND channel_id != ? LIMIT 1",
            (email.lower(), QUALIFIED, exclude),
        ).fetchone()
        return row[0] if row else None

    def filter_new(self, ids: Iterable[str]) -> list[str]:
        known = self.known_ids()
        out, seen = [], set()
        for cid in ids:
            if cid and cid not in known and cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out

    # -- writes ------------------------------------------------------------
    def upsert(self, row: dict[str, Any]) -> None:
        row = {k: row.get(k) for k in LEAD_FIELDS}
        cid = row["channel_id"]
        if not cid:
            raise ValueError("upsert requires channel_id")
        now = _utcnow()
        existing = self.conn.execute(
            "SELECT first_seen FROM channels WHERE channel_id = ?", (cid,)
        ).fetchone()
        row["first_seen"] = existing["first_seen"] if existing else now
        row["last_checked"] = now

        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "channel_id")
        self.conn.execute(
            f"INSERT INTO channels ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(channel_id) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )

    def commit(self) -> None:
        self.conn.commit()

    def update_category(self, channel_id: str, category: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE channels SET category = ?, reason = ? WHERE channel_id = ?",
            (category, reason, channel_id),
        )

    def mark_synced(self, channel_ids: Sequence[str]) -> None:
        self.conn.executemany(
            "UPDATE channels SET synced = 1 WHERE channel_id = ?",
            [(c,) for c in channel_ids],
        )
        self.conn.commit()

    # -- reads -------------------------------------------------------------
    def unsynced_leads(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM channels WHERE status = ? AND synced = 0 "
            "ORDER BY subscribers DESC",
            (QUALIFIED,),
        ))

    def all_leads(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM channels WHERE status = ? ORDER BY subscribers DESC",
            (QUALIFIED,),
        ))

    def status_counts(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT status, COUNT(*) FROM channels GROUP BY status ORDER BY 2 DESC"
        )
        return {r[0]: r[1] for r in cur}

    def category_counts(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT category, COUNT(*) FROM channels WHERE status = ? "
            "GROUP BY category ORDER BY 2 DESC",
            (QUALIFIED,),
        )
        return {r[0] or "other": r[1] for r in cur}

    # -- quota -------------------------------------------------------------
    def quota_used(self, day: str | None = None) -> int:
        day = day or date.today().isoformat()
        row = self.conn.execute(
            "SELECT units FROM quota WHERE day = ?", (day,)
        ).fetchone()
        return row[0] if row else 0

    def add_quota(self, units: int, day: str | None = None) -> int:
        day = day or date.today().isoformat()
        self.conn.execute(
            "INSERT INTO quota (day, units) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET units = units + excluded.units",
            (day, units),
        )
        self.conn.commit()
        return self.quota_used(day)

    # -- seeds -------------------------------------------------------------
    def seed_done_today(self, seed: str, page: int) -> bool:
        day = date.today().isoformat()
        return self.conn.execute(
            "SELECT 1 FROM seeds_done WHERE seed=? AND page=? AND day=?",
            (seed, page, day),
        ).fetchone() is not None

    def mark_seed_done(self, seed: str, page: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seeds_done (seed, page, day) VALUES (?,?,?)",
            (seed, page, date.today().isoformat()),
        )
        self.conn.commit()

    # -- runs --------------------------------------------------------------
    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (_utcnow(),)
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def end_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET ended_at = ?, stats = ? WHERE id = ?",
            (_utcnow(), json.dumps(stats), run_id),
        )
        self.conn.commit()
