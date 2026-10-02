# LeadForge · asbaq000
"""
Daily lead generation: ten random niches in, ten leads out, one per niche.

    keywords.txt
        -> pick 10 at random             (a different slice of the list daily)
        -> for each: guess seeds, crawl  (keyword_seeds, related_crawler)
        -> read recent post timestamps   (instagram_parser)
        -> apply the criteria            (filters.shortlist)
        -> stop that niche at 1 lead     and move to the next
        -> deliver                       (Google Sheet, or CSV if no --sheet)

Criteria, all overridable:
    10,000 <= followers < 500,000
    at least 5 posts in the last 7 days
    10 leads per day, one per keyword
    never an account already delivered, ever

Two delivery modes, chosen automatically:

    --sheet given     -> append to that Google Sheet; the sheet's own
                          `username` column is the dedupe source of truth.
    --sheet omitted    -> write data/leads/leads-YYYY-MM-DD.csv. Re-running
                          the same day tops that file up to --daily-limit
                          instead of starting a second batch; a local ledger
                          (data/leads/delivered.txt) makes sure no account is
                          ever handed out twice, on any day.

Usage:
    python src/find_creators.py -k data/keywords.txt          # -> CSV
    python src/find_creators.py -k data/keywords.txt \
        --sheet https://docs.google.com/spreadsheets/d/<id>/edit \
        --credentials src/config/google-credentials.json      # -> Sheet

Google Sheets setup is documented at the top of src/outputs/sheets.py.
Add --dry-run to see the leads without writing anything anywhere.
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
for _sub in ("extractors", "outputs", "filters", "discovery"):
    sys.path.insert(0, os.path.join(_HERE, _sub))

import leads  # noqa: E402
import sheets  # noqa: E402
import shortlist  # noqa: E402
from instagram_parser import (  # noqa: E402
    ProfileNotFound,
    RateLimited,
    ScrapeError,
    SessionError,
    build_session,
    embedded_post_timestamps,
    fetch_post_timestamps,
    fetch_profile,
    parse_profile,
)
from related_crawler import crawl  # noqa: E402
from utils_format import normalize_username, read_usernames  # noqa: E402

DEFAULT_DAILY_LIMIT = 10
DEFAULT_PER_KEYWORD_BUDGET = 45


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Find small, actively-posting creators by niche.")
    p.add_argument("-k", "--keywords", help="keyword list file (one per line)")
    p.add_argument("-u", "--usernames", nargs="*", default=[],
                   help="skip discovery and evaluate these handles")
    p.add_argument("-i", "--input", help="file of handles to evaluate (skips discovery)")
    p.add_argument("-o", "--output-dir", default="data/run", help="run artifacts")
    p.add_argument("--leads-dir", default="data/leads", help="local delivered-leads mirror")

    p.add_argument("--sheet", help="Google Sheet URL or id to append leads to")
    p.add_argument("--credentials", default="src/config/google-credentials.json",
                   help="service account JSON key")
    p.add_argument("--worksheet", default=sheets.DEFAULT_WORKSHEET)
    p.add_argument("--dry-run", action="store_true",
                   help="find leads but write nothing to the sheet or the ledger")

    p.add_argument("--login", help="username whose saved instaloader session to use "
                    "instead of anonymous requests — raises the rate-limit ceiling a lot. "
                    "Create the session once with:  instaloader --login=<that username>")
    p.add_argument("--session-file", help="explicit path to the session file, "
                    "if not at instaloader's default location")

    p.add_argument("--daily-limit", type=int, default=DEFAULT_DAILY_LIMIT,
                   help="leads to deliver today, one per keyword (default 10)")
    p.add_argument("--per-keyword-budget", type=int, default=DEFAULT_PER_KEYWORD_BUDGET,
                   help="profile fetches to spend hunting one niche before giving up")
    p.add_argument("--seed", type=int, help="fix the random keyword pick (for reproducible runs)")

    p.add_argument("--max-followers", type=int, default=shortlist.DEFAULT_MAX_FOLLOWERS)
    p.add_argument("--min-followers", type=int, default=shortlist.DEFAULT_MIN_FOLLOWERS)
    p.add_argument("--min-posts", type=int, default=shortlist.DEFAULT_MIN_POSTS)
    p.add_argument("--window-days", type=int, default=shortlist.DEFAULT_WINDOW_DAYS)
    p.add_argument("--max-depth", type=int, default=2, help="related-profile hops from a seed")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    return p.parse_args(argv)


def read_keywords(path):
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def ensure_post_timestamps(session, record, delay=0.0):
    """
    Make sure a record has post timestamps, paying for a request only when
    it has to.

    Most profiles arrive with timestamps already lifted out of the profile
    payload for free. media_count is None — not 0 — whenever the fallback app
    id served the profile, so a confident 0 is the only safe reason to skip.
    """
    if record.get("post_timestamps"):
        return record
    if record.get("is_private") or not record.get("user_id"):
        record["post_timestamps"] = []
        return record
    if record.get("media_count") == 0:
        record["post_timestamps"] = []
        return record

    record["post_timestamps"] = fetch_post_timestamps(session, record["user_id"])
    if delay:
        time.sleep(delay)
    return record


def make_judge(session, criteria, seen, examined, delay):
    """
    Build the accept/reject callback the crawler calls on each keyword match.

    `seen` grows as leads are accepted, so a single account can never be
    delivered twice inside one run either.
    """
    def judge(record):
        if record["username"].lower() in seen:
            return False
        ensure_post_timestamps(session, record, delay)
        verdict = shortlist.evaluate(record, **criteria)
        examined.append(verdict)
        return verdict["verdict"] == shortlist.QUALIFIED

    return judge


def hunt_one_per_keyword(session, keywords, seen, criteria, args, log):
    """
    Walk the chosen keywords, taking the first qualifying account from each.

    Returns (leads, per_keyword_notes, stopped_early). Each keyword gets its
    own crawl with target=1, so one barren niche can't drain the budget for
    the rest — and a rate-limit stops the whole sweep cleanly.
    """
    found, notes, examined = [], [], []
    stopped_early = False

    for index, keyword in enumerate(keywords, start=1):
        log(f"\n[{index}/{len(keywords)}] {keyword}")

        winner = []
        judge = make_judge(session, criteria, seen, examined, args.delay)

        def take(record, _winner=winner, _judge=judge, _keyword=keyword):
            if _winner or not _judge(record):
                return False
            record["matched_keyword"] = _keyword
            _winner.append(record)
            return True

        _, stats = crawl(
            session,
            [keyword],
            max_profiles=args.per_keyword_budget,
            max_depth=args.max_depth,
            delay=args.delay,
            log=log,
            checkpoint_path=os.path.join(args.output_dir, "crawl.checkpoint.jsonl"),
            skip=seen,
            on_candidate=take,
            target=1,
        )

        if winner:
            record = winner[0]
            verdict = shortlist.evaluate(record, **criteria)
            verdict["matched_keyword"] = keyword
            found.append(verdict)
            seen.add(record["username"].lower())
            notes.append({"keyword": keyword, "lead": record["username"],
                          "fetches": stats["fetched"]})
            log(f"    -> @{record['username']} "
                f"({record['follower_count']:,} followers, "
                f"{verdict['posts_last_week']} posts/{args.window_days}d)")
        else:
            reason = ("rate limited" if stats.get("rate_limited")
                      else "budget spent" if stats["fetched"] >= args.per_keyword_budget
                      else "no more accounts reachable")
            notes.append({"keyword": keyword, "lead": None, "fetches": stats["fetched"],
                          "reason": reason})
            log(f"    -> nothing qualified ({reason}, {stats['fetched']} fetches)")

        if stats.get("rate_limited"):
            log("\n  ! Anonymous rate limit hit — stopping the sweep here. "
                "Re-run in ~15 minutes; the checkpoint means nothing is refetched.")
            stopped_early = True
            break

    return found, notes, examined, stopped_early


def evaluate_supplied(session, handles, seen, criteria, args, log):
    """The -u / -i path: no discovery, just check the handles given."""
    log(f"Evaluating {len(handles)} supplied handle(s) — skipping discovery.\n")
    found, examined = [], []
    for handle in handles:
        if handle.lower() in seen:
            log(f"  - @{handle}: already delivered previously, skipping")
            continue
        try:
            user = fetch_profile(session, handle)
        except RateLimited as exc:
            log(f"  ! rate limited — stopping. {exc}")
            break
        except (ProfileNotFound, ScrapeError) as exc:
            log(f"  - @{handle}: {exc}")
            continue

        record = parse_profile(user)
        record["user_id"] = str(user.get("id") or "")
        record["is_private"] = bool(user.get("is_private"))
        record["post_timestamps"] = embedded_post_timestamps(user)
        record["keywords"] = []
        record["found_via"] = "supplied"

        ensure_post_timestamps(session, record, args.delay)
        verdict = shortlist.evaluate(record, **criteria)
        examined.append(verdict)

        if verdict["verdict"] == shortlist.QUALIFIED:
            found.append(verdict)
            seen.add(record["username"].lower())
            log(f"  * @{record['username']:<26} {record['follower_count']:>9,} followers")
            if len(found) >= args.daily_limit:
                break
        time.sleep(args.delay)
    return found, examined


def load_seen(args, log):
    """
    Every account already delivered — the sheet is authoritative, the local
    mirror covers rows deleted by hand and runs made before the sheet existed.
    Returns (seen, worksheet_or_None).
    """
    seen = {u.lower() for u in leads.load_ledger(args.leads_dir)}
    if seen:
        log(f"Local ledger: {len(seen)} account(s) already delivered.")

    if not args.sheet:
        return seen, None

    ws = sheets.connect(args.sheet, args.credentials, args.worksheet)
    in_sheet = sheets.existing_usernames(ws)
    log(f"Sheet '{args.worksheet}': {len(in_sheet)} account(s) already there.")
    new_to_mirror = in_sheet - seen
    if new_to_mirror and not args.dry_run:
        # Keep the mirror in step so a later --dry-run or offline run still
        # knows about everything the sheet holds.
        leads.append_ledger(args.leads_dir, sorted(new_to_mirror))
    return seen | in_sheet, ws


def main(argv=None):
    args = parse_args(argv)
    started = time.time()
    stamp = leads.today_stamp()
    os.makedirs(args.output_dir, exist_ok=True)
    csv_mode = not args.sheet

    criteria = dict(
        max_followers=args.max_followers,
        min_followers=args.min_followers,
        min_posts=args.min_posts,
        window_days=args.window_days,
    )

    # --- who have we already delivered ------------------------------------
    try:
        seen, worksheet = load_seen(args, print)
    except sheets.SheetError as exc:
        print(f"\nGoogle Sheets: {exc}")
        return 2

    # CSV mode additionally tops today's file up to --daily-limit rather than
    # always hunting a fresh batch of --daily-limit — otherwise a resumed run
    # after a rate-limit block would overshoot the day's target.
    today_rows = leads.load_today(args.leads_dir, stamp) if csv_mode else []
    if csv_mode:
        print(f"CSV mode (no --sheet given) — writing to "
              f"{leads.day_file(args.leads_dir, stamp)}")
        needed = max(0, args.daily_limit - len(today_rows))
        print(f"Date {stamp} — {len(today_rows)} lead(s) already logged today, "
              f"need {needed} more.")
        if needed == 0:
            print(f"\nToday's quota of {args.daily_limit} is already met: "
                  f"{leads.day_file(args.leads_dir, stamp)}")
            return 0
    else:
        needed = args.daily_limit
        print(f"\nDate {stamp}")

    print(f"Criteria: {args.min_followers:,} <= followers < {args.max_followers:,}, "
          f">= {args.min_posts} posts in {args.window_days}d")

    try:
        session = build_session(
            login_username=args.login,
            session_file=args.session_file,
            state_path=os.path.join(args.output_dir, "ratelimit.json"),
        )
    except SessionError as exc:
        print(f"\nLogin session: {exc}")
        return 4

    if args.login:
        print(f"Using logged-in session for {args.login} — anonymous rate limit doesn't apply.")
    else:
        # Check the persisted cooldown before doing anything: probing an
        # active block just to confirm it's still active can extend it.
        remaining = session.state.seconds_remaining
        if remaining > 0:
            mins, secs = int(remaining // 60), int(remaining % 60)
            print(f"\nStill cooling down from an earlier rate-limit block — "
                  f"{mins}m {secs}s left.")
            print("Nothing was requested. Re-run after that, or use "
                  "--login <account> to bypass the anonymous limit entirely.")
            return 5

    # --- find --------------------------------------------------------------
    handles = [normalize_username(u) for u in args.usernames if normalize_username(u)]
    if args.input:
        handles += read_usernames(args.input)

    if handles:
        found, examined = evaluate_supplied(session, handles, seen, criteria, args, print)
        notes, stopped_early = [], False
        chosen = []
    else:
        if not args.keywords:
            sys.exit("Give me either -k <keywords file> or -u/-i <handles>.")
        pool = read_keywords(args.keywords)
        if csv_mode:
            # Don't hunt a keyword this file already delivered a lead for
            # today, unless the list is too short to avoid it.
            used_today = {row.get("keyword", "") for row in today_rows}
            available = [k for k in pool if k not in used_today]
            pool = available or pool
        rng = random.Random(args.seed)
        chosen = rng.sample(pool, min(needed, len(pool)))
        print(f"Picked {len(chosen)} niche(s) at random from {len(pool)} "
              f"(seed {args.seed if args.seed is not None else 'random'}): "
              f"one lead each.")
        found, notes, examined, stopped_early = hunt_one_per_keyword(
            session, chosen, seen, criteria, args, print
        )

    # --- deliver -----------------------------------------------------------
    csv_path = leads.day_file(args.leads_dir, stamp) if csv_mode else None

    if found and not args.dry_run:
        if worksheet is not None:
            try:
                sheets.append_leads(worksheet, found, stamp)
                print(f"\nAppended {len(found)} row(s) to the sheet.")
            except sheets.SheetError as exc:
                # Never lose leads to a network blip — park them on disk so a
                # later run can push them, and don't ledger them as delivered.
                spill = os.path.join(args.output_dir, f"unsent-{stamp}.json")
                with open(spill, "w", encoding="utf-8") as f:
                    json.dump(found, f, indent=2, ensure_ascii=False)
                print(f"\nSheet write FAILED: {exc}")
                print(f"Leads saved to {spill} — not marked as delivered.")
                return 3
        else:
            rows = today_rows + [leads.to_row(r, stamp) for r in found]
            csv_path = leads.write_day(args.leads_dir, stamp, rows)
            print(f"\nWrote {len(rows)} row(s) to {csv_path} "
                  f"({len(found)} new this run).")
        leads.append_ledger(args.leads_dir, [r["username"] for r in found])

    with open(os.path.join(args.output_dir, "examined.json"), "w", encoding="utf-8") as f:
        json.dump(examined, f, indent=2, ensure_ascii=False)

    delivered_today = len(today_rows) + (len(found) if not args.dry_run else 0)
    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "date": stamp,
        "mode": "csv" if csv_mode else "sheet",
        "criteria": criteria,
        "daily_limit": args.daily_limit,
        "keywords_picked": chosen,
        "per_keyword": notes,
        "leads_this_run": len(found),
        "delivered_today": delivered_today if csv_mode else None,
        "examined": len(examined),
        "dry_run": args.dry_run,
        "stopped_early": stopped_early,
        "elapsed_seconds": round(time.time() - started, 1),
    }
    with open(os.path.join(args.output_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # --- report ------------------------------------------------------------
    header_count = delivered_today if csv_mode else len(found)
    print(f"\n{'':-<78}")
    print(f"{header_count}/{args.daily_limit} leads for {stamp}"
          f"{'  (DRY RUN — nothing written)' if args.dry_run else ''}"
          f"   {len(examined)} accounts examined")
    print(f"{'':-<78}")
    for record in found:
        print(f"  {record.get('matched_keyword', ''):<30} "
              f"https://www.instagram.com/{record['username']}/")
        print(f"  {'':<30} {record['follower_count']:>9,} followers   "
              f"{record['posts_last_week']} posts/{args.window_days}d")

    missed = [n["keyword"] for n in notes if not n["lead"]]
    if missed:
        print(f"\n  No lead found for: {', '.join(missed)}")
        print("  Re-run to try different niches, or raise --per-keyword-budget.")

    if csv_mode:
        print(f"\nCSV: {csv_path}")
        if header_count < args.daily_limit and not args.dry_run:
            print(f"  {args.daily_limit - header_count} short of today's target — "
                  f"re-run the same command to top up.")
    elif not args.dry_run:
        print(f"\nSheet: {args.sheet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
