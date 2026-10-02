"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from ig_leads import auth, config, output
from ig_leads.budget import RequestBudget
from ig_leads.config import Filters, Pacing, Settings
from ig_leads.errors import (
    AccountAtRisk,
    BrowserMissing,
    BudgetExhausted,
    IGLeadsError,
    NotSignedIn,
)
from ig_leads.niches import available, load_niches
from ig_leads.scraper import IGLeadScraper
from ig_leads.store import LeadStore

LOGGER = logging.getLogger(__name__)

EPILOG = """
examples:
  ig-leads --login                          sign in once (opens a browser)
  ig-leads --dry-run                        list the niches, use no requests
  ig-leads --niches wildlife,restoration    search two niches
  ig-leads --max-tags 5 --max-accounts 25   a small, safe run
  ig-leads --recheck-known                  re-check leads already recorded

Every lead is appended to leads/all_leads.csv, which accumulates across runs
and holds the profile URL, bio, followers and posting cadence. Accounts already
in that file are skipped BEFORE any request is spent on them.

Finds accounts with 1k-500k followers that posted at least 5 times in the last
14 days, in your content niches, excluding video editors and agencies.

Pacing adapts to the run size: small runs (under ~30 requests) use 5-12s
gaps, larger sweeps use 18-42s. Sustained volume is what Instagram acts on, so
a big run stays slow - use --slow to force that, or --min-gap to set your own.

    --max-tags 2 --max-accounts 5     ~9 requests, ~1 min
    default (8 tags, 40 accounts)     ~68 requests, ~34 min
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ig-leads",
        description="Find Instagram content creators to pitch video editing to.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    what = p.add_argument_group("what to look for")
    what.add_argument("--niche-file", default="creator_niches", metavar="NAME|PATH",
                      help=f"niche list (built in: {', '.join(available())})")
    what.add_argument("--niches", metavar="A,B,C",
                      help="only these niches, matched loosely "
                           "(e.g. 'wildlife,restoration,homestead')")
    what.add_argument("--min-followers", type=int, default=1_000, metavar="N",
                      help="default: 1000")
    what.add_argument("--max-followers", type=int, default=500_000, metavar="N",
                      help="default: 500000")
    what.add_argument("--min-posts", type=int, default=5, metavar="N",
                      help="posts required inside the window (default: 5)")
    what.add_argument("--days", type=int, default=14, metavar="N",
                      help="the window, in days (default: 14)")
    what.add_argument("--include-private", action="store_true",
                      help="do not skip private accounts (they cannot be assessed)")
    what.add_argument("--include-rejected", action="store_true",
                      help="also output accounts that were filtered out, and why")

    limits = p.add_argument_group("how much to do (this protects the account)")
    limits.add_argument("--max-tags", type=int, default=8, metavar="N",
                        help="hashtags to search this run (default: 8, 1 request each)")
    limits.add_argument("--max-accounts", type=int, default=40, metavar="N",
                        help="accounts to check this run (default: 40, 1-2 requests each)")
    limits.add_argument("--daily-requests", type=int, default=400, metavar="N",
                        help="hard daily ceiling, shared across runs (default: 400)")
    limits.add_argument("--run-requests", type=int, default=200, metavar="N",
                        help="hard ceiling for this run (default: 200)")
    limits.add_argument("--min-gap", type=float, metavar="SEC",
                        help="minimum pause between requests. Default adapts: "
                             "5-12s for runs under ~30 requests, 18-42s above.")
    limits.add_argument("--max-gap", type=float, metavar="SEC",
                        help="maximum pause between requests (see --min-gap)")
    limits.add_argument("--slow", action="store_true",
                        help="force 18-42s gaps even on a small run")

    out = p.add_argument_group("output")
    out.add_argument("-f", "--format", choices=("digest", "csv", "json"),
                     default="digest", help="default: digest")
    out.add_argument("-o", "--out", metavar="PATH",
                     help="write to this exact file ('auto' names it by date). "
                          "Replaces the automatic CSV.")
    out.add_argument("--leads-dir", default="leads", metavar="DIR",
                     help="where all_leads.csv lives (default: leads/)")
    out.add_argument("--no-save", action="store_true",
                     help="do not append to all_leads.csv")
    out.add_argument("--recheck-known", action="store_true",
                     help="also check accounts already recorded as leads "
                          "(costs budget; they are skipped by default)")

    session = p.add_argument_group("session")
    session.add_argument("--login", action="store_true",
                         help="sign in to Instagram and exit")
    session.add_argument("--logout", action="store_true",
                         help="delete the saved session and exit")
    session.add_argument("--session-status", action="store_true",
                         help="check whether the saved session still works")
    session.add_argument("--no-login", action="store_true",
                         help="never open a login window; fail instead")
    session.add_argument("--login-timeout", type=int, default=420, metavar="SEC",
                         help="how long to wait for the sign-in (default: 420)")

    misc = p.add_argument_group("other")
    misc.add_argument("--dry-run", action="store_true",
                      help="show the plan and make no requests at all")
    misc.add_argument("--budget-status", action="store_true",
                      help="show today's request usage and exit")
    misc.add_argument("--headful", action="store_true",
                      help="show the browser (debugging)")
    misc.add_argument("-v", "--verbose", action="store_true")
    misc.add_argument("-q", "--quiet", action="store_true")
    return p


def init_logging(verbose: bool, quiet: bool):
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                           datefmt="%H:%M:%S"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_settings(args) -> Settings:
    explicit = args.min_gap is not None or args.max_gap is not None or args.slow
    pacing = Pacing(
        min_gap_s=args.min_gap if args.min_gap is not None else 18.0,
        max_gap_s=args.max_gap if args.max_gap is not None else 42.0,
        daily_requests=args.daily_requests,
        run_requests=args.run_requests,
        adaptive=not explicit,
    )
    filters = Filters(
        min_followers=args.min_followers,
        max_followers=args.max_followers,
        min_recent_posts=args.min_posts,
        recent_days=args.days,
        skip_private=not args.include_private,
    )
    return Settings(pacing=pacing, filters=filters, headless=not args.headful)


def selected_niches(args):
    niche_set = load_niches(args.niche_file)
    wanted = [w.strip() for w in args.niches.split(",")] if args.niches else None
    picked = niche_set.pick(wanted)
    if wanted and not picked:
        raise IGLeadsError(
            f"No niche matched {args.niches!r}. Try --dry-run to list them."
        )
    return niche_set, picked


def run_dry(args) -> int:
    niche_set, picked = selected_niches(args)
    plan = [(n, n.hashtags[0]) for n in picked if n.hashtags][: args.max_tags]

    print(f"niche file   {niche_set.name} - {niche_set.description}")
    print(f"niches       {len(niche_set.niches)} available, {len(picked)} selected")
    print(f"this run     {len(plan)} hashtags, up to {args.max_accounts} accounts")
    print(f"filters      {args.min_followers:,}-{args.max_followers:,} followers, "
          f">={args.min_posts} posts in {args.days}d")
    est = len(plan) + int(args.max_accounts * 1.5)
    pacing = build_settings(args).pacing.for_run(est)
    gap = (pacing.min_gap_s + pacing.max_gap_s) / 2
    print(f"cost         ~{est} requests at {pacing.min_gap_s:.0f}-"
          f"{pacing.max_gap_s:.0f}s gaps, roughly {est * gap / 60:.0f} min")
    print()
    print("hashtags this run:")
    for niche, tag in plan:
        print(f"   #{tag:<30} {niche.name}")
    if len(picked) > len(plan):
        print(f"   ... and {len(picked) - len(plan)} more niches "
              f"(raise --max-tags, or use --niches)")
    return 0


def run_budget_status(args) -> int:
    b = RequestBudget(args.daily_requests, args.run_requests)
    print(f"Budget file  {b.path}")
    print(f"Spent today  {b.spent_today}/{b.daily_limit}")
    print(f"Remaining    {b.remaining_today}  (resets at UTC midnight)")
    return 0


async def run_login(args) -> int:
    try:
        await auth.interactive_login(timeout_s=args.login_timeout)
    except IGLeadsError as e:
        print(f"\nSign-in failed: {e}", file=sys.stderr)
        return 1
    return 0


def run_logout(args) -> int:
    store = auth.SessionStore()
    if store.forget():
        print(f"Signed out — removed {store.path}")
    else:
        print("Nothing to remove.")
    return 0


async def run_session_status(args) -> int:
    store = auth.SessionStore()
    state = store.load()
    print(f"Session file  {store.path}")
    if state is None:
        print("Status        no usable session")
        print("\nRun `python -m ig_leads --login`.")
        return 1
    age = store.age_days()
    print(f"Cookies       {len(state.get('cookies', []))}")
    print(f"Age           {age:.1f} days" if age is not None else "Age           ?")
    print("Checking against Instagram...")
    live = await auth.verify(state, headless=not args.headful)
    print(f"Status        {'live' if live else 'expired or rejected'}")
    return 0 if live else 1


async def run_scrape(args) -> int:
    niche_set, picked = selected_niches(args)
    settings = build_settings(args)

    state = await auth.ensure_session(
        allow_login=not args.no_login, login_timeout_s=args.login_timeout
    )

    store = LeadStore(args.leads_dir)
    if len(store):
        LOGGER.info("%d leads already recorded in %s", len(store), store.path)

    scraper = IGLeadScraper(
        settings,
        known_leads=set() if args.recheck_known else set(store.known),
    )
    LOGGER.info("Budget: %d requests left today", scraper.budget.remaining_today)

    result = await scraper.run(
        state, picked, max_hashtags=args.max_tags, max_accounts=args.max_accounts
    )

    accounts = result.leads + (result.rejected if args.include_rejected else [])
    text = output.render(accounts, args.format)

    if args.out:
        path = Path(output.default_filename(args.format)
                    if args.out == "auto" else args.out)
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {len(accounts)} rows to {path}", file=sys.stderr)
    else:
        print(text)

    # Leads always go into the running record, whatever else was printed. A run
    # costs real budget and real minutes; losing it to a closed terminal would
    # mean spending the day's allowance again to get it back.
    if not args.no_save:
        added = store.append(result.leads)
        if added:
            print(f"\nAppended {added} new leads to {store.path} "
                  f"({len(store)} total)", file=sys.stderr)
        elif result.leads:
            print(f"\nNo new leads to append — all {len(result.leads)} were "
                  f"already recorded", file=sys.stderr)

    s = result.stats
    print(f"\n{s.summary()}", file=sys.stderr)
    print(f"{len(result.leads)} leads, {len(result.rejected)} filtered out",
          file=sys.stderr)
    if s.skipped_known_leads:
        print(f"{s.skipped_known_leads} known leads skipped before spending a "
              f"request (--recheck-known to include them)", file=sys.stderr)
    print(f"Requests: {result.budget_summary}", file=sys.stderr)
    if s.stopped_early:
        print(f"\n!! Run ended early: {s.stopped_early}", file=sys.stderr)
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    args = build_parser().parse_args(argv)
    init_logging(args.verbose, args.quiet)

    try:
        if args.logout:
            return run_logout(args)
        if args.budget_status:
            return run_budget_status(args)
        if args.dry_run:
            return run_dry(args)
        if args.login:
            return asyncio.run(run_login(args))
        if args.session_status:
            return asyncio.run(run_session_status(args))
        return asyncio.run(run_scrape(args))

    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except BrowserMissing as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    except NotSignedIn as e:
        print(f"\nNot signed in: {e}", file=sys.stderr)
        return 3
    except AccountAtRisk as e:
        print(f"\n!! STOPPED TO PROTECT THE ACCOUNT: {e}", file=sys.stderr)
        return 4
    except BudgetExhausted as e:
        print(f"\n{e}", file=sys.stderr)
        return 5
    except (IGLeadsError, FileNotFoundError, ValueError) as e:
        print(f"\n{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
