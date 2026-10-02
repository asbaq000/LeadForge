"""Local SQLite store: cross-run dedupe, resume, and CSV export.

The database is what makes a 1,000-lead run survivable. Scraping that many
listings takes hours and Google will interrupt you. Every lead is persisted the
moment it is found, so a killed run resumes instead of starting over, and
re-running next week adds only genuinely new businesses.

Dedupe happens twice over. `place_id` is the primary key, which catches the
same listing surfacing under several search phrasings or neighbouring tiles.
`dkey` (see models.dedupe_key) catches the same business listed twice under
different place ids, which Google does after a shop moves or re-registers.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from .models import SHEET_COLUMNS, Lead, dedupe_key

_TABLES = """
CREATE TABLE IF NOT EXISTS leads (
    place_id     TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    category     TEXT DEFAULT '',
    address      TEXT DEFAULT '',
    phone        TEXT DEFAULT '',
    website      TEXT DEFAULT '',
    email        TEXT DEFAULT '',
    email_source TEXT DEFAULT '',
    rating       REAL,
    reviews      INTEGER,
    maps_url     TEXT DEFAULT '',
    city         TEXT DEFAULT '',
    country      TEXT DEFAULT '',
    niche        TEXT DEFAULT '',
    query        TEXT DEFAULT '',
    lat          REAL,
    lng          REAL,
    scraped_at   TEXT DEFAULT '',
    dkey         TEXT DEFAULT '',
    verified     INTEGER DEFAULT 0,
    enriched     INTEGER DEFAULT 0,
    pushed       INTEGER DEFAULT 0
);

-- Remembers which (query, tile) pairs are already done, so a resumed run
-- skips straight to unscraped ground.
CREATE TABLE IF NOT EXISTS done_tiles (
    city  TEXT NOT NULL,
    query TEXT NOT NULL,
    lat   REAL NOT NULL,
    lng   REAL NOT NULL,
    found INTEGER DEFAULT 0,
    PRIMARY KEY (city, query, lat, lng)
);
"""

# Built after _migrate(), since an older database reaches this point without
# the columns some of these index.
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_leads_city ON leads(city);
CREATE INDEX IF NOT EXISTS idx_leads_niche ON leads(niche);
CREATE INDEX IF NOT EXISTS idx_leads_pushed ON leads(pushed);
CREATE INDEX IF NOT EXISTS idx_leads_enriched ON leads(enriched);
CREATE INDEX IF NOT EXISTS idx_leads_dkey ON leads(dkey);
CREATE INDEX IF NOT EXISTS idx_leads_verified ON leads(verified);
"""

_LEAD_FIELDS = [
    "place_id", "name", "category", "address", "phone", "website",
    "email", "email_source", "rating", "reviews", "maps_url",
    "city", "country", "niche", "query", "lat", "lng", "scraped_at",
    "verified",
]

# Columns added after the first release. A database from an earlier run is
# migrated in place rather than thrown away.
_ADDED_COLUMNS = {
    "country": "TEXT DEFAULT ''",
    "niche": "TEXT DEFAULT ''",
    "dkey": "TEXT DEFAULT ''",
    "verified": "INTEGER DEFAULT 0",
}


class LeadStore:
    def __init__(self, path: str | Path = "data/leads.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The web UI reads the store from request handlers while the worker
        # thread writes to it, so the connection has to cross threads.
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        # WAL lets the UI read counts while the scraper is still writing.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.conn.executescript(_TABLES)
        self._migrate()
        self.conn.executescript(_INDEXES)
        self.conn.commit()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(leads)")}
        for column, decl in _ADDED_COLUMNS.items():
            if column not in have:
                self.conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {decl}")
        # Backfill dedupe keys for rows written before the column existed.
        rows = self.conn.execute(
            "SELECT place_id, name, phone, address FROM leads "
            "WHERE dkey IS NULL OR dkey = ''"
        ).fetchall()
        if rows:
            self.conn.executemany(
                "UPDATE leads SET dkey = ? WHERE place_id = ?",
                [
                    (dedupe_key(r["name"], r["phone"], r["address"]), r["place_id"])
                    for r in rows
                ],
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LeadStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- scope -------------------------------------------------------------

    @staticmethod
    def _scope(city: str | None, niche: str | None) -> tuple[str, list]:
        """Build the WHERE fragment shared by every read."""
        parts, args = [], []
        if city:
            parts.append("city = ?")
            args.append(city)
        if niche:
            parts.append("niche = ?")
            args.append(niche)
        return (" AND ".join(parts), args)

    # --- writing -----------------------------------------------------------

    def add_many(self, leads: list[Lead]) -> int:
        """Insert leads we do not already hold. Returns how many were new."""
        return len(self.add_returning(leads))

    def add_returning(self, leads: list[Lead]) -> list[Lead]:
        """Insert leads we do not already hold, and return the ones inserted.

        Skips anything matching an existing place id *or* an existing dedupe
        key, so one business never lands twice under two different listings.
        """
        if not leads:
            return []

        known = {
            row[0]
            for row in self.conn.execute("SELECT dkey FROM leads WHERE dkey != ''")
        }
        candidates: list[Lead] = []
        rows: list[tuple] = []
        in_batch: set[str] = set()
        for lead in leads:
            key = dedupe_key(lead.name, lead.phone, lead.address)
            if key and (key in known or key in in_batch):
                continue
            if key:
                in_batch.add(key)
            candidates.append(lead)
            rows.append(tuple(getattr(lead, f) for f in _LEAD_FIELDS) + (key,))

        if not rows:
            return []

        # Which of these place ids do we already hold? Anything left is new,
        # and the caller wants exactly those back for its live feed.
        held = set()
        ids = [lead.place_id for lead in candidates]
        for i in range(0, len(ids), 400):
            chunk = ids[i : i + 400]
            held.update(
                row[0]
                for row in self.conn.execute(
                    f"SELECT place_id FROM leads WHERE place_id IN "
                    f"({', '.join('?' for _ in chunk)})",
                    chunk,
                )
            )

        columns = _LEAD_FIELDS + ["dkey"]
        sql = (
            f"INSERT OR IGNORE INTO leads ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        self.conn.executemany(sql, rows)
        self.conn.commit()
        return [lead for lead in candidates if lead.place_id not in held]

    def update_contact(self, lead: Lead) -> None:
        """Write back a phone / address / website recovered from a detail page."""
        self.conn.execute(
            "UPDATE leads SET phone = ?, website = ?, address = ?, dkey = ?, "
            "verified = 1 WHERE place_id = ?",
            (
                lead.phone,
                lead.website,
                lead.address,
                dedupe_key(lead.name, lead.phone, lead.address),
                lead.place_id,
            ),
        )
        self.conn.commit()

    def update_email(self, place_id: str, email: str, source: str) -> None:
        self.conn.execute(
            "UPDATE leads SET email = ?, email_source = ?, enriched = 1 "
            "WHERE place_id = ?",
            (email, source, place_id),
        )
        self.conn.commit()

    def mark_enriched(self, place_ids: list[str]) -> None:
        self.conn.executemany(
            "UPDATE leads SET enriched = 1 WHERE place_id = ?",
            [(pid,) for pid in place_ids],
        )
        self.conn.commit()

    def mark_pushed(self, place_ids: list[str]) -> None:
        self.conn.executemany(
            "UPDATE leads SET pushed = 1 WHERE place_id = ?",
            [(pid,) for pid in place_ids],
        )
        self.conn.commit()

    def drop_with_website(self) -> int:
        """Remove leads that turned out to have a website after all."""
        cur = self.conn.execute(
            "DELETE FROM leads WHERE website IS NOT NULL AND website != ''"
        )
        self.conn.commit()
        return cur.rowcount

    def prune_duplicates(self) -> int:
        """Collapse rows sharing a dedupe key, keeping the richest one.

        Only bites when a phone number recovered later reveals that two
        listings we already stored are the same business.
        """
        cur = self.conn.execute(
            """
            DELETE FROM leads WHERE place_id IN (
                SELECT place_id FROM (
                    SELECT place_id, ROW_NUMBER() OVER (
                        PARTITION BY dkey
                        ORDER BY (email != '') DESC, (phone != '') DESC,
                                 COALESCE(reviews, 0) DESC
                    ) AS rank
                    FROM leads WHERE dkey != '' AND pushed = 0
                ) WHERE rank > 1
            )
            """
        )
        self.conn.commit()
        return cur.rowcount

    # --- tiles -------------------------------------------------------------

    def mark_tile_done(
        self, city: str, query: str, lat: float, lng: float, found: int
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO done_tiles (city, query, lat, lng, found) "
            "VALUES (?, ?, ?, ?, ?)",
            (city, query, lat, lng, found),
        )
        self.conn.commit()

    def is_tile_done(self, city: str, query: str, lat: float, lng: float) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM done_tiles WHERE city = ? AND query = ? "
            "AND lat = ? AND lng = ?",
            (city, query, lat, lng),
        ).fetchone()
        return row is not None

    def clear_tiles(self, city: str | None = None) -> None:
        if city:
            self.conn.execute("DELETE FROM done_tiles WHERE city = ?", (city,))
        else:
            self.conn.execute("DELETE FROM done_tiles")
        self.conn.commit()

    # --- reading -----------------------------------------------------------

    def _count_where(self, extra: str, city: str | None, niche: str | None) -> int:
        where, args = self._scope(city, niche)
        clauses = [c for c in (extra, where) if c]
        sql = "SELECT COUNT(*) FROM leads"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(sql, args).fetchone()[0]

    def count(self, city: str | None = None, niche: str | None = None) -> int:
        return self._count_where("", city, niche)

    def count_with_phone(self, city: str | None = None, niche: str | None = None) -> int:
        return self._count_where("phone != ''", city, niche)

    def count_with_email(self, city: str | None = None, niche: str | None = None) -> int:
        return self._count_where("email != ''", city, niche)

    def _select(self, where: str, args: list, order: str = "") -> list[Lead]:
        sql = f"SELECT {', '.join(_LEAD_FIELDS)} FROM leads"
        if where:
            sql += f" WHERE {where}"
        if order:
            sql += f" {order}"
        rows = self.conn.execute(sql, args).fetchall()
        return [Lead.from_dict(dict(r)) for r in rows]

    def all_leads(self, city: str | None = None, niche: str | None = None) -> list[Lead]:
        where, args = self._scope(city, niche)
        return self._select(where, args, "ORDER BY name")

    def needing_enrichment(
        self, city: str | None = None, niche: str | None = None, limit: int = 0
    ) -> list[Lead]:
        where, args = self._scope(city, niche)
        clauses = ["enriched = 0", "email = ''"] + ([where] if where else [])
        order = "ORDER BY reviews DESC NULLS LAST"
        if limit:
            order += f" LIMIT {int(limit)}"
        return self._select(" AND ".join(clauses), args, order)

    def missing_phone(
        self, city: str | None = None, niche: str | None = None
    ) -> list[Lead]:
        where, args = self._scope(city, niche)
        clauses = ["phone = ''"] + ([where] if where else [])
        return self._select(
            " AND ".join(clauses), args, "ORDER BY reviews DESC NULLS LAST"
        )

    def needs_detail_visit(
        self, city: str | None = None, niche: str | None = None
    ) -> list[Lead]:
        """Leads whose listing page still has something to tell us.

        Either we never got a phone off the card, or the search that found
        them could not show websites at all, so "no website" is still a guess.
        Unverified ones go first: a wrong lead costs more than a missing phone.
        """
        where, args = self._scope(city, niche)
        clauses = ["(phone = '' OR verified = 0)"] + ([where] if where else [])
        return self._select(
            " AND ".join(clauses), args,
            "ORDER BY verified ASC, reviews DESC NULLS LAST",
        )

    def count_unverified(
        self, city: str | None = None, niche: str | None = None
    ) -> int:
        return self._count_where("verified = 0", city, niche)

    def unpushed(self, city: str | None = None, niche: str | None = None) -> list[Lead]:
        where, args = self._scope(city, niche)
        clauses = ["pushed = 0"] + ([where] if where else [])
        return self._select(" AND ".join(clauses), args, "ORDER BY name")

    def niches(self) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT niche FROM leads WHERE niche != '' ORDER BY niche"
            )
        ]

    def summary_rows(self) -> list[dict]:
        """Per niche/city totals, for the dashboard table."""
        rows = self.conn.execute(
            """
            SELECT niche, city, country, COUNT(*) AS leads,
                   SUM(phone != '') AS phones,
                   SUM(email != '') AS emails,
                   SUM(pushed = 1) AS pushed
            FROM leads GROUP BY niche, city, country ORDER BY niche, city
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # --- export ------------------------------------------------------------

    def export_csv(
        self, path: str | Path, city: str | None = None, niche: str | None = None
    ) -> int:
        leads = self.all_leads(city, niche)
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig so Excel on Windows renders non-Latin names correctly.
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(SHEET_COLUMNS)
            for lead in leads:
                writer.writerow(lead.to_row())
        return len(leads)
