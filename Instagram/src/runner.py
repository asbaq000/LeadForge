"""
Instagram Profiles Scraper PPR — entry point.

Usage:
    python src/runner.py instagram natgeo nasa
    python src/runner.py -i data/inputs.sample.txt -o data/output.json
    python src/runner.py -i data/inputs.sample.txt -f csv -o data/output.csv
    python src/runner.py -c src/config/settings.json -i data/inputs.sample.txt

CLI flags always win over the settings file, and the settings file always
wins over the built-in defaults.
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Let the script be run directly (python src/runner.py) without installing
# anything or fiddling with PYTHONPATH.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _sub in ("extractors", "outputs"):
    sys.path.insert(0, os.path.join(_HERE, _sub))

import exporters  # noqa: E402
from instagram_parser import (  # noqa: E402
    ProfileNotFound,
    RateLimited,
    ScrapeError,
    build_session,
    scrape_profile,
)
from utils_format import normalize_username, read_usernames  # noqa: E402

DEFAULTS = {
    "delay_between_profiles": 2.0,
    "timeout": 20,
    "max_retries": 3,
    "retry_backoff": 5.0,
    "concurrency": 1,
    "output_format": "json",
    "output_path": "data/output.json",
    "pretty": True,
    "proxies": {},
    "headers": {},
}


def load_settings(path):
    settings = dict(DEFAULTS)
    if not path:
        return settings
    if not os.path.exists(path):
        sys.exit(f"Settings file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        user_settings = json.load(f)
    settings.update({k: v for k, v in user_settings.items() if not k.startswith("_")})
    return settings


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrape public Instagram profiles into structured JSON/CSV."
    )
    parser.add_argument("usernames", nargs="*", help="usernames, @handles or profile URLs")
    parser.add_argument("-i", "--input", help="file with one username per line")
    parser.add_argument("-o", "--output", help="output file path")
    parser.add_argument("-f", "--format", choices=["json", "jsonl", "csv"], help="output format")
    parser.add_argument("-c", "--config", help="path to a settings.json")
    parser.add_argument("--delay", type=float, help="seconds to wait between profiles")
    parser.add_argument("--concurrency", type=int, help="parallel workers (default 1)")
    parser.add_argument("--compact", action="store_true", help="write JSON without indentation")
    parser.add_argument("--quiet", action="store_true", help="only print the final summary")
    return parser.parse_args(argv)


def collect_usernames(args):
    usernames, seen = [], set()

    for raw in args.usernames:
        name = normalize_username(raw)
        if name and name not in seen:
            seen.add(name)
            usernames.append(name)

    if args.input:
        if not os.path.exists(args.input):
            sys.exit(f"Input file not found: {args.input}")
        for name in read_usernames(args.input):
            if name not in seen:
                seen.add(name)
                usernames.append(name)

    return usernames


def _scrape_one(session, username, settings, log):
    """Return (record, error_dict) — exactly one of the two is None."""
    try:
        record = scrape_profile(session, username, settings)
    except ProfileNotFound as exc:
        log(f"  not found: {exc}")
        return None, {"username": username, "error": str(exc)}
    except RateLimited as exc:
        log(f"  rate limited: {exc}")
        return None, {"username": username, "error": str(exc)}
    except ScrapeError as exc:
        log(f"  error: {exc}")
        return None, {"username": username, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — one bad profile shouldn't kill the run
        log(f"  failed: {exc}")
        return None, {"username": username, "error": f"unexpected error: {exc}"}

    log(
        f"  {record['follower_count']:,} followers · "
        f"{record['media_count']:,} posts · "
        f"{'verified' if record['is_verified'] else 'not verified'}"
    )
    return record, None


def run_sequential(session, usernames, settings, log):
    records, errors = [], []
    delay = settings["delay_between_profiles"]

    for index, username in enumerate(usernames, start=1):
        log(f"[{index}/{len(usernames)}] @{username}")
        record, error = _scrape_one(session, username, settings, log)
        (records if record else errors).append(record or error)
        if index < len(usernames) and delay > 0:
            time.sleep(delay)

    return records, errors


def run_parallel(session, usernames, settings, log, workers):
    """
    Parallel mode trades rate-limit safety for speed — Instagram's anonymous
    quota is per-IP, so more workers means you hit the wall sooner. Kept
    opt-in for when you're running behind rotating proxies.
    """
    records, errors = [], []

    def run(username):
        # Buffer each worker's output so lines from concurrent profiles
        # don't interleave into nonsense.
        lines = []
        result = _scrape_one(session, username, settings, lines.append)
        return result, lines

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run, username): username for username in usernames}
        for done, future in enumerate(as_completed(futures), start=1):
            username = futures[future]
            (record, error), lines = future.result()
            log(f"[{done}/{len(usernames)}] @{username}")
            for line in lines:
                log(line)
            (records if record else errors).append(record or error)

    # Restore the caller's original ordering — as_completed scrambles it.
    order = {name: i for i, name in enumerate(usernames)}
    records.sort(key=lambda r: order.get(r["username"], 0))
    errors.sort(key=lambda e: order.get(e["username"], 0))
    return records, errors


def main(argv=None):
    args = parse_args(argv)
    settings = load_settings(args.config)

    # CLI overrides
    if args.delay is not None:
        settings["delay_between_profiles"] = args.delay
    if args.concurrency is not None:
        settings["concurrency"] = args.concurrency
    if args.format:
        settings["output_format"] = args.format
    if args.output:
        settings["output_path"] = args.output
    if args.compact:
        settings["pretty"] = False

    log = (lambda *a, **k: None) if args.quiet else print

    usernames = collect_usernames(args)
    if not usernames:
        sys.exit("No usernames given. Pass them as arguments or use -i <file>.")

    log(f"Scraping {len(usernames)} profile(s)...\n")
    session = build_session(settings)
    workers = max(1, int(settings["concurrency"]))

    started = time.time()
    if workers > 1:
        records, errors = run_parallel(session, usernames, settings, log, workers)
    else:
        records, errors = run_sequential(session, usernames, settings, log)
    elapsed = time.time() - started

    output_path = settings["output_path"]
    exporters.export(
        records, output_path, settings["output_format"], settings.get("pretty", True)
    )

    print(f"\nDone in {elapsed:.1f}s — {len(records)} scraped, {len(errors)} failed.")
    print(f"Output: {output_path}")

    if errors:
        error_path = os.path.splitext(output_path)[0] + ".errors.json"
        exporters.write_json(errors, error_path)
        print(f"Errors: {error_path}")

    return 0 if records else 1


if __name__ == "__main__":
    sys.exit(main())
