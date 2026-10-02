"""SQLite persistence.

Two jobs, both load-bearing:
  1. Remember every channel we have ever touched so we never spend quota on it
     twice -- including the ones we rejected for not being podcasts.
  2. Hold the canonical lead rows, so the CSV and the Google Sheet can be
     rebuilt or resynced without re-scraping anything.

The schema is additive-migrating: `_migrate()` adds any column the running
code expects and the file on disk lacks. That is what lets a database
written by the old niche-scraper keep its "never revisit" memory instead of
being thrown away when the podcast columns arrive.
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
    channel_id             TEXT PRIMARY KEY,
    title                  TEXT,
    handle                 TEXT,
    url                    TEXT,
    subscribers            INTEGER,
    video_count            INTEGER,
    view_count             INTEGER,
    country                TEXT,
    created_at             TEXT,

    -- podcast profile
    genre                  TEXT,
    genre_score            REAL,
    podcast_format         TEXT,
    podcast_score          REAL,
    podcast_confidence     TEXT,
    podcast_signals        TEXT,
    host_name              TEXT,
    language               TEXT,

    -- publishing shape
    last_upload            TEXT,
    days_since_upload      REAL,
    median_gap_days        REAL,
    episodes_per_month     REAL,
    uploads_sampled        INTEGER,
    median_episode_minutes REAL,
    longest_episode_minutes REAL,
    shorts_ratio           REAL,
    avg_episode_views      INTEGER,

    -- contact + distribution
    email                  TEXT,
    instagram              TEXT,
    facebook               TEXT,
    twitter                TEXT,
    tiktok                 TEXT,
    linkedin               TEXT,
    discord                TEXT,
    telegram               TEXT,
    website                TEXT,
    spotify                TEXT,
    apple_podcasts         TEXT,
    other_platform         TEXT,
    rss_feed               TEXT,
    booking_link           TEXT,
    membership_link        TEXT,
    other_links            TEXT,

    description            TEXT,
    status                 TEXT NOT NULL,
    reason                 TEXT,
    discovered_via         TEXT,
    run_id                 INTEGER,
    first_seen             TEXT,
    last_checked           TEXT,
    synced                 INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_channels_status ON channels(status);
CREATE INDEX IF NOT EXISTS idx_channels_synced ON channels(synced);
CREATE INDEX IF NOT EXISTS idx_channels_genre  ON channels(genre);
CREATE INDEX IF NOT EXISTS idx_channels_run    ON channels(run_id);

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
    "rejected_not_podcast",
    "rejected_subs",
    "rejected_inactive",
    "rejected_no_contact",
    "rejected_few_videos",
    "rejected_duplicate_contact",
    "error",
)

LEAD_FIELDS: tuple[str, ...] = (
    "channel_id", "title", "handle", "url", "subscribers", "video_count",
    "view_count", "country", "created_at",
    "genre", "genre_score", "podcast_format", "podcast_score",
    "podcast_confidence", "podcast_signals", "host_name", "language",
    "last_upload", "days_since_upload", "median_gap_days", "episodes_per_month",
    "uploads_sampled", "median_episode_minutes", "longest_episode_minutes",
    "shorts_ratio", "avg_episode_views",
    "email", "instagram", "facebook", "twitter", "tiktok", "linkedin",
    "discord", "telegram", "website", "spotify", "apple_podcasts",
    "other_platform", "rss_feed", "booking_link", "membership_link",
    "other_links",
    "description", "status", "reason", "discovered_via",
)

# column -> SQL type, for the additive migration below.
_COLUMN_TYPES: dict[str, str] = {
    "genre": "TEXT", "genre_score": "REAL", "podcast_format": "TEXT",
    "podcast_score": "REAL", "podcast_confidence": "TEXT",
    "podcast_signals": "TEXT", "host_name": "TEXT", "language": "TEXT",
    "episodes_per_month": "REAL", "median_episode_minutes": "REAL",
    "longest_episode_minutes": "REAL", "shorts_ratio": "REAL",
    "avg_episode_views": "INTEGER", "spotify": "TEXT",
    "apple_podcasts": "TEXT", "other_platform": "TEXT", "rss_feed": "TEXT",
    "booking_link": "TEXT", "membership_link": "TEXT", "run_id": "INTEGER",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        # Set by start_run(); every row written afterwards is stamped with it,
        # which is what makes "just this run's leads" a real query instead of
        # a guess based on timestamps.
        self.current_run: int | None = None

    def _migrate(self) -> None:
        """Add any expected column the file on disk is missing.

        SQLite's ALTER TABLE ADD COLUMN is cheap and non-destructive, so a
        database from an earlier version of this scraper keeps every channel
        it already knows about -- which is the entire point of the DB.
        """
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(channels)")}
        if not have:
            return
        for col, sql_type in _COLUMN_TYPES.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE channels ADD COLUMN {col} {sql_type}")

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

        Podcast networks run several shows off one booking address, and a
        host with three shows uses the same inbox for all of them. Without
        this, one person lands in the outreach list two or three times.
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
        row["run_id"] = self.current_run
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

    def update_profile(
        self, channel_id: str, genre: str, podcast_format: str,
        host_name: str, language: str, reason: str,
    ) -> None:
        """Backfill the LLM-derived fields on an existing lead.

        Only non-empty values overwrite -- a later pass that fails to name
        the host must not wipe a host name an earlier pass got right.
        """
        sets = ["genre = ?", "reason = ?"]
        vals: list[Any] = [genre, reason]
        for col, value in (
            ("podcast_format", podcast_format),
            ("host_name", host_name),
            ("language", language),
        ):
            if value:
                sets.append(f"{col} = ?")
                vals.append(value)
        vals.append(channel_id)
        self.conn.execute(
            f"UPDATE channels SET {', '.join(sets)} WHERE channel_id = ?", vals
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

    def leads_for_run(self, run_id: int | None) -> list[sqlite3.Row]:
        """Only the leads this run found -- the per-run CSV.

        Channels are never re-evaluated once they are in the DB, so a lead
        belongs to exactly one run and this can never repeat yesterday's
        rows. A run that found nothing returns an empty list rather than
        silently falling back to the full table.
        """
        if run_id is None:
            return []
        return list(self.conn.execute(
            "SELECT * FROM channels WHERE status = ? AND run_id = ? "
            "ORDER BY subscribers DESC",
            (QUALIFIED, run_id),
        ))

    def last_run_id(self) -> int | None:
        """The most recent run that actually produced leads -- what
        `main.py csv` defaults to when invoked on its own."""
        row = self.conn.execute(
            "SELECT run_id FROM channels WHERE status = ? AND run_id IS NOT NULL "
            "ORDER BY run_id DESC LIMIT 1",
            (QUALIFIED,),
        ).fetchone()
        return int(row[0]) if row else None

    def status_counts(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT status, COUNT(*) FROM channels GROUP BY status ORDER BY 2 DESC"
        )
        return {r[0]: r[1] for r in cur}

    def genre_counts(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT genre, COUNT(*) FROM channels WHERE status = ? "
            "GROUP BY genre ORDER BY 2 DESC",
            (QUALIFIED,),
        )
        return {r[0] or "other": r[1] for r in cur}

    def format_counts(self) -> dict[str, int]:
        cur = self.conn.execute(
            "SELECT podcast_format, COUNT(*) FROM channels WHERE status = ? "
            "GROUP BY podcast_format ORDER BY 2 DESC",
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
        self.current_run = int(cur.lastrowid or 0)
        return self.current_run

    def end_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET ended_at = ?, stats = ? WHERE id = ?",
            (_utcnow(), json.dumps(stats), run_id),
        )
        self.conn.commit()
