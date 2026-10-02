"""A request budget that survives process exit.

The point is narrow and important: an in-memory cap only limits one run. The
realistic way this account gets banned is not one aggressive run — it is
running the tool five times in an afternoon because the first pass looked
interesting, each run politely staying under its own limit while together they
make a day's worth of traffic in an hour.

So the counter lives on disk, keyed by UTC date, and every run spends from the
same pot.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from ig_leads.config import BUDGET_PATH
from ig_leads.errors import BudgetExhausted

LOGGER = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RequestBudget:
    """Counts API calls against a daily ceiling and a per-run ceiling."""

    def __init__(
        self,
        daily_limit: int,
        run_limit: int,
        path: Path | None = None,
        today: str | None = None,
    ):
        self.daily_limit = daily_limit
        self.run_limit = run_limit
        self.path = Path(path or BUDGET_PATH)
        self._today = today or _today()
        self.spent_today = self._load()
        self.spent_this_run = 0

    def _load(self) -> int:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as e:
            # A broken counter must not read as "plenty of budget left".
            # Assume the worst and make the operator clear it deliberately.
            LOGGER.warning(
                "Budget file unreadable (%s) — treating today as fully spent. "
                "Delete %s to reset.", type(e).__name__, self.path,
            )
            return self.daily_limit
        if raw.get("date") != self._today:
            return 0
        return int(raw.get("spent", 0))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": self._today,
            "spent": self.spent_today,
            "updated": time.time(),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def remaining_today(self) -> int:
        return max(0, self.daily_limit - self.spent_today)

    @property
    def remaining_this_run(self) -> int:
        return max(0, self.run_limit - self.spent_this_run)

    @property
    def remaining(self) -> int:
        return min(self.remaining_today, self.remaining_this_run)

    def check(self, needed: int = 1) -> None:
        """Raise before spending, so a run stops cleanly rather than mid-call."""
        if self.remaining_today < needed:
            raise BudgetExhausted(
                f"Daily request budget spent ({self.spent_today}/{self.daily_limit}). "
                f"This resets at UTC midnight. Raising it is how accounts get "
                f"banned — prefer running again tomorrow."
            )
        if self.remaining_this_run < needed:
            raise BudgetExhausted(
                f"Per-run request limit reached ({self.spent_this_run}/{self.run_limit})."
            )

    def spend(self, n: int = 1) -> None:
        self.spent_today += n
        self.spent_this_run += n
        # Written every time: a crash must not hand back budget that was used.
        self.save()

    def summary(self) -> str:
        return (
            f"{self.spent_this_run} requests this run, "
            f"{self.spent_today}/{self.daily_limit} today"
        )
