"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from x_leads import __version__
from x_leads import output
from x_leads.auth.session import (
    SESSION_PATH,
    SessionManager,
    ensure_session,
    interactive_login,
)
from x_leads.errors import BrowserMissing, NotSignedIn, SearchBlocked, XLeadsError
from x_leads.leads.classifier import Thresholds
from x_leads.leads.location import DEFAULT_EXCLUDED_COUNTRIES, LocationFilter
from x_leads.log_config import force_utf8_streams, init_logger
from x_leads.niches import DEFAULT_NICHE, available, load_niche
from x_leads.scraper import ScrapeConfig, XLeadScraper
from x_leads.state import SeenStore

LOGGER = logging.getLogger(__name__)

EPILOG = """
examples:
  x-leads                             last 48h, new leads only, to the terminal
  x-leads --include-seen              also re-show ones already reported
  x-leads --min-verdict hot           only the posts with a budget attached
  x-leads -f csv -o leads.csv         a spreadsheet
  x-leads --include-rejected          everything, with the reason each was cut
  x-leads --login                     sign in (or re-sign-in) and exit
  x-leads --dry-run                   print the queries without running them

country filtering:
  x-leads                             reports where authors are, drops nothing
  x-leads --country-filter drop       actually cut India/Pakistan/Bangladesh/
                                      Philippines
  x-leads --country-filter drop --exclude-country egypt,nepal
  x-leads --country-filter off        skip the location pass entirely

Start with the default 'report' mode and read the coverage line on stderr. An X
profile location is optional free text, so the filter can only judge the
authors who filled it in; that percentage decides whether dropping is worth it.

The first run opens a browser window to sign in to X. After that the saved
session is reused and every run is headless.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="x-leads",
        description="Find people on X who need video editing help.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    what = p.add_argument_group("what to look for")
    what.add_argument(
        "--niche", default=DEFAULT_NICHE, metavar="NAME|PATH",
        help=f"phrase preset to search (default: {DEFAULT_NICHE}; "
             f"built in: {', '.join(available())}). Also takes a path to a JSON file.",
    )
    what.add_argument(
        "--hours", type=float, default=48.0, metavar="H",
        help="how far back to look (default: 48). The single biggest lever on "
             "runtime — the search stops as soon as it reaches this age.",
    )
    what.add_argument(
        "--min-verdict", choices=("cold", "warm", "hot"), default="cold",
        help="lowest confidence to report (default: cold)",
    )
    what.add_argument(
        "--min-followers", type=int, default=0, metavar="N",
        help="drop authors below this follower count (default: 0 — off, because "
             "a new channel with 200 followers is a real buyer)",
    )
    what.add_argument(
        "--include-rejected", action="store_true",
        help="also show filtered-out posts and why each was cut — the way to "
             "check the classifier is not eating real leads",
    )

    where = p.add_argument_group("where the author is")
    where.add_argument(
        "--country-filter", choices=LocationFilter.MODES, default="report",
        metavar="MODE",
        help="off | report | drop (default: report). 'report' records each "
             "author's country and prints what it *would* have dropped, "
             "without dropping it — run that first, because an X profile "
             "location is optional and often blank, and the coverage number "
             "is what tells you whether dropping is safe.",
    )
    where.add_argument(
        "--exclude-country", action="append", default=[], metavar="NAME",
        help=f"add a country to the block list (repeatable, or comma-separated). "
             f"Default list: {', '.join(c.title() for c in DEFAULT_EXCLUDED_COUNTRIES)}",
    )
    where.add_argument(
        "--allow-country", action="append", default=[], metavar="NAME",
        help="remove a country from the block list (repeatable)",
    )
    where.add_argument(
        "--drop-unknown-location", action="store_true",
        help="with --country-filter drop, also cut authors whose location "
             "cannot be read at all. Off by default: a blank location is the "
             "most common value on X and is not evidence of anything, so this "
             "discards a lot of real leads.",
    )

    limits = p.add_argument_group("how hard to work")
    limits.add_argument(
        "--max-queries", type=int, metavar="N",
        help="cap the number of searches (phrases are packed ~11 per query)",
    )
    limits.add_argument(
        "--max-scrolls", type=int, default=12, metavar="N",
        help="scroll depth per search (default: 12). Rarely reached — the age "
             "cutoff usually stops it first.",
    )
    limits.add_argument(
        "--max-tweets", type=int, default=400, metavar="N",
        help="per-query tweet cap (default: 400)",
    )
    limits.add_argument(
        "--concurrency", type=int, default=2, metavar="N",
        help="searches in flight at once (default: 2). Raising this is the "
             "quickest way to get rate limited.",
    )

    out = p.add_argument_group("output")
    out.add_argument(
        "-f", "--format", choices=("digest", "json", "jsonl", "csv"),
        default="digest", help="output format (default: digest)",
    )
    out.add_argument(
        "-o", "--out", metavar="PATH",
        help="write to a file instead of stdout ('auto' names it by date)",
    )
    out.add_argument(
        "--include-seen", action="store_true",
        help="also show leads already reported by an earlier run. Off by "
             "default: a repeat run answers 'what's new?', not 'show me the "
             "same twenty posts again'.",
    )
    out.add_argument(
        "--forget-seen", action="store_true",
        help="clear the seen-leads store, so everything counts as new again",
    )
    # Was the opt-in flag before this became the default. Kept so existing
    # scripts and scheduled tasks do not break on an unknown argument.
    out.add_argument("--only-new", action="store_true", help=argparse.SUPPRESS)

    session = p.add_argument_group("session")
    session.add_argument(
        "--login", action="store_true",
        help="open a browser window to sign in to X, then exit",
    )
    session.add_argument(
        "--logout", action="store_true",
        help="delete the saved session and login profile, then exit",
    )
    session.add_argument(
        "--session-status", action="store_true",
        help="report whether the saved session still works, then exit",
    )
    session.add_argument(
        "--no-login", action="store_true",
        help="never open a login window; fail instead. Use on a schedule, "
             "where there is nobody to sign in.",
    )
    session.add_argument(
        "--login-timeout", type=int, default=300, metavar="SECONDS",
        help="how long to wait for the sign-in (default: 300)",
    )
    session.add_argument(
        "--skip-session-check", action="store_true",
        help="trust the saved session without verifying it (saves ~4s)",
    )

    misc = p.add_argument_group("other")
    misc.add_argument(
        "--dry-run", action="store_true",
        help="print the queries that would be run, and stop",
    )
    misc.add_argument(
        "--headful", action="store_true",
        help="show the scraping browser (for debugging)",
    )
    misc.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    misc.add_argument("-q", "--quiet", action="store_true", help="warnings only")
    misc.add_argument("--version", action="version", version=f"x-leads {__version__}")
    return p


def _country_args(values: list[str]) -> list[str]:
    """Flatten repeated flags and comma-separated values into one list.

    So `--exclude-country egypt --exclude-country nepal,kenya` and
    `--exclude-country "egypt, nepal, kenya"` mean the same thing.
    """
    out: list[str] = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def build_location_filter(args) -> LocationFilter:
    return LocationFilter.build(
        add=_country_args(args.exclude_country),
        remove=_country_args(args.allow_country),
        mode=args.country_filter,
        drop_unknown=args.drop_unknown_location,
    )


# ---------------------------------------------------------------- subcommands
def run_dry(args) -> int:
    niche = load_niche(args.niche)
    cfg = ScrapeConfig(niche=args.niche, hours=args.hours, max_queries=args.max_queries)
    queries = XLeadScraper(cfg).build_queries(niche)

    print(f"niche      {niche.name} — {niche.description}")
    print(f"phrases    {len(niche.phrases)}")
    print(f"queries    {len(queries)}  (X caps a query at ~512 characters)")
    print(f"window     last {args.hours:g} hours")
    print()
    for i, q in enumerate(queries, 1):
        print(f"[{i}] ({len(q)} chars)")
        print(f"    {q}")
        print()
    return 0


async def run_login(args) -> int:
    manager = SessionManager()
    try:
        saved = await interactive_login(manager, timeout_s=args.login_timeout)
    except XLeadsError as e:
        print(f"Sign-in failed: {e}", file=sys.stderr)
        return 1
    print(f"Session saved to {manager.session_path} ({saved} cookies).")
    return 0


def run_logout(args) -> int:
    manager = SessionManager()
    if manager.forget():
        print(f"Signed out — removed {manager.session_path} and the login profile.")
    else:
        print("Nothing to remove; there was no saved session.")
    return 0


async def run_session_status(args) -> int:
    from x_leads.auth.session import _state_works

    manager = SessionManager()
    state = manager.load()
    print(f"Session file   {manager.session_path}")
    if state is None:
        print("Status         no usable session saved")
        print("\nRun `x-leads --login` to sign in.")
        return 1

    age = manager.age_days()
    print(f"Cookies        {len(state.get('cookies', []))}")
    print(f"Age            {age:.1f} days" if age is not None else "Age            ?")
    print("Checking it against X...")
    works = await _state_works(state)
    print(f"Status         {'live' if works else 'expired or rejected'}")
    if not works:
        print("\nRun `x-leads --login` to sign in again.")
    return 0 if works else 1


async def run_scrape(args) -> int:
    seen = SeenStore()
    if args.forget_seen:
        seen.clear()
        print("Cleared the seen-leads store.", file=sys.stderr)

    cfg = ScrapeConfig(
        niche=args.niche,
        hours=args.hours,
        max_queries=args.max_queries,
        max_scrolls=args.max_scrolls,
        max_tweets_per_query=args.max_tweets,
        concurrency=args.concurrency,
        headless=not args.headful,
        min_verdict=args.min_verdict,
        min_followers=args.min_followers,
        include_rejected=args.include_rejected,
        skip_seen=not args.include_seen,
        allow_login=not args.no_login,
        login_timeout_s=args.login_timeout,
        verify_session=not args.skip_session_check,
        thresholds=Thresholds(),
        location_filter=build_location_filter(args),
    )

    result = await XLeadScraper(cfg, seen=seen).run()
    seen.save()

    text = output.render(result.leads, args.format)
    if args.out:
        path = Path(
            output.default_filename(args.format, cfg.niche)
            if args.out == "auto" else args.out
        )
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {len(result.leads)} leads to {path}", file=sys.stderr)
    else:
        print(text)

    _report(result, cfg)
    return 0


def _report(result, cfg) -> None:
    """The one-line summary, on stderr so it never pollutes piped data."""
    s = result.stats
    counts = result.counts
    bits = [f"{v}: {counts[v]}" for v in ("hot", "warm", "cold") if v in counts]

    print(
        f"\n{s.summary()} -> {result.tweets_collected} collected, "
        f"{len(result.leads)} reported"
        + (f" ({', '.join(bits)})" if bits else ""),
        file=sys.stderr,
    )
    if result.classified_out:
        print(
            f"{result.classified_out} filtered out as sellers, noise or off-topic "
            f"(--include-rejected to see them)",
            file=sys.stderr,
        )
    if result.suppressed_as_seen:
        print(
            f"{result.suppressed_as_seen} already reported by an earlier run "
            f"(--include-seen to show them again)",
            file=sys.stderr,
        )
    for line in cfg.location_filter.report(result.location_audit):
        print(line, file=sys.stderr)
    if s.errors:
        print(f"{len(s.errors)} errors: {'; '.join(s.errors[:3])}", file=sys.stderr)


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    force_utf8_streams()
    args = build_parser().parse_args(argv)
    init_logger(verbose=args.verbose, quiet=args.quiet)

    try:
        if args.logout:
            return run_logout(args)
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
    except SearchBlocked as e:
        print(f"\nBlocked: {e}", file=sys.stderr)
        return 4
    except FileNotFoundError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2
    except XLeadsError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
