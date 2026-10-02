"""Remembering which leads have already been seen.

Without this, a scraper on a daily schedule hands you the same twenty posts
every morning and you have to diff them by eye. With it, `--only-new` answers
the question that actually matters: what came in since yesterday?

Only tweet IDs and a timestamp are stored — not the tweets. The file stays
small, and it holds nothing that would matter if it leaked.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from x_leads.auth.session import STATE_DIR

LOGGER = logging.getLogger(__name__)

DEFAULT_STATE_PATH = STATE_DIR / "seen.json"

# Tweets fall out of the search window long before this; the expiry just stops
# the file growing without bound.
RETENTION_DAYS = 90


class SeenStore:
    """Tweet IDs already reported, with an age-based expiry.

    Loading a corrupt or missing file yields an empty store rather than
    raising: a broken cache should cost you a duplicate lead, never a run.
    """

    def __init__(self, path: Path | None = None, retention_days: int = RETENTION_DAYS):
        self.path = Path(path or DEFAULT_STATE_PATH)
        self.retention_days = retention_days
        self._seen: dict[str, float] = {}
        self.load()

    def load(self) -> int:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as e:
            LOGGER.warning(
                "Ignoring unreadable seen-store at %s (%s)", self.path, type(e).__name__
            )
            return 0

        entries = raw.get("seen") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return 0

        cutoff = time.time() - self.retention_days * 86400
        self._seen = {
            k: v for k, v in entries.items()
            if isinstance(v, (int, float)) and v > cutoff
        }
        return len(self._seen)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated": time.time(), "seen": self._seen}
        # Written via a temp file so an interrupted run cannot leave a
        # half-written store that the next one refuses to read.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.path)

    def __contains__(self, tweet_id: str) -> bool:
        return tweet_id in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def add(self, tweet_id: str) -> None:
        self._seen[tweet_id] = time.time()

    def add_all(self, tweet_ids) -> int:
        added = 0
        for tweet_id in tweet_ids:
            if tweet_id not in self._seen:
                added += 1
            self.add(tweet_id)
        return added

    def filter_new(self, leads: list) -> list:
        """Only the leads whose tweet hasn't been reported before."""
        return [lead for lead in leads if lead.tweet.tweet_id not in self._seen]

    def clear(self) -> None:
        self._seen.clear()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
