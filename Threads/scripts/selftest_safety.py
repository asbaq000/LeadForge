"""Tests for the account-protection layer - no browser, no network.

Verifies the guards that keep the Threads account alive actually fire:
rate ceilings, the escalating circuit breaker, active-hours gating, session
length caps and the single-instance lock.

    python scripts/selftest_safety.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from threads_leads.config import load_settings  # noqa: E402
from threads_leads.safety import (  # noqa: E402
    BudgetExhausted,
    CooldownActive,
    InstanceLock,
    OutsideActiveHours,
    SafetyGovernor,
)
from threads_leads.store import Store  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(label: str, passed: bool) -> None:
    RESULTS.append((label, bool(passed)))
    print(f"  {'PASS' if passed else 'FAIL'}  {label}")


def raises(exc_type, fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception as exc:
        print(f"        (raised {type(exc).__name__} instead: {exc})")
        return False
    return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="threads_safety_"))
    s = load_settings()
    s.db_path = tmp / "safety.db"
    s.lock_path = tmp / "safety.lock"
    s.active_hours = (0, 24)

    store = Store(s.db_path)
    guard = SafetyGovernor(s, store, log=lambda *_: None)

    print("Rate ceilings")
    check("clean state allows searching", guard.allow_search())
    for _ in range(s.max_searches_per_hour):
        guard.record("search")
    check(
        f"blocks after {s.max_searches_per_hour} searches in an hour",
        not guard.allow_search(),
    )
    check("action counter reads back correctly",
          store.actions_since("search", hours=1) == s.max_searches_per_hour)

    print("\nProfile-visit ceiling")
    guard2 = SafetyGovernor(s, store, log=lambda *_: None)
    check("profile visits allowed initially", guard2.allow_profile_visit())
    for _ in range(s.max_profile_visits_per_hour):
        guard2.record("profile")
    check("blocks once the profile ceiling is hit", not guard2.allow_profile_visit())

    print("\nDaily budget")
    check(
        "preflight rejects once the daily search budget is spent",
        raises(BudgetExhausted, guard2.preflight)
        if store.actions_since("search", hours=24) >= s.max_searches_per_day
        else True,
    )

    print("\nCircuit breaker")
    store2 = Store(tmp / "breaker.db")
    guard3 = SafetyGovernor(s, store2, log=lambda *_: None)
    guard3.preflight()  # clean state - must not raise
    check("clean breaker passes preflight", True)

    guard3.trip("simulated challenge page")
    check("tripping the breaker blocks the next run", raises(CooldownActive, guard3.preflight))

    until, reason, strikes = store2.breaker_state()
    check("strike recorded", strikes == 1)
    check("reason recorded", "simulated challenge" in reason)
    first_window = (until - datetime.now(timezone.utc)).total_seconds() / 3600
    ladder = s.cooldown_hours_on_challenge
    check(
        f"first cooldown is ~{ladder[0]}h (got {first_window:.1f}h)",
        ladder[0] <= first_window <= ladder[0] * 1.3,
    )

    guard3.trip("second challenge")
    until2, _, strikes2 = store2.breaker_state()
    second_window = (until2 - datetime.now(timezone.utc)).total_seconds() / 3600
    check("second strike recorded", strikes2 == 2)
    check(
        f"cooldown escalates on repeat strikes ({first_window:.1f}h -> {second_window:.1f}h)",
        second_window > first_window,
    )

    print("\nActive hours")
    s_night = load_settings()
    s_night.db_path = tmp / "night.db"
    store3 = Store(s_night.db_path)
    guard4 = SafetyGovernor(s_night, store3, log=lambda *_: None)
    current = guard4.persona_now().hour
    # A window that deliberately excludes the account's current hour.
    s_night.active_hours = ((current + 2) % 24, (current + 3) % 24)
    check("refuses to run outside active hours", raises(OutsideActiveHours, guard4.preflight))

    # The gate must follow the ACCOUNT's timezone, not the machine's.
    s_tz = load_settings()
    s_tz.db_path = tmp / "tz.db"
    store_tz = Store(s_tz.db_path)
    s_tz.timezone_id = "Asia/Karachi"
    g_pk = SafetyGovernor(s_tz, store_tz, log=lambda *_: None)
    s_tz2 = load_settings()
    s_tz2.db_path = tmp / "tz2.db"
    s_tz2.timezone_id = "America/Los_Angeles"
    g_la = SafetyGovernor(s_tz2, Store(s_tz2.db_path), log=lambda *_: None)
    pk_hour, la_hour = g_pk.persona_now().hour, g_la.persona_now().hour
    check(
        f"persona clock tracks configured timezone (Karachi {pk_hour:02d}h vs LA {la_hour:02d}h)",
        pk_hour != la_hour,
    )

    s_bad = load_settings()
    s_bad.db_path = tmp / "bad.db"
    s_bad.timezone_id = "Not/ARealZone"
    g_bad = SafetyGovernor(s_bad, Store(s_bad.db_path), log=lambda *_: None)
    check("unknown timezone falls back to machine clock instead of crashing",
          isinstance(g_bad.persona_now(), datetime))

    print("\nSession length cap")
    s_short = load_settings()
    s_short.db_path = tmp / "short.db"
    s_short.session_max_minutes = 0  # anything elapsed is over budget
    guard5 = SafetyGovernor(s_short, Store(s_short.db_path), log=lambda *_: None)
    time.sleep(0.05)
    check("session cap stops further searches", not guard5.allow_search())

    print("\nSingle-instance lock")
    lock_a = InstanceLock(s.lock_path, enabled=True)
    lock_a.acquire()
    check("lock file created", s.lock_path.exists())
    lock_b = InstanceLock(s.lock_path, enabled=True)
    # Same PID counts as alive, so a second acquire must be refused.
    check("second instance refused", raises(RuntimeError, lock_b.acquire))
    lock_a.release()
    check("lock released on exit", not s.lock_path.exists())

    print("\nStrike forgiveness")
    store4 = Store(tmp / "forgive.db")
    guard6 = SafetyGovernor(s, store4, log=lambda *_: None)
    guard6.trip("old incident")
    store4.conn.execute(
        "UPDATE breaker SET last_clean_run = ?, cooldown_until = ? WHERE id = 1",
        ("2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00"),
    )
    store4.conn.commit()
    guard6.preflight()  # elapsed cooldown + old clean run => strikes reset
    _, _, strikes_after = store4.breaker_state()
    check("old strikes are forgiven after the reset window", strikes_after == 0)

    for st in (store, store2, store3, store4):
        try:
            st.close()
        except Exception:
            pass
    shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
