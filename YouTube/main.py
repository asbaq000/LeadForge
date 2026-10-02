# LeadForge · asbaq000
"""YouTube lead scraper -- CLI entry point.

    python main.py run                  # discover, filter, classify, push to Sheets + CSV
    python main.py run --max-channels 200 --no-sheets
    python main.py run --no-csv         # skip the CSV write, e.g. if exports/ is locked
    python main.py export               # push anything the DB has not synced yet
    python main.py reclassify           # re-run LLM classification on existing leads
    python main.py csv                  # dump qualified leads to exports/leads.csv on demand
    python main.py stats                # what is in the DB and today's quota
    python main.py init-sheet           # create the tabs and headers up front
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from ytleads import pipeline, sheets
from ytleads.classify import all_categories
from ytleads.config import ConfigError, load_config, load_seeds
from ytleads.store import Store
from ytleads.youtube_api import QuotaExhausted, YouTubeClient

ROOT = Path(__file__).resolve().parent

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _client(cfg, store: Store) -> YouTubeClient:
    return YouTubeClient(
        api_key=cfg.youtube_api_key,
        store=store,
        daily_limit=int(cfg.get("quota.daily_limit", 10_000)),
        reserve=int(cfg.get("quota.reserve", 200)),
    )


# ── commands ─────────────────────────────────────────────────────────────

_STAT_FIELDS = (
    "discovered", "already_known", "fetched", "rejected_subs",
    "rejected_few_videos", "rejected_inactive", "excluded_animation",
    "excluded_motion_graphics", "rejected_no_contact",
    "rejected_duplicate_contact", "qualified", "with_email",
    "pending_classification",
)


def _merge_stats(total: pipeline.Stats, st: pipeline.Stats) -> None:
    for f in _STAT_FIELDS:
        setattr(total, f, getattr(total, f) + getattr(st, f))


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    seeds = load_seeds(args.seeds or cfg.get("discovery.seeds_file", "seeds.txt"))
    if args.seed:
        seeds = args.seed

    min_leads = (args.min_leads if args.min_leads is not None
                 else int(cfg.get("discovery.min_qualified_leads", 50)))
    max_passes = int(cfg.get("discovery.max_discovery_passes", 6))

    with Store() as store:
        client = _client(cfg, store)
        log("=" * 78)
        log(f"  YouTube lead scraper -- {date.today().isoformat()}")
        log(f"  seeds: {len(seeds)}   quota today: {client.used}/{client.daily_limit}"
            f"   available: {client.remaining}")
        log(f"  range: {int(cfg.get('filters.min_subscribers', 1000)):,}"
            f"-{int(cfg.get('filters.max_subscribers', 1000000)):,} subs"
            f"   cadence: <= {cfg.get('filters.max_days_since_last_upload', 21)}d")
        log(f"  known channels in DB: {len(store.known_ids()):,} (never re-checked)")
        log(f"  target: at least {min_leads} qualified leads before stopping")
        log("=" * 78)

        if client.remaining < 100:
            log("[!] Not enough quota left today to run a search. Try after the "
                "midnight-Pacific reset, or run `export` to sync what you have.")
            return 1

        run_id = store.start_run()
        st = pipeline.Stats()
        pages = int(cfg.get("discovery.search_pages_per_seed", 1))
        quota_reserve = int(cfg.get("quota.reserve", 200))
        try:
            attempt = 0
            while True:
                attempt += 1
                if attempt > max_passes:
                    log(f"\n[!] Hit the {max_passes}-pass safety cap at "
                        f"{st.qualified}/{min_leads} qualified leads. Today's seeds and "
                        f"quota don't have {min_leads} more on-target channels left -- "
                        f"this is a real ceiling, not a bug. Tune `seeds.txt` or lower "
                        f"`discovery.min_qualified_leads` if this keeps happening.")
                    break
                if client.remaining < quota_reserve + 100:
                    log(f"\n[!] Only {client.remaining} quota units left today -- "
                        f"stopping at {st.qualified}/{min_leads} qualified leads.")
                    break
                if attempt > 1:
                    log(f"\n--- pass {attempt}: {st.qualified}/{min_leads} qualified so far, "
                        f"searching one page deeper per seed for more ---")
                pass_st = pipeline.run(
                    cfg, store, client, seeds,
                    max_channels=args.max_channels,
                    log=log,
                    about_pages=None if args.about is None else args.about,
                    pages_per_seed=pages,
                )
                _merge_stats(st, pass_st)
                if st.qualified >= min_leads:
                    log(f"\nReached target: {st.qualified}/{min_leads} qualified leads.")
                    break
                if pass_st.discovered == 0:
                    pages += 1  # today's shallower pages are used up; go deeper next pass
        except QuotaExhausted as exc:
            log(f"\n[!] {exc}")
        except KeyboardInterrupt:
            log("\n[!] Interrupted -- progress is saved in leads.db.")
            store.commit()
            return 130
        st.quota_used = client.used

        if not args.no_sheets:
            log("\n[4/5] Google Sheets")
            st.sheet_rows = pipeline.export(cfg, store, log=log)
        else:
            log("\n[4/5] Sheets export skipped (--no-sheets)")

        if not args.no_csv:
            log("\n[5/5] CSV export")
            csv_out = Path(args.csv_out) if args.csv_out else ROOT / "exports" / "leads.csv"
            _write_csv(store, csv_out)
        else:
            log("\n[5/5] CSV export skipped (--no-csv)")

        store.end_run(run_id, st.as_dict())
        _summary(st, store)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with Store() as store:
        pipeline.export(cfg, store, log=log, resync_all=args.all)
    return 0


def _write_csv(store: Store, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    leads = store.all_leads()
    if not leads:
        log("No qualified leads in the DB yet.")
        return 0
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(sheets.HEADERS)
        for lead in leads:
            writer.writerow(sheets.row_from_lead(lead))
    log(f"Wrote {len(leads)} leads -> {out}")
    return len(leads)


def cmd_csv(args: argparse.Namespace) -> int:
    load_config(args.config)
    out = Path(args.out) if args.out else ROOT / "exports" / "leads.csv"
    with Store() as store:
        _write_csv(store, out)
    return 0


def cmd_reclassify(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with Store() as store:
        pipeline.reclassify(cfg, store, log=log)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    with Store() as store:
        used = store.quota_used()
        limit = int(cfg.get("quota.daily_limit", 10_000))
        log(f"\nQuota today: {used:,}/{limit:,}  ({max(0, limit - used):,} left)")

        counts = store.status_counts()
        total = sum(counts.values())
        log(f"\nChannels evaluated: {total:,}")
        for status, n in counts.items():
            log(f"    {status:<28} {n:>7,}")

        cats = store.category_counts()
        if cats:
            log(f"\nQualified leads by content type:")
            for cat, n in cats.items():
                log(f"    {sheets.tab_name(cat):<28} {n:>7,}")

        unsynced = len(store.unsynced_leads())
        if unsynced:
            log(f"\n{unsynced:,} lead(s) not yet in the sheet -- run `python main.py export`")
    return 0


def cmd_init_sheet(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    sid = cfg.spreadsheet_id
    if not sid:
        log("[!] Set SPREADSHEET_ID in .env first.")
        return 1
    creds = cfg.service_account_file
    if not creds.exists():
        log(f"[!] Service account file not found: {creds}")
        return 1

    writer = sheets.SheetsWriter(str(creds), sid)
    names = [str(cfg.get("sheets.master_tab", "Youtube Leads"))]
    if cfg.get("sheets.tab_per_category", True) and args.all_tabs:
        names += [sheets.tab_name(c) for c in all_categories()]
    for name in names:
        writer.tab(name)
        writer.ensure_header(writer.tab(name))
        log(f"    ready: {name}")
    log(f"\n{len(names)} tab(s) ready in the spreadsheet.")
    return 0


def _summary(st: pipeline.Stats, store: Store) -> None:
    log("\n" + "=" * 78)
    log("  RUN SUMMARY")
    log("=" * 78)
    rows = [
        ("new candidates discovered", st.discovered),
        ("channel records fetched", st.fetched),
        ("rejected: outside sub range", st.rejected_subs),
        ("rejected: too few videos", st.rejected_few_videos),
        ("rejected: inactive / irregular", st.rejected_inactive),
        ("excluded: animation", st.excluded_animation),
        ("excluded: motion graphics", st.excluded_motion_graphics),
        ("rejected: no contact info", st.rejected_no_contact),
        ("rejected: duplicate email", st.rejected_duplicate_contact),
        ("QUALIFIED LEADS", st.qualified),
        ("  ...of those, with an email", st.with_email),
        ("  ...of those, pending classification", st.pending_classification),
        ("quota units spent", st.quota_used),
    ]
    for label, value in rows:
        log(f"    {label:<34} {value:>8,}")
    if st.sheet_rows:
        log(f"    {'rows written to sheets':<34} {sum(st.sheet_rows.values()):>8,}")
    log(f"    {'total channels known (never re-run)':<34} {len(store.known_ids()):>8,}")
    log("=" * 78)
    if st.pending_classification:
        log(f"\n{st.pending_classification} lead(s) need classification -- every configured "
            f"API key failed or rate-limited for them. Run `python main.py reclassify` once "
            f"keys have room again (rate limits reset, or add another key).")


# ── argparse ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="YouTube channel lead scraper for outreach.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="discover, filter, classify and export")
    r.add_argument("--max-channels", type=int, default=None,
                   help="cap new candidates per discovery stage, per pass")
    r.add_argument("--min-leads", type=int, default=None,
                   help="keep running discovery passes until this many qualified "
                        "leads are found today (default: discovery.min_qualified_leads)")
    r.add_argument("--seeds", default=None, help="override the seeds file")
    r.add_argument("--seed", action="append", default=None,
                   help="run a single keyword (repeatable)")
    r.add_argument("--no-sheets", action="store_true", help="skip the Sheets export")
    r.add_argument("--no-csv", action="store_true", help="skip the CSV export")
    r.add_argument("--csv-out", default=None, help="override the CSV output path")
    r.add_argument("--about", dest="about", action="store_true", default=None,
                   help="force About-page enrichment on")
    r.add_argument("--no-about", dest="about", action="store_false",
                   help="skip About-page enrichment (faster, fewer socials)")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("export", help="push DB leads to Google Sheets")
    e.add_argument("--all", action="store_true",
                   help="resync every qualified lead, not just unsynced ones")
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("csv", help="dump qualified leads to CSV")
    c.add_argument("--out", default=None)
    c.set_defaults(func=cmd_csv)

    rc = sub.add_parser("reclassify",
                        help="re-run LLM classification on existing leads (0 YouTube quota)")
    rc.set_defaults(func=cmd_reclassify)

    s = sub.add_parser("stats", help="database and quota summary")
    s.set_defaults(func=cmd_stats)

    i = sub.add_parser("init-sheet", help="create tabs and headers")
    i.add_argument("--all-tabs", action="store_true",
                   help="pre-create a tab for every content type")
    i.set_defaults(func=cmd_init_sheet)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        log(f"[!] Config error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
