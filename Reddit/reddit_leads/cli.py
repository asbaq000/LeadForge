"""Command line entry point: python -m reddit_leads"""

import argparse
import json
import sys

from . import config, export, fetch, pipeline


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="reddit_leads",
        description="Find recent remote AI job / AI project leads on Reddit. "
                    "Uses Reddit's public JSON API - no account or API key needed.",
    )
    parser.add_argument("-o", "--out", default="leads.csv", help="output CSV path")
    parser.add_argument("-n", "--limit", type=int, default=config.DEFAULT_LEAD_COUNT,
                        help="number of leads to return; 0 = no limit (default)")
    parser.add_argument("-d", "--days", type=int, default=config.DEFAULT_MAX_AGE_DAYS,
                        help="maximum post age in days (default: 14)")
    parser.add_argument("--delay", type=float, default=config.REQUEST_DELAY_SECONDS,
                        help="seconds between requests (default: 2)")
    parser.add_argument("--save-raw", metavar="PATH",
                        help="also save every fetched post as JSON")
    parser.add_argument("--from-json", metavar="PATH",
                        help="score posts from a saved JSON file instead of fetching")
    parser.add_argument("-q", "--quiet", action="store_true", help="less output")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    verbose = not args.quiet

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as handle:
            posts = json.load(handle)
        errors = []
        if verbose:
            print("Loaded %d posts from %s" % (len(posts), args.from_json))
    else:
        if verbose:
            print("Fetching Reddit's public JSON API (no login)...")
        posts, errors = fetch.collect(verbose=verbose, delay=args.delay)

    if args.save_raw:
        with open(args.save_raw, "w", encoding="utf-8") as handle:
            json.dump(posts, handle)

    rows, stats = pipeline.build_leads(posts, args.days, args.limit)

    if not rows:
        print("No leads matched. Try --days 21 or widen the queries in config.py.",
              file=sys.stderr)
        return 1

    export.write_csv(rows, args.out)

    if verbose:
        print("\n%d posts fetched -> %d passed filters -> %d after dedupe (%d duplicates removed)"
              % (stats["fetched"], stats["passed_filters"], stats["unique"],
                 stats["duplicates_removed"]))
        print("Top rejection reasons:")
        for reason, count in sorted(stats["rejections"].items(),
                                    key=lambda kv: kv[1], reverse=True)[:6]:
            print("  %5d  %s" % (count, reason))
        if errors:
            print("%d source(s) failed:" % len(errors))
            for label, message in errors[:5]:
                print("  %s: %s" % (label, message))
    print("\nWrote %d leads to %s" % (len(rows), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
