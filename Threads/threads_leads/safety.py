"""Account-preservation layer.

Three independent guards, because the failure modes are different:

  * RateLimiter    - never exceed a human-plausible volume of searches/profile hits
  * CircuitBreaker - on any challenge/login bounce, stop for hours (escalating),
                     rather than retrying straight into a permanent block
  * InstanceLock   - never drive one account from two processes at once

The breaker is the important one. Almost every account loss follows the same
shape: a soft rate-limit appears, the scraper keeps hammering, the soft limit
becomes a hard one. Backing off on the first signal is what prevents that.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore


class CooldownActive(RuntimeError):
    """Breaker is open - the account is resting."""


class OutsideActiveHours(RuntimeError):
    """It is the middle of the night for this account's persona."""


class BudgetExhausted(RuntimeError):
    """Hourly or daily action ceiling reached."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
#  Single-instance lock
# ---------------------------------------------------------------------------


class InstanceLock:
    """Cheap PID lockfile. Two concurrent sessions on one account is a loud flag."""

    def __init__(self, path: Path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self._held = False

    def acquire(self) -> None:
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
            except (ValueError, OSError):
                pid = 0
            if pid and _pid_alive(pid):
                raise RuntimeError(
                    f"Another scraper instance (pid {pid}) is already running. "
                    "Driving one account from two processes is a fast way to get "
                    f"flagged. If that PID is dead, delete {self.path}."
                )
            self.path.unlink(missing_ok=True)  # stale lock
        self.path.write_text(str(os.getpid()))
        self._held = True

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ---------------------------------------------------------------------------
#  Governor
# ---------------------------------------------------------------------------


class SafetyGovernor:
    """Wraps the store's action log and breaker state into a single gate."""

    def __init__(self, settings, store, log=print):
        self.s = settings
        self.store = store
        self.log = log
        self.session_start = time.monotonic()

    # ---- preflight ----

    def preflight(self) -> None:
        """Called once before the browser opens. Raises if this run must not happen."""
        self._check_breaker()
        self._check_active_hours()
        self._check_daily_budget()

    def _check_breaker(self) -> None:
        until, reason, strikes = self.store.breaker_state()
        if not until:
            return
        if _utcnow() < until:
            mins = int((until - _utcnow()).total_seconds() // 60)
            raise CooldownActive(
                f"Cooling down for another {mins // 60}h{mins % 60:02d}m "
                f"(strike {strikes}). Reason: {reason}. "
                "This is deliberate - retrying now is how accounts get banned."
            )
        # Cooldown elapsed. Decay the strike counter if we have been clean a while.
        self.store.maybe_reset_strikes(self.s.strike_reset_days)

    def persona_now(self) -> datetime:
        """Wall clock in the timezone the ACCOUNT lives in, not the machine's.

        These are usually the same, but not when the scraper runs on a VPS in
        another region - and in that case the machine clock is the wrong one to
        gate on. The account's own timezone is what has to look plausible.
        """
        tz_name = (self.s.timezone_id or "").strip()
        if ZoneInfo and tz_name:
            try:
                return datetime.now(ZoneInfo(tz_name))
            except Exception:
                self.log(
                    f"  [safety] unknown timezone {tz_name!r}; falling back to machine clock"
                )
        return datetime.now()

    def _check_active_hours(self) -> None:
        start, end = self.s.active_hours
        now = self.persona_now()
        hour = now.hour
        if not (start <= hour < end):
            raise OutsideActiveHours(
                f"It is {now.strftime('%H:%M')} for this account "
                f"({self.s.timezone_id or 'machine local'}), outside the active window "
                f"{start:02d}:00-{end:02d}:00. Real accounts sleep."
            )

    def _check_daily_budget(self) -> None:
        used = self.store.actions_since("search", hours=24)
        if used >= self.s.max_searches_per_day:
            raise BudgetExhausted(
                f"Daily search ceiling reached ({used}/{self.s.max_searches_per_day})."
            )

    # ---- per-action gates ----

    def allow_search(self) -> bool:
        if self._session_expired():
            return False
        hourly = self.store.actions_since("search", hours=1)
        if hourly >= self.s.max_searches_per_hour:
            self.log(f"  [safety] hourly search ceiling reached ({hourly}) - stopping run")
            return False
        daily = self.store.actions_since("search", hours=24)
        if daily >= self.s.max_searches_per_day:
            self.log(f"  [safety] daily search ceiling reached ({daily}) - stopping run")
            return False
        return True

    def allow_profile_visit(self) -> bool:
        if self._session_expired():
            return False
        hourly = self.store.actions_since("profile", hours=1)
        if hourly >= self.s.max_profile_visits_per_hour:
            self.log(f"  [safety] hourly profile ceiling reached ({hourly})")
            return False
        return True

    def _session_expired(self) -> bool:
        elapsed_min = (time.monotonic() - self.session_start) / 60.0
        if elapsed_min >= self.s.session_max_minutes:
            self.log(
                f"  [safety] session length cap hit ({elapsed_min:.0f}m / "
                f"{self.s.session_max_minutes}m) - wrapping up"
            )
            return True
        return False

    def record(self, kind: str) -> None:
        self.store.record_action(kind)

    # ---- breaker control ----

    def trip(self, reason: str) -> timedelta:
        ladder = self.s.cooldown_hours_on_challenge or [6, 24, 72]
        strikes = self.store.bump_strike()
        hours = ladder[min(strikes, len(ladder)) - 1]
        # Jitter so repeated trips don't resume on a suspiciously exact schedule.
        hours = hours * random.uniform(1.0, 1.25)
        until = _utcnow() + timedelta(hours=hours)
        self.store.open_breaker(until, reason, strikes)
        self.log(
            f"  [safety] CIRCUIT BREAKER TRIPPED (strike {strikes}). "
            f"Pausing all activity for {hours:.1f}h. Reason: {reason}"
        )
        return timedelta(hours=hours)

    def note_clean_run(self) -> None:
        self.store.note_clean_run()

    def status(self) -> dict:
        until, reason, strikes = self.store.breaker_state()
        return {
            "searches_last_hour": self.store.actions_since("search", hours=1),
            "searches_last_24h": self.store.actions_since("search", hours=24),
            "profiles_last_hour": self.store.actions_since("profile", hours=1),
            "strikes": strikes,
            "cooldown_until": until.isoformat() if until else None,
            "cooldown_reason": reason,
        }
