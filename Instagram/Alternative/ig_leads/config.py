"""Settings, and where the credentials live.

Credentials are read from the environment or a `.env` file beside the package —
never from source. The old scripts had the password sitting in
`insta_discover_by_keyword.py`, which meant it went into git and into every
copy of the file.

Pacing scales with the size of the run. What Instagram acts on is sustained
volume and write actions, not the gap between any two reads - a person browsing
opens profiles every few seconds. So a short read-only burst runs at roughly
human speed, and only long sweeps get the slow, heavily spaced treatment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
PROJECT_DIR = PACKAGE_DIR.parent

# Everything stateful lives together, outside the repo, so a `git clean` cannot
# destroy a session and force a fresh login.
STATE_DIR = Path(os.getenv("IG_LEADS_HOME") or (Path.home() / ".ig_lead_scraper"))
SESSION_PATH = STATE_DIR / "session.json"
BUDGET_PATH = STATE_DIR / "budget.json"
LOGIN_PROFILE_DIR = STATE_DIR / "login_profile"


def _load_dotenv() -> None:
    """Minimal .env reader. Real env vars always win."""
    for candidate in (PROJECT_DIR / ".env", Path.cwd() / ".env"):
        if not candidate.exists():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
        except OSError:
            pass


_load_dotenv()


def username() -> str:
    return os.getenv("IG_USERNAME", "")


def password() -> str:
    return os.getenv("IG_PASSWORD", "")


# ---------------------------------------------------------------- ban safety
@dataclass(slots=True)
class Pacing:
    """How slowly to work. Every default here is a safety margin, not a guess.

    `min_gap_s`/`max_gap_s` are the pause between API calls. Randomised because
    a constant interval is itself a signature — nothing human requests a page
    every exactly-25 seconds.

    `long_break_every` inserts a much bigger pause periodically, which is what
    real browsing looks like: bursts of attention separated by gaps.
    """

    min_gap_s: float = 18.0
    max_gap_s: float = 42.0
    long_break_every: int = 12
    long_break_min_s: float = 90.0
    long_break_max_s: float = 210.0

    # Risk here is about SUSTAINED volume, not the gap between any two
    # requests: a person browsing Instagram opens profiles every few seconds,
    # so a short read-only burst at human speed is unremarkable, while 400
    # requests an hour is not, at any spacing.
    #
    # So the gap scales with how big the run actually is. A 12-request check
    # used to take 6 minutes at the pacing meant for a 68-request sweep, which
    # bought no safety and just made small runs painful.
    # Set False when the operator names a gap explicitly - their number wins.
    adaptive: bool = True

    small_run_requests: int = 30
    small_run_min_gap_s: float = 5.0
    small_run_max_gap_s: float = 12.0

    def for_run(self, planned_requests: int) -> "Pacing":
        """A copy paced for a run of this size. Large runs are unchanged."""
        if not self.adaptive or planned_requests > self.small_run_requests:
            return self
        return Pacing(
            min_gap_s=min(self.min_gap_s, self.small_run_min_gap_s),
            max_gap_s=min(self.max_gap_s, self.small_run_max_gap_s),
            # A burst this short never reaches a long break anyway.
            long_break_every=0,
            daily_requests=self.daily_requests,
            run_requests=self.run_requests,
            failure_streak_limit=self.failure_streak_limit,
            small_run_requests=self.small_run_requests,
        )

    # Hard ceilings. `daily_requests` persists across runs, so running the tool
    # five times in an afternoon cannot quietly spend five days of budget.
    daily_requests: int = 400
    run_requests: int = 200

    # Consecutive failures before the run stops. Instagram serves soft errors
    # before it serves hard ones; a streak means it has already noticed.
    failure_streak_limit: int = 3


@dataclass(slots=True)
class Filters:
    """What counts as a lead. Matches the brief:

    followers between 1k and 500k, and at least 5 posts in the last 14 days.
    """

    min_followers: int = 1_000
    max_followers: int = 500_000
    min_recent_posts: int = 5
    recent_days: int = 14
    # A private account cannot be assessed and cannot be pitched effectively.
    skip_private: bool = True


@dataclass(slots=True)
class Settings:
    pacing: Pacing = field(default_factory=Pacing)
    filters: Filters = field(default_factory=Filters)
    headless: bool = True
