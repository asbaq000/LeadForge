"""SQLite state: de-duplication, author cooldown, daily quota tracking, raw archive."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id     TEXT PRIMARY KEY,
    author      TEXT,
    score       INTEGER,
    first_seen  TEXT,
    exported    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS authors (
    username        TEXT PRIMARY KEY,
    last_exported   TEXT,
    times_exported  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily (
    day       TEXT PRIMARY KEY,
    exported  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS raw (
    post_id     TEXT PRIMARY KEY,
    payload     TEXT,
    captured_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    started_at  TEXT,
    finished_at TEXT,
    seen        INTEGER,
    qualified   INTEGER,
    exported    INTEGER,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    ts    TEXT,
    kind  TEXT
);
CREATE TABLE IF NOT EXISTS breaker (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    cooldown_until TEXT,
    reason         TEXT,
    strikes        INTEGER DEFAULT 0,
    last_clean_run TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author);
CREATE INDEX IF NOT EXISTS idx_actions_ts ON actions(ts);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_key() -> str:
    return _utcnow().strftime("%Y-%m-%d")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- de-duplication ----------

    def seen_post(self, post_id: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,))
        return cur.fetchone() is not None

    def author_in_cooldown(self, username: str, cooldown_days: int) -> bool:
        """True if we already exported this person recently - avoids spamming one user."""
        if not username or cooldown_days <= 0:
            return False
        cur = self.conn.execute(
            "SELECT last_exported FROM authors WHERE username = ?", (username.lower(),)
        )
        row = cur.fetchone()
        if not row or not row["last_exported"]:
            return False
        try:
            last = datetime.fromisoformat(row["last_exported"])
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (_utcnow() - last) < timedelta(days=cooldown_days)

    def known_post_ids(self) -> set[str]:
        return {r["post_id"] for r in self.conn.execute("SELECT post_id FROM posts")}

    # ---------- writes ----------

    def mark_seen(self, post_id: str, author: str, score: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO posts (post_id, author, score, first_seen, exported) "
            "VALUES (?, ?, ?, ?, 0)",
            (post_id, (author or "").lower(), int(score), _utcnow().isoformat()),
        )
        self.conn.commit()

    def mark_exported(self, post_id: str, author: str) -> None:
        now = _utcnow().isoformat()
        self.conn.execute("UPDATE posts SET exported = 1 WHERE post_id = ?", (post_id,))
        self.conn.execute(
            "INSERT INTO authors (username, last_exported, times_exported) VALUES (?, ?, 1) "
            "ON CONFLICT(username) DO UPDATE SET "
            "last_exported = excluded.last_exported, times_exported = times_exported + 1",
            ((author or "").lower(), now),
        )
        day = today_key()
        self.conn.execute(
            "INSERT INTO daily (day, exported) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET exported = exported + 1",
            (day,),
        )
        self.conn.commit()

    def archive_raw(self, post_id: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO raw (post_id, payload, captured_at) VALUES (?, ?, ?)",
            (post_id, json.dumps(payload, ensure_ascii=False), _utcnow().isoformat()),
        )
        self.conn.commit()

    def import_existing_ids(self, post_ids: list[str]) -> int:
        """Seed the DB from IDs already present in the sheet (survives a DB wipe)."""
        now = _utcnow().isoformat()
        rows = [(pid, "", 0, now, 1) for pid in post_ids if pid]
        self.conn.executemany(
            "INSERT OR IGNORE INTO posts (post_id, author, score, first_seen, exported) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    # ---------- quota ----------

    def exported_today(self) -> int:
        cur = self.conn.execute("SELECT exported FROM daily WHERE day = ?", (today_key(),))
        row = cur.fetchone()
        return int(row["exported"]) if row else 0

    def remaining_today(self, daily_target: int) -> int:
        return max(0, daily_target - self.exported_today())

    def log_run(self, started: str, seen: int, qualified: int, exported: int, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO runs (started_at, finished_at, seen, qualified, exported, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (started, _utcnow().isoformat(), seen, qualified, exported, note),
        )
        self.conn.commit()

    def stats(self) -> dict:
        c = self.conn
        total = c.execute("SELECT COUNT(*) n FROM posts").fetchone()["n"]
        exported = c.execute("SELECT COUNT(*) n FROM posts WHERE exported = 1").fetchone()["n"]
        days = c.execute(
            "SELECT day, exported FROM daily ORDER BY day DESC LIMIT 7"
        ).fetchall()
        return {
            "posts_seen_total": total,
            "leads_exported_total": exported,
            "today": self.exported_today(),
            "last_7_days": [(r["day"], r["exported"]) for r in days],
        }

    # ---------- safety: action budget ----------

    def record_action(self, kind: str) -> None:
        self.conn.execute(
            "INSERT INTO actions (ts, kind) VALUES (?, ?)", (_utcnow().isoformat(), kind)
        )
        # Keep the table from growing forever; nothing older than a week matters.
        cutoff = (_utcnow() - timedelta(days=7)).isoformat()
        self.conn.execute("DELETE FROM actions WHERE ts < ?", (cutoff,))
        self.conn.commit()

    def actions_since(self, kind: str, hours: int) -> int:
        cutoff = (_utcnow() - timedelta(hours=hours)).isoformat()
        cur = self.conn.execute(
            "SELECT COUNT(*) n FROM actions WHERE kind = ? AND ts >= ?", (kind, cutoff)
        )
        return int(cur.fetchone()["n"])

    # ---------- safety: circuit breaker ----------

    def _breaker_row(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM breaker WHERE id = 1").fetchone()

    def breaker_state(self) -> tuple[datetime | None, str, int]:
        row = self._breaker_row()
        if not row:
            return None, "", 0
        until = None
        if row["cooldown_until"]:
            try:
                until = datetime.fromisoformat(row["cooldown_until"])
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
            except ValueError:
                until = None
        return until, row["reason"] or "", int(row["strikes"] or 0)

    def open_breaker(self, until: datetime, reason: str, strikes: int) -> None:
        self.conn.execute(
            "INSERT INTO breaker (id, cooldown_until, reason, strikes) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cooldown_until = excluded.cooldown_until, "
            "reason = excluded.reason, strikes = excluded.strikes",
            (until.isoformat(), reason, int(strikes)),
        )
        self.conn.commit()

    def bump_strike(self) -> int:
        row = self._breaker_row()
        strikes = int(row["strikes"] or 0) + 1 if row else 1
        self.conn.execute(
            "INSERT INTO breaker (id, strikes) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET strikes = excluded.strikes",
            (strikes,),
        )
        self.conn.commit()
        return strikes

    def note_clean_run(self) -> None:
        self.conn.execute(
            "INSERT INTO breaker (id, last_clean_run) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_clean_run = excluded.last_clean_run",
            (_utcnow().isoformat(),),
        )
        self.conn.commit()

    def maybe_reset_strikes(self, clean_days: int) -> None:
        """Forgive old strikes so one bad week doesn't permanently throttle the account."""
        row = self._breaker_row()
        if not row or not row["strikes"]:
            return
        last = row["last_clean_run"]
        if not last:
            return
        try:
            when = datetime.fromisoformat(last)
        except ValueError:
            return
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if (_utcnow() - when) >= timedelta(days=clean_days):
            self.conn.execute(
                "UPDATE breaker SET strikes = 0, cooldown_until = NULL, reason = '' WHERE id = 1"
            )
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()
