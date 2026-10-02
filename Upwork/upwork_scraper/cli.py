"""Command line interface: scrape Upwork jobs to stdout or a file.

    python -m upwork_scraper --pages 2 --format json --out jobs.json
    python -m upwork_scraper --query "python scraper" --format csv --out jobs.csv
    python -m upwork_scraper --watch --interval 120 --out feed.jsonl

Logs go to stderr, data goes to stdout, so piping into `jq` works.
"""

import argparse
import csv
import io
import json
import logging
import signal
import sys
import threading

from upwork_scraper import config
from upwork_scraper.country_filter import (
    DEFAULT_EXCLUDED_COUNTRIES,
    CountryFilter,
    parse_country_list,
)
from upwork_scraper.enrich import (
    BROWSER_REQUIRED_HELP,
    BrowserFetcher,
    ClientEnricher,
    HumanDelay,
)
from upwork_scraper.enrich.browser_fetcher import (
    DEFAULT_PROFILE_DIR,
    LOGIN_PROFILE_DIR,
    profile_for,
)
from upwork_scraper.enrich.client_fetcher import parse_client_info
from upwork_scraper.log_config import init_logger
from upwork_scraper.models.job_models import Job
from upwork_scraper.niches import (
    NICHE_DIR,
    available_niches,
    load_niche,
    read_keywords_file,
)
from upwork_scraper.proxies.proxy_manager import (
    NoProxyManager,
    build_proxy_manager,
    check_proxies,
)
from upwork_scraper.scraper import UpworkScraper

LOGGER = logging.getLogger(__name__)

CSV_FIELDS = [
    "cipher", "title", "description", "link", "skills",
    "published_date", "job_type", "is_hourly",
    "hourly_low", "hourly_high", "budget",
    "duration_weeks", "contractor_tier", "matched_query",
]


# Client fields worth a spreadsheet column, flattened as client_*.
#
# Ordered the way you read a client card: who they are, whether they can be
# trusted, what they have actually spent, then what this specific job is doing.
#
# An earlier version listed 14 of the 29 fields and silently dropped the rest,
# including `hire_rate`, `rating`, `total_reviews` and `payment_verified` —
# the ones enrichment (and signing in) exists to obtain. Anyone exporting CSV
# instead of JSON lost exactly the columns they would qualify a client on, with
# nothing to indicate it. Keep this in step with `ClientInfo`; the test asserts
# every field is accounted for.
CLIENT_CSV_FIELDS = [
    # who and where
    "country", "city", "timezone", "local_time", "member_since",
    "industry", "company_size",
    # can they be trusted
    "payment_verified", "is_top_client", "rating", "total_reviews",
    # do they actually hire
    "hire_rate", "total_posted_jobs", "open_jobs", "total_hires",
    "total_jobs_with_hires", "active_hires", "has_ever_hired",
    "hires_per_job", "active_hire_share",
    # what they pay
    "total_spent", "avg_spend_per_hire", "avg_hourly_rate_paid", "total_hours",
    # this job's activity
    "proposals", "interviewing", "invites_sent", "unanswered_invites",
    "last_viewed", "job_location",
    # did the lookup work
    "fetch_status", "fetch_error",
]


def _to_row(job: Job, with_client: bool) -> dict:
    data = job.model_dump(mode="json")
    row = {field: data.get(field) for field in CSV_FIELDS}
    skills = row.get("skills")
    row["skills"] = "|".join(skills) if skills else ""

    if with_client:
        client = data.get("client") or {}
        for field in CLIENT_CSV_FIELDS:
            row[f"client_{field}"] = client.get(field)
    return row


def csv_fields(jobs: list[Job]) -> list[str]:
    """Job columns, plus client_* columns once anything has been enriched."""
    if any(j.client is not None for j in jobs):
        return CSV_FIELDS + [f"client_{f}" for f in CLIENT_CSV_FIELDS]
    return CSV_FIELDS


def render(jobs: list[Job], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(
            [j.model_dump(mode="json") for j in jobs], indent=2, ensure_ascii=False
        )

    if fmt == "jsonl":
        return "\n".join(
            json.dumps(j.model_dump(mode="json"), ensure_ascii=False) for j in jobs
        )

    if fmt == "csv":
        fields = csv_fields(jobs)
        with_client = len(fields) > len(CSV_FIELDS)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(_to_row(j, with_client) for j in jobs)
        return buf.getvalue().rstrip("\n")

    raise ValueError(f"Unknown format: {fmt}")


def use_utf8_streams():
    """Force UTF-8 on stdout/stderr.

    Job titles routinely contain arrows, em-dashes, emoji and non-Latin script.
    On Windows a redirected stream defaults to the ANSI code page (cp1252),
    which raises UnicodeEncodeError mid-write — so `... > jobs.jsonl` would die
    partway through. Console output is unaffected either way.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass  # already wrapped, or not a real stream (e.g. pytest capture)


def _emit(text: str, out: str | None, append: bool = False):
    if not out:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        return

    mode = "a" if append else "w"
    with open(out, mode, encoding="utf-8", newline="") as fh:
        fh.write(text + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upwork-scraper",
        description="Scrape public Upwork job listings (no database required).",
    )
    parser.add_argument(
        "--pages", type=int, default=config.MAX_PAGES,
        help=f"pages to fetch, 50 jobs each (default: {config.MAX_PAGES})",
    )
    parser.add_argument(
        "--query", action="append", default=None,
        help="keyword filter. Repeat or comma-separate for several: "
             "--query react --query 'web scraping'. Adds to --niche keywords.",
    )
    parser.add_argument(
        "--niche", default=None, metavar="NAME_OR_PATH",
        help="use a niche preset's keywords and relevance filter, e.g. 'video'. "
             "Also accepts a path to your own JSON file.",
    )
    parser.add_argument(
        "--keywords-file", default=None, metavar="PATH",
        help="extra keywords, one per line (# comments allowed)",
    )
    parser.add_argument(
        "--list-niches", action="store_true", help="show built-in niches and exit"
    )
    parser.add_argument(
        "--check-proxies", action="store_true",
        help="test each configured proxy against Upwork and exit",
    )
    parser.add_argument(
        "--login", action="store_true",
        help="open the scraper's browser so you can sign in to Upwork yourself. "
             "The session is kept in its profile and unlocks client hire rate.",
    )
    parser.add_argument(
        "--login-status", action="store_true",
        help="report whether the signed-in profile is still signed in, and exit",
    )
    parser.add_argument(
        "--reset-login", action="store_true",
        help="delete the signed-in browser profile and start clean. Use when a "
             "profile keeps getting rate limited; you will need --login again.",
    )
    parser.add_argument(
        "--login-timeout", type=int, default=300, metavar="SECONDS",
        help="how long --login waits for you to finish signing in (default: 300)",
    )
    parser.add_argument(
        "--no-auto-login", action="store_true",
        help="with --logged-in, never ask and never open a sign-in window when "
             "the session is dead; fall back to anonymous instead. Implied by "
             "--watch.",
    )
    parser.add_argument(
        "--logged-in", action="store_true",
        help="enrich using the signed-in browser profile, which adds client "
             "hire rate and jobs-posted. Off by default: runs are anonymous "
             "unless you ask for this. Set up once with --login. If the session "
             "is dead you are asked whether to sign in, reset and sign in, or "
             "run anonymously — it never downgrades silently.",
    )
    parser.add_argument(
        "--no-filter", action="store_true",
        help="keep every search result instead of applying the niche's relevance filter",
    )
    parser.add_argument(
        "--exclude-country", action="append", default=None, metavar="COUNTRY",
        help="drop jobs posted from this country. Repeat or comma-separate. "
             "Adds to the default list ("
             + ", ".join(c.title() for c in DEFAULT_EXCLUDED_COUNTRIES)
             + "). Needs --enrich-clients, which is where country comes from.",
    )
    parser.add_argument(
        "--allow-country", action="append", default=None, metavar="COUNTRY",
        help="keep jobs from this country even though it is excluded by "
             "default, e.g. --allow-country India",
    )
    parser.add_argument(
        "--no-country-filter", action="store_true",
        help="keep jobs from every country, including the default exclusions",
    )
    parser.add_argument(
        "--drop-unknown-country", action="store_true",
        help="also drop jobs whose country could not be determined (never "
             "enriched, or the lookup failed). By default those are kept.",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="fetch already-posted jobs first (deep pagination). Combine with "
             "--watch to backfill and then stream new ones.",
    )
    parser.add_argument(
        "--backfill-pages", type=int, default=20, metavar="N",
        help="pages per keyword when backfilling, 50 jobs each (default: 20). "
             "Upwork caps pagination at ~101 pages.",
    )
    parser.add_argument(
        "--max-age", type=float, default=None, metavar="MINUTES",
        help="only keep jobs published within the last N minutes",
    )
    parser.add_argument(
        "--max-age-days", type=float, default=None, metavar="DAYS",
        help="only keep jobs published within the last N days (e.g. 2)",
    )
    parser.add_argument(
        "--enrich-clients", action="store_true",
        help="after jobs are fetched, run the separate stage that looks up each "
             "job poster (country, city, member since) and the job's proposal "
             "counts. Needs a browser fetcher — see GUIDE.md.",
    )
    parser.add_argument(
        "--separate-clients", action="store_true",
        help="also write client details to their own file (they are merged into "
             "each job under `client` either way)",
    )
    parser.add_argument(
        "--clients-out", default=None, metavar="PATH",
        help="path for the separate client file (implies --separate-clients "
             "behaviour when that flag is set)",
    )
    parser.add_argument(
        "--enrich-delay", nargs=2, type=float, default=None,
        metavar=("MIN", "MAX"),
        help="seconds between client requests, randomised in this range "
             "(default: 4 11). Raise it if you get blocked.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="keep only the newest N jobs. With --enrich-clients every one of "
             "them gets client details, so there are no null clients.",
    )
    parser.add_argument(
        "--enrich-limit", type=int, default=None, metavar="N",
        help="enrich only the newest N of the jobs kept (each is a page "
             "request, ~8s). Omit to enrich all of them.",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="enrich over plain HTTP instead of a browser (Cloudflare will block it)",
    )
    parser.add_argument(
        "--headful", action="store_true",
        help="show the browser window on screen (default: parked off-screen)",
    )
    parser.add_argument(
        "--browser-headless", action="store_true",
        help="run the browser headless. Cloudflare detects this — expect blocks.",
    )
    parser.add_argument(
        "--skip-backlog", action="store_true",
        help="watch mode: ignore jobs that already existed at startup",
    )
    parser.add_argument(
        "--sort", default="recency", help="Upwork sort order (default: recency)"
    )
    parser.add_argument(
        "--format", dest="fmt", choices=["json", "jsonl", "csv"], default="json",
        help="output format (default: json)",
    )
    parser.add_argument("--out", default=None, help="write to this file instead of stdout")
    parser.add_argument(
        "--watch", action="store_true", help="keep scraping on an interval"
    )
    parser.add_argument(
        "--interval", type=int, default=config.SCRAPE_INTERVAL,
        help=f"seconds between cycles when watching (default: {config.SCRAPE_INTERVAL})",
    )
    parser.add_argument(
        "--webshare-url", default=None,
        help="Webshare proxy list URL (falls back to WEBSHARE_URL env var)",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", help="ignore WEBSHARE_URL and connect directly"
    )
    parser.add_argument(
        "--pin-proxy", action="store_true",
        help="send every page through the same proxy that fetched the token",
    )
    parser.add_argument(
        "--workers", type=int, default=None, help="concurrent page fetchers"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def parse_queries(raw: list[str] | None) -> list[str] | None:
    """`--query a --query "b,c"` -> ["a", "b", "c"]."""
    if not raw:
        return None
    keywords = [k.strip() for value in raw for k in value.split(",") if k.strip()]
    return keywords or None


def resolve_keywords(args) -> tuple[list[str] | None, object | None]:
    """Combine niche preset, --keywords-file and --query into one keyword list.

    Returns the keywords and the niche (whose relevance filter the caller
    applies), or (None, None) for an unfiltered site-wide scrape.
    """
    extra = parse_queries(args.query) or []
    if args.keywords_file:
        extra = read_keywords_file(args.keywords_file) + extra

    if not args.niche:
        return (extra or None), None

    niche = load_niche(args.niche).with_extra_keywords(extra)
    LOGGER.info(
        "Niche '%s': %d keywords%s",
        niche.name,
        len(niche.keywords),
        f" (+{len(extra)} of your own)" if extra else "",
    )
    return niche.keywords, niche


def report_proxy_check(args) -> int:
    """Print how each proxy fares against Upwork. Exit code 1 if none work."""
    manager = (
        NoProxyManager() if args.no_proxy else build_proxy_manager(args.webshare_url)
    )
    if isinstance(manager, NoProxyManager):
        print("No proxies configured — set WEBSHARE_API_KEY in .env")
        return 1

    results = check_proxies(manager)
    print(f"\nTesting {len(results)} proxies against upwork.com\n")
    for proxy, ok, detail in results:
        print(f"  {'OK     ' if ok else 'BLOCKED'} {proxy.label:<40} {detail}")

    working = sum(1 for _, ok, _ in results if ok)
    print(f"\n{working}/{len(results)} usable with Upwork")

    if not working:
        print(
            "\nNone of these work against Upwork. Datacenter proxy IPs are\n"
            "blocked at the edge — reaching an IP-echo service proves the proxy\n"
            "is alive, not that Upwork will accept it.\n\n"
            "Options:\n"
            "  - Run without proxies. They are optional, and direct works.\n"
            "  - Buy residential proxies; Webshare sells them separately."
        )
    return 0 if working else 1


LOGIN_URL = "https://www.upwork.com/ab/account-security/login"

LOGIN_NOTES = """
A few things worth knowing before you do this:

  - Signing in is what unlocks hire rate. Upwork sends `postedCount` (the
    denominator) only to signed-in sessions.
  - The session lives in this scraper's own Chrome profile, not in a file and
    not in your normal browser. Nothing is copied anywhere else.
  - Automated access using your signed-in session is against Upwork's terms.
    The risk moves from your IP to your account, and no amount of pacing
    removes that. Enrichment stays slow for a reason.
  - To undo it, sign out in the window this opens, or delete the profile at
    %s
""".strip()


def _sample_job_url() -> str | None:
    """A live job URL to test what a signed-in session actually returns."""
    try:
        jobs = UpworkScraper().scrape(max_pages=1, query="video editing")
        for job in jobs:
            if job.link:
                return job.link
    except Exception:
        return None
    return None


def _report_gated_fields(html: str, cipher: str) -> bool:
    """Show whether the signed-in-only client fields came through."""
    info = parse_client_info(html, cipher)
    print()
    print("  hire rate         ", info.hire_rate)
    print("  jobs posted       ", info.total_posted_jobs)
    print("  open jobs         ", info.open_jobs)
    print("  jobs with hires   ", info.total_jobs_with_hires)
    print("  total hires       ", info.total_hires)
    return info.total_posted_jobs is not None or info.hire_rate is not None


def reset_login_profile() -> int:
    """Delete the signed-in profile so the next login starts from scratch.

    A profile that keeps getting rate limited carries whatever Upwork has
    associated with it — cookies, storage, fingerprint state. Starting clean
    is usually quicker than guessing which part is the problem.
    """
    import shutil

    if not LOGIN_PROFILE_DIR.exists():
        print(f"Nothing to reset — {LOGIN_PROFILE_DIR} does not exist.")
        return 0

    try:
        shutil.rmtree(LOGIN_PROFILE_DIR)
    except OSError as e:
        print(f"Could not delete the profile: {e}")
        print("Close any browser window still using it, then try again.")
        return 1

    print(f"Deleted {LOGIN_PROFILE_DIR}")
    print("Run --login to sign in again. Anonymous runs are unaffected.")
    return 0


def run_login(args) -> int:
    """Open the browser for a manual sign-in, then verify what it unlocked."""
    print(LOGIN_NOTES % LOGIN_PROFILE_DIR)

    fetcher = BrowserFetcher(
        headless=False, offscreen=False, profile_dir=LOGIN_PROFILE_DIR
    ).start()
    try:
        fetcher.open_page(LOGIN_URL)
        print(
            "\nA browser window is open at Upwork's sign-in page.\n"
            "Sign in there — including any 2FA. This notices by itself when you\n"
            f"are done, and gives up after {args.login_timeout // 60} minutes.\n"
        )

        if not fetcher.wait_for_login(timeout_s=args.login_timeout):
            print(
                "No Upwork session appeared in the browser profile.\n"
                "Nothing was changed — anonymous runs are unaffected."
            )
            return 1

        # Google sign-in offers no "keep me logged in", so Upwork may hand out
        # session-scoped cookies that Chrome drops on exit. Save them with an
        # explicit expiry rather than relying on a checkbox that isn't there.
        saved = fetcher.save_session()
        if saved:
            print(f"\n  Session saved ({saved} cookies) — it will persist between runs.")
            print(f"  Stored in {fetcher.session_path}")
            print("  That file holds live tokens; treat it like a password.")
        else:
            print("\n  !! No Upwork cookies found to save — the sign-in may not have taken.")

        print("\nSigned in. Checking what that unlocks...")

        url = _sample_job_url()
        if not url:
            print("Signed in, but no sample job could be fetched to verify with.")
            return 0

        print(f"\nChecking what a signed-in session returns for:\n  {url}")
        status, html = fetcher.fetch(url)
        if status != 200:
            print(f"  page not retrieved (HTTP {status})")
            return 1

        unlocked = _report_gated_fields(html, url.rsplit("/", 1)[-1])
        print(
            "\n  -> hire rate is now available; enrichment will include it."
            if unlocked else
            "\n  -> still no hire rate. Either the sign-in did not take, or this\n"
            "     client has no posting history. Try --login-status, or run\n"
            "     --login again and check the window really shows you signed in."
        )
        return 0
    finally:
        fetcher.close()


def report_login_status(args) -> int:
    """Fetch one job page and report whether signed-in-only fields appear."""
    url = _sample_job_url()
    if not url:
        print("Could not fetch a job to test with.")
        return 1

    fetcher = BrowserFetcher(
        headless=False, offscreen=not args.headful, profile_dir=LOGIN_PROFILE_DIR
    ).start()
    try:
        status, html = fetcher.fetch(url)
        if status != 200:
            print(f"Page not retrieved (HTTP {status})")
            return 1
        signed_in = "global-signup-desktop-login" not in html
        print(f"Profile: {LOGIN_PROFILE_DIR}")
        print(f"Signed in: {signed_in}")
        _report_gated_fields(html, url.rsplit("/", 1)[-1])
        return 0 if signed_in else 1
    finally:
        fetcher.close()


def resolve_max_age(args) -> float | None:
    """--max-age-days is just a friendlier --max-age; days win if both given."""
    if args.max_age_days is not None:
        return args.max_age_days * 24 * 60
    return args.max_age


def resolve_country_filter(args) -> CountryFilter:
    """Build the country filter from flags and `EXCLUDED_COUNTRIES`.

    Order: the env list (or the built-in default when unset), plus
    `--exclude-country`, minus `--allow-country`. `--no-country-filter` wins
    over all of it.

    A warning fires when the filter is armed but nothing will be enriched,
    because that combination silently keeps everything: country is only ever
    known from a client lookup.
    """
    if args.no_country_filter:
        LOGGER.info("Country filter off — keeping every country")
        return CountryFilter()

    country_filter = CountryFilter.build(
        base=parse_country_list(config.EXCLUDED_COUNTRIES),
        add=parse_queries(args.exclude_country),
        remove=parse_queries(args.allow_country),
        drop_unknown=args.drop_unknown_country,
    )

    if not country_filter.is_active:
        LOGGER.info("Country filter off — no countries excluded")
        return country_filter

    LOGGER.info("Country filter: %s", country_filter.describe())
    if not args.enrich_clients:
        LOGGER.warning(
            "Country filter is on but --enrich-clients is not: the search API "
            "does not return the client's country, so nothing can be excluded. "
            "Add --enrich-clients, or --no-country-filter to silence this."
        )
    return country_filter


def probe_session_state(args) -> str:
    """Ask Upwork about the signed-in profile, then let go of it.

    Opened and closed on its own because Chrome allows one process per
    profile — the login window and the enrichment browser cannot both hold it.
    """
    fetcher = BrowserFetcher(
        headless=args.browser_headless,
        offscreen=not args.headful,
        profile_dir=LOGIN_PROFILE_DIR,
    ).start()
    try:
        return fetcher.session_state()
    finally:
        fetcher.close()


class RunAborted(Exception):
    """The user chose to stop rather than run without what they asked for."""


STATE_EXPLANATION = {
    "signed_out": "this profile is not signed in",
    "rate_limited": "Upwork is rate limiting this profile",
    "unknown": "the session could not be confirmed",
}

SESSION_OPTIONS = (
    ("login", "Sign in now — a browser window opens"),
    ("reset", "Reset this profile, then sign in (use if signing in keeps failing)"),
    ("anonymous", "Carry on without hire rate or jobs-posted"),
    ("stop", "Stop, change nothing"),
)


def can_ask(args) -> bool:
    """Whether there is a person at the keyboard to answer a question.

    Watch mode and --no-auto-login are explicit instructions not to block on
    one, so they never prompt however the run was started.
    """
    if args.no_auto_login or args.watch:
        return False
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def ask_about_session(state: str) -> str:
    """Ask what to do about a session that will not deliver hire rate.

    Printed to stderr, because stdout is the data stream.
    """
    # Signing in on top of a flagged profile just reproduces the block, so the
    # offered default changes with the diagnosis.
    default = "reset" if state == "rate_limited" else "login"
    keys = [key for key, _ in SESSION_OPTIONS]

    print(
        f"\n--logged-in was asked for, but {STATE_EXPLANATION.get(state, state)}.\n"
        "Hire rate and jobs-posted come only from a signed-in session; every\n"
        "other client field works either way.\n",
        file=sys.stderr,
    )
    for i, (key, label) in enumerate(SESSION_OPTIONS, 1):
        mark = "  <- default" if key == default else ""
        print(f"  [{i}] {label}{mark}", file=sys.stderr)

    try:
        answer = input(f"\nChoose 1-{len(keys)} [{keys.index(default) + 1}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return "stop"

    if not answer:
        return default
    if answer.isdigit() and 1 <= int(answer) <= len(keys):
        return keys[int(answer) - 1]
    # An unreadable answer must not silently pick the outcome they were being
    # warned about.
    print(f"Not one of the options — taking [{keys.index(default) + 1}].", file=sys.stderr)
    return default


def ensure_signed_in(args) -> bool:
    """Get the signed-in profile into a usable state, signing in if needed.

    Returns whether hire rate is available afterwards. At most one sign-in is
    attempted per run, so a refused login cannot loop.

    With someone at the keyboard the choice is theirs: --logged-in is asked for
    precisely when hire rate is the point of the run, and quietly downgrading
    it to an anonymous run produces a file that looks complete and has empty
    hire-rate columns. Unattended runs keep the old behaviour — sign in when
    allowed, otherwise warn and fall back — because there is nobody to ask and
    blocking on a window nobody will see is worse.
    """
    state = probe_session_state(args)
    if state == "live":
        return True

    if can_ask(args):
        choice = ask_about_session(state)
        if choice == "stop":
            raise RunAborted(
                "Stopped before scraping. Sign in with --login (or "
                "--reset-login then --login), or drop --logged-in to run "
                "anonymously."
            )
        if choice == "anonymous":
            LOGGER.warning(
                "Continuing anonymously — hire rate and jobs-posted will be empty."
            )
            return False
        if choice == "reset":
            reset_login_profile()

    else:
        if state == "unknown":
            # A timeout is not evidence of anything. Try the run as-is.
            LOGGER.warning(
                "Could not confirm the session — trying it anyway. If hire rate "
                "comes back empty, check with --login-status."
            )
            return True

        auto = not (args.no_auto_login or args.watch)
        if not auto:
            LOGGER.warning(
                "Session is %s and auto sign-in is off%s — continuing anonymously.",
                state, " (watch mode)" if args.watch else "",
            )
            return False

        if state == "rate_limited":
            # A flagged profile stays flagged; signing in again on top of it
            # just reproduces the block, so start from a clean one.
            LOGGER.warning(
                "This profile is rate limited. Resetting it and signing in again."
            )
            reset_login_profile()
        else:
            LOGGER.warning("Not signed in. Opening a sign-in window.")

    if run_login(args) != 0:
        LOGGER.warning("Sign-in did not complete — continuing anonymously.")
        return False

    state = probe_session_state(args)
    if state == "live":
        LOGGER.info("Signed in — hire rate is available.")
        return True

    LOGGER.warning(
        "Still %s after signing in — continuing anonymously.", state
    )
    return False


def build_enricher(args, proxy_manager):
    """The client-lookup stage. Returns None when not requested."""
    if not args.enrich_clients:
        return None

    min_delay, max_delay = args.enrich_delay or (
        config.ENRICH_MIN_DELAY, config.ENRICH_MAX_DELAY
    )
    # Cloudflare 403s plain HTTP on job pages, so a browser is the default
    # transport here. --no-browser opts out (and will be blocked).
    fetcher = None
    logged_in = args.logged_in

    # Settle the session before opening the enrichment browser: signing in
    # needs the profile to itself, and Chrome allows one process per profile.
    if logged_in and not args.no_browser:
        logged_in = ensure_signed_in(args)

    # What the run actually ended up with, as opposed to what was asked for.
    # `run_enrichment` uses it to check the session delivered what it promised.
    args.logged_in_active = logged_in

    if not args.no_browser:
        try:
            fetcher = BrowserFetcher(
                proxy_manager=proxy_manager,
                headless=args.browser_headless,
                offscreen=not args.headful,
                profile_dir=profile_for(logged_in),
            ).start()

            if logged_in:
                # ensure_signed_in already settled and verified the session;
                # re-checking here would just cost another request.
                LOGGER.info("Using the signed-in browser profile")
            elif args.logged_in:
                LOGGER.warning(
                    "Running anonymously: you still get location, spend, hires, "
                    "rating and proposals — everything except hire rate and "
                    "jobs-posted."
                )
        except RuntimeError as e:
            LOGGER.error("%s", e)
            return None
    else:
        LOGGER.warning(
            "--no-browser: plain HTTP will be blocked on job pages.\n%s",
            BROWSER_REQUIRED_HELP,
        )

    enricher = ClientEnricher(
        proxy_manager=proxy_manager,
        cookie=config.UPWORK_COOKIE,
        delay=HumanDelay(min_seconds=min_delay, max_seconds=max_delay),
        html_fetcher=fetcher,
    )

    if isinstance(proxy_manager, NoProxyManager):
        LOGGER.warning(
            "Enriching without proxies: every request comes from your own IP. "
            "Set WEBSHARE_URL to spread the load."
        )
    return enricher


def apply_limit(jobs, args):
    """Trim to the newest N jobs, before enrichment so all of them get enriched."""
    if not args.limit or len(jobs) <= args.limit:
        return jobs
    LOGGER.info("Keeping the newest %d of %d jobs", args.limit, len(jobs))
    return jobs[: args.limit]


def run_enrichment(enricher, jobs, args, country_filter=None) -> list:
    """Separate pass over already-scraped jobs, attaching client details.

    The fetching stays its own stage — jobs are scraped in full first, and an
    enrichment failure can never affect them. The *output* is merged: each job
    carries its poster's details under `client`, so a job can be qualified on
    client history without joining two files. `--separate-clients` restores the
    two-file behaviour.

    Returns the jobs to actually emit. `country_filter` is applied here, at the
    one point in the run where a country is known, and before the separate
    client file is written so both outputs describe the same set of jobs.
    """
    def _filtered(kept):
        return country_filter.apply(kept) if country_filter else list(kept)

    if not enricher or not jobs:
        return _filtered(jobs)

    targets = jobs[: args.enrich_limit] if args.enrich_limit else jobs
    results = enricher.enrich_all(targets)
    if not results:
        return _filtered(jobs)

    by_cipher = {r.cipher: r for r in results}
    for job in jobs:
        if job.cipher in by_cipher:
            job.client = by_cipher[job.cipher]

    usable = sum(1 for r in results if r.fetch_status == "ok")
    LOGGER.info("Client details attached for %d/%d jobs", usable, len(targets))

    # The session check runs before any job page is loaded, so it can only ever
    # predict. This is the outcome itself: a signed-in run that produced no
    # hire rate at all did not get what it was for, and saying so here beats
    # the user finding an empty column later.
    if getattr(args, "logged_in_active", False) and usable:
        if not any(r.hire_rate is not None for r in results):
            LOGGER.warning(
                "Signed in, but not one client came back with a hire rate. The "
                "session is probably not live after all — check it with "
                "--login-status, or run --reset-login then --login."
            )

    kept = _filtered(jobs)

    if not args.separate_clients:
        return kept

    # Optional second copy in its own file, for anyone joining on cipher. Only
    # the clients of jobs that survived the filter — an excluded job's poster
    # appearing here would contradict the main output.
    as_array = args.fmt == "json" and not args.watch
    suffix = "clients.json" if as_array else "clients.jsonl"
    out = args.clients_out or (f"{args.out}.{suffix}" if args.out else None)

    kept_ciphers = {j.cipher for j in kept}
    records = [
        r.model_dump(mode="json") for r in results if r.cipher in kept_ciphers
    ]
    text = (
        json.dumps(records, indent=2, ensure_ascii=False)
        if as_array
        else "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    )
    _emit(text, out)
    LOGGER.info("Client details also written to %s", out or "stdout")
    return kept


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    use_utf8_streams()
    init_logger(args.log_level)

    if args.list_niches:
        print("Built-in niches (copy and edit any of these files):\n")
        for name in available_niches():
            niche = load_niche(name)
            print(f"  {name:<10} {len(niche.keywords):>3} keywords — {niche.description}")
            print(f"  {'':<10} {NICHE_DIR / f'{name}.json'}\n")
        return 0

    if args.check_proxies:
        return report_proxy_check(args)

    if args.reset_login:
        return reset_login_profile()

    if args.login:
        return run_login(args)

    if args.login_status:
        return report_login_status(args)

    queries, niche = resolve_keywords(args)
    max_age_minutes = resolve_max_age(args)
    job_filter = None if (niche is None or args.no_filter) else niche.is_relevant
    country_filter = resolve_country_filter(args)

    proxy_manager = (
        NoProxyManager() if args.no_proxy else build_proxy_manager(args.webshare_url)
    )
    scraper = UpworkScraper(proxy_manager=proxy_manager, workers=args.workers)
    try:
        enricher = build_enricher(args, proxy_manager)
    except RunAborted as e:
        print(f"\n{e}", file=sys.stderr)
        return 3

    # Line-oriented formats are required whenever output is appended in stages.
    fmt = args.fmt
    appending = args.watch or (args.backfill and args.watch)
    if args.out and appending and fmt == "json":
        fmt = "jsonl"
        LOGGER.info("Appending to a file — using jsonl instead of json")

    backfilled: list[str] = []
    wrote_anything = False

    if args.backfill:
        keyword_count = len(queries) if queries else 1
        LOGGER.info(
            "Backfilling already-posted jobs: %d keyword(s) x up to %d pages "
            "(~%d requests, fewer where a keyword has less results)",
            keyword_count, args.backfill_pages, keyword_count * args.backfill_pages,
        )
        jobs = scraper.scrape(
            max_pages=args.backfill_pages,
            query=queries,
            sort=args.sort,
            pin_proxy=args.pin_proxy,
            max_age_minutes=max_age_minutes,
            job_filter=job_filter,
        )
        LOGGER.info("Backfill complete: %d unique jobs", len(jobs))
        jobs = apply_limit(jobs, args)
        backfilled = [j.cipher for j in jobs if j.cipher]

        jobs = run_enrichment(enricher, jobs, args, country_filter)

        text = render(jobs, fmt if args.watch else args.fmt)
        _emit(text, args.out)
        wrote_anything = True

        if not args.watch:
            return 0

    elif not args.watch:
        jobs = scraper.scrape(
            max_pages=args.pages,
            query=queries,
            sort=args.sort,
            pin_proxy=args.pin_proxy,
            max_age_minutes=max_age_minutes,
            job_filter=job_filter,
        )
        LOGGER.info("Scraped %d jobs", len(jobs))
        jobs = apply_limit(jobs, args)
        jobs = run_enrichment(enricher, jobs, args, country_filter)
        _emit(render(jobs, args.fmt), args.out)
        return 0

    stop = threading.Event()

    def _handle_signal(sig, _frame):
        LOGGER.info("Received %s, shutting down...", signal.Signals(sig).name)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    wrote_header = wrote_anything
    for jobs in scraper.scrape_loop(
        interval=args.interval,
        max_pages=args.pages,
        query=queries,
        sort=args.sort,
        pin_proxy=args.pin_proxy,
        max_age_minutes=max_age_minutes,
        skip_backlog=args.skip_backlog,
        job_filter=job_filter,
        initial_seen=backfilled,
        stop_event=stop,
    ):
        if not jobs:
            LOGGER.info("No new jobs this cycle")
            continue

        jobs = apply_limit(jobs, args)
        jobs = run_enrichment(enricher, jobs, args, country_filter)
        if not jobs:
            LOGGER.info("Every new job this cycle was filtered out")
            continue

        text = render(jobs, fmt)
        if fmt == "csv" and wrote_header:
            text = text.split("\n", 1)[1] if "\n" in text else ""
        if text:
            _emit(text, args.out, append=wrote_header)
            wrote_header = True
        LOGGER.info("Emitted %d new jobs", len(jobs))

    return 0


if __name__ == "__main__":
    sys.exit(main())
