# LeadForge · asbaq000
"""Threads lead scraper - command line entry point.

  python run.py check                 verify setup (session, sheet, config)
  python run.py scrape                collect + score + write to Google Sheets
  python run.py scrape --dry-run      collect + score, print instead of writing
  python run.py scrape --limit 10     cap this run at 10 leads
  python run.py stats                 show what has been collected
  python run.py safety                show account-protection state
  python run.py score "some text"     test the scoring rules against a sample post
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from threads_leads.config import load_settings
from threads_leads.pipeline import Pipeline
from threads_leads.scorer import LeadScorer
from threads_leads.store import Store


# Threads posts are full of emoji and non-Latin script. The Windows console
# defaults to cp1252, which raises UnicodeEncodeError on the first emoji and
# kills the run. Force UTF-8 and degrade gracefully if a glyph is unmappable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _log(msg: str = "") -> None:
    print(msg, flush=True)


def cmd_check(args) -> int:
    s = load_settings(args.config)
    ok = True

    _log("Configuration")
    _log(f"  queries              {len(s.queries)}")
    _log(f"  hashtags             {len(s.hashtags)}")
    _log(f"  scoring groups       {len(s.score_groups)}")
    _log(f"  daily target         {s.daily_target}")
    _log(f"  min score            {s.min_score}")
    _log(f"  headless             {s.headless}")

    _log("\nSafety")
    _log(f"  searches/hour        max {s.max_searches_per_hour}")
    _log(f"  searches/day         max {s.max_searches_per_day}")
    _log(f"  session length       max {s.session_max_minutes} min")
    from threads_leads.safety import SafetyGovernor

    persona = SafetyGovernor(s, Store(s.db_path), log=_log).persona_now()
    _log(
        f"  active hours         {s.active_hours[0]:02d}:00-{s.active_hours[1]:02d}:00 "
        f"{s.timezone_id} (now {persona.strftime('%H:%M')})"
    )
    if s.proxy_server:
        _log(f"  proxy                {s.proxy_server}")
    else:
        _log("  proxy                (none - traffic exits from this machine's IP)")

    _log("\nThreads session")
    profile_ready = s.user_data_dir.exists() and any(s.user_data_dir.iterdir())
    if profile_ready:
        _log(f"  OK   persistent profile at {s.user_data_dir}")
    elif s.storage_state.exists():
        _log(f"  OK   cookie backup at {s.storage_state} (will migrate into profile)")
    else:
        ok = False
        _log(f"  MISSING  no profile at {s.user_data_dir}")
        _log("       -> run: python scripts/login.py")

    _log("\nGoogle Sheets")
    if not s.sheet_id:
        ok = False
        _log("  MISSING  GOOGLE_SHEET_ID not set in .env")
    elif not s.service_account_json.exists():
        ok = False
        _log(f"  MISSING  {s.service_account_json}")
    else:
        try:
            from threads_leads.sheets import SheetWriter

            writer = SheetWriter(s, log=lambda m: _log(f"  {m}"))
            existing = writer.existing_post_ids()
            _log(f"  OK   connected to tab '{s.sheet_tab}' ({len(existing)} rows already there)")
        except Exception as exc:
            ok = False
            _log(f"  FAIL {exc}")

    _log("\nPlaywright")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        _log("  OK   chromium launches")
    except Exception as exc:
        ok = False
        _log(f"  FAIL {exc}")
        _log("       -> run: python -m playwright install chromium")

    _log("\n" + ("All checks passed." if ok else "Some checks failed - see above."))
    return 0 if ok else 1


def cmd_scrape(args) -> int:
    s = load_settings(args.config)
    if args.min_score is not None:
        s.min_score = args.min_score
    if args.headful:
        s.headless = False

    pipe = Pipeline(s, log=_log)
    try:
        summary = pipe.run(
            limit=args.limit,
            dry_run=args.dry_run,
            sync_sheet=not args.no_sheet,
        )
    finally:
        pipe.close()

    # Hitting a safety ceiling or the daily target is the system working, not
    # failing. Only a block, a network abort or an exception is a real failure -
    # otherwise Task Scheduler would flag almost every healthy run as an error.
    note = summary.get("note", "ok")
    healthy = ("ok", "stopped on safety budget", "skipped:")
    return 0 if note.startswith(healthy) else 2


def cmd_stats(args) -> int:
    s = load_settings(args.config)
    store = Store(s.db_path)
    st = store.stats()
    _log(f"Posts seen (all time)   {st['posts_seen_total']}")
    _log(f"Leads exported          {st['leads_exported_total']}")
    _log(f"Today                   {st['today']} / {s.daily_target}")
    _log("\nLast 7 days")
    if not st["last_7_days"]:
        _log("  (nothing yet)")
    for day, count in st["last_7_days"]:
        bar = "#" * min(count, 60)
        flag = "" if count >= s.daily_target else "  <- under target"
        _log(f"  {day}  {count:>3}  {bar}{flag}")

    rows = store.conn.execute(
        "SELECT started_at, seen, qualified, exported, note FROM runs "
        "ORDER BY rowid DESC LIMIT 5"
    ).fetchall()
    if rows:
        _log("\nRecent runs")
        for r in rows:
            _log(
                f"  {r['started_at'][:19]}  seen={r['seen']:<5} "
                f"qualified={r['qualified']:<4} exported={r['exported']:<4} {r['note']}"
            )
    store.close()
    return 0


def cmd_safety(args) -> int:
    """Show the account-protection state: budgets used, strikes, cooldown."""
    from threads_leads.safety import SafetyGovernor

    s = load_settings(args.config)
    store = Store(s.db_path)
    status = SafetyGovernor(s, store, log=_log).status()

    _log("Action budget")
    _log(f"  searches, last hour   {status['searches_last_hour']:>3} / {s.max_searches_per_hour}")
    _log(f"  searches, last 24h    {status['searches_last_24h']:>3} / {s.max_searches_per_day}")
    _log(f"  profiles, last hour   {status['profiles_last_hour']:>3} / {s.max_profile_visits_per_hour}")

    _log("\nCircuit breaker")
    if status["cooldown_until"]:
        _log(f"  strikes               {status['strikes']}")
        _log(f"  cooling down until    {status['cooldown_until']}")
        _log(f"  reason                {status['cooldown_reason']}")
    else:
        _log(f"  strikes               {status['strikes']}")
        _log("  status                clear - safe to run")

    if args.reset:
        store.conn.execute(
            "UPDATE breaker SET cooldown_until = NULL, reason = '', strikes = 0 WHERE id = 1"
        )
        store.conn.commit()
        _log("\nBreaker manually cleared. Only do this if you know why it tripped.")

    store.close()
    return 0


def cmd_score(args) -> int:
    s = load_settings(args.config)
    verdict = LeadScorer(s).score(args.text)
    _log(f"score          {verdict.score}")
    _log(f"level          {verdict.level}")
    _log(f"signals        {', '.join(verdict.signals) or '-'}")
    _log(f"matched        {' | '.join(verdict.matched_terms) or '-'}")
    _log(f"topics         {', '.join(verdict.topics) or '-'}")
    if verdict.reject_reason:
        _log(f"rejected       {verdict.reject_reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run.py", description="Threads lead scraper for AI/automation services"
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="verify setup")
    p_check.set_defaults(func=cmd_check)

    p_scrape = sub.add_parser("scrape", help="collect and export leads")
    p_scrape.add_argument("--limit", type=int, default=None, help="max leads this run")
    p_scrape.add_argument("--dry-run", action="store_true", help="print instead of writing")
    p_scrape.add_argument("--no-sheet", action="store_true", help="CSV only, skip Google Sheets")
    p_scrape.add_argument("--headful", action="store_true", help="show the browser window")
    p_scrape.add_argument("--min-score", type=int, default=None, help="override min_score")
    p_scrape.set_defaults(func=cmd_scrape)

    p_stats = sub.add_parser("stats", help="show collection stats")
    p_stats.set_defaults(func=cmd_stats)

    p_safety = sub.add_parser("safety", help="show account-protection state")
    p_safety.add_argument("--reset", action="store_true", help="clear a tripped breaker")
    p_safety.set_defaults(func=cmd_safety)

    p_score = sub.add_parser("score", help="test scoring on a sample post")
    p_score.add_argument("text", help="post text to score")
    p_score.set_defaults(func=cmd_score)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if args.command == "scrape":
        _log(f"=== Threads lead scrape @ {started} ===")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
