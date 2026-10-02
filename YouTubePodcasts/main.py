# LeadForge · asbaq000
"""YouTube PODCASTER lead scraper -- CLI entry point.

Finds podcast channels worldwide, verifies they really are podcasts, enriches
each one into a full outreach lead, and writes CSV + Google Sheets.

    python main.py run                  # discover, gate, enrich, push to CSV + Sheets
    python main.py run --max-channels 200 --no-sheets
    python main.py run --no-csv         # skip the CSV write, e.g. if exports/ is locked
    python main.py export               # push anything the DB has not synced yet
    python main.py reclassify           # re-run LLM profiling on existing leads
    python main.py csv                  # re-dump the LAST run's leads
    python main.py csv --all            # dump every lead ever found
    python main.py test-llm             # check the LLM providers, no quota spent
    python main.py stats                # what is in the DB and today's quota
    python main.py init-sheet           # create the tabs and headers up front
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

from ytleads import pipeline, sheets
from ytleads.config import ConfigError, load_config, load_seeds
from ytleads.podcast import all_genres
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


# -- commands -------------------------------------------------------------

_STAT_FIELDS = (
    "discovered", "already_known", "fetched", "rejected_subs",
    "rejected_few_videos", "rejected_inactive", "rejected_not_podcast",
    "rejected_no_contact", "rejected_duplicate_contact", "qualified",
    "with_email", "with_platform", "with_booking", "pending_classification",
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
        region = str(cfg.get("discovery.region_code", "") or "") or "worldwide"
        log("=" * 78)
        log(f"  YouTube PODCASTER lead scraper -- {date.today().isoformat()}")
        log(f"  seeds: {len(seeds)}   quota today: {client.used}/{client.daily_limit}"
            f"   available: {client.remaining}")
        log(f"  range: {int(cfg.get('filters.min_subscribers', 500)):,}"
            f"-{int(cfg.get('filters.max_subscribers', 500000)):,} subs"
            f"   cadence: <= {cfg.get('filters.max_days_since_last_upload', 45)}d"
            f"   region: {region}")
        log(f"  podcast gate: score >= {cfg.get('podcast.min_score', 5.0)} "
            f"(a show name or a platform link passes on its own)")
        log(f"  known channels in DB: {len(store.known_ids()):,} (never re-checked)")
        log(f"  target: at least {min_leads} qualified podcaster leads before stopping")
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
                        f"quota don't have {min_leads} more on-target podcasts left -- "
                        f"this is a real ceiling, not a bug. Add seeds in other "
                        f"languages, or lower `discovery.min_qualified_leads`.")
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
                    # Only ever ask for the shortfall. Without this a single
                    # 50-candidate search page overshoots the target badly,
                    # spending quota and LLM calls on leads you did not ask for.
                    stop_at=max(0, min_leads - st.qualified),
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
            # THIS RUN'S LEADS ONLY. The filename carries a timestamp so a run
            # can never overwrite or blend into the previous one -- yesterday's
            # 30 stay in yesterday's file, and this file holds only what was
            # found just now.
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            csv_out = (Path(args.csv_out) if args.csv_out
                       else ROOT / "exports" / f"leads_{stamp}.csv")
            _write_csv(store, csv_out, store.leads_for_run(run_id))
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


def _write_csv(store: Store, out: Path, leads=None) -> int:
    """Write `leads` (default: every qualified lead ever) to a CSV.

    UTF-8 with BOM so Excel and Google Sheets both read non-Latin show names
    (Arabic, Hindi, CJK) correctly on import instead of as mojibake.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    leads = store.all_leads() if leads is None else leads
    if not leads:
        log("No leads to write.")
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
    with Store() as store:
        if args.all:
            leads = store.all_leads()
            default_name = "leads_all.csv"
        else:
            run_id = store.last_run_id()
            leads = store.leads_for_run(run_id)
            default_name = f"leads_run{run_id}.csv" if run_id else "leads.csv"
            if not leads:
                log("The last run produced no leads. Use --all to dump the "
                    "whole database instead.")
        out = Path(args.out) if args.out else ROOT / "exports" / default_name
        _write_csv(store, out, leads)
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

        genres = store.genre_counts()
        if genres:
            log("\nQualified podcasters by genre:")
            for genre, n in genres.items():
                log(f"    {sheets.tab_name(genre):<28} {n:>7,}")

        formats = store.format_counts()
        if formats:
            log("\nQualified podcasters by format:")
            for fmt, n in formats.items():
                log(f"    {sheets.format_label(fmt):<28} {n:>7,}")

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
    names = [str(cfg.get("sheets.master_tab", "Podcaster Leads"))]
    if args.all_tabs:
        names += [sheets.tab_name(g) for g in all_genres()]
    for name in names:
        writer.ensure_header(writer.tab(name))
        log(f"    ready: {name}")
    log(f"\n{len(names)} tab(s) ready in the spreadsheet.")
    return 0


def cmd_test_llm(args: argparse.Namespace) -> int:
    """Probe every configured provider once and report the real reason.

    This exists because a stale model id and a revoked key are invisible
    during a run -- both just produce unclassified leads. One command, one
    real request per attempt, the actual HTTP status printed.
    """
    from ytleads import llm_classify, podcast

    cfg = load_config(args.config)
    sx = pipeline.Settings.from_config(cfg)
    chain = llm_classify.build_chain(sx)
    if not chain:
        log("[!] No API keys configured in .env (GROQ_API_KEY / GEMINI_API_KEY / "
            "OPENROUTER_API_KEYS).")
        log("    That is survivable: set classify.method: keyword in config.yaml "
            "and the scraper runs fully offline.")
        return 1

    channel = {"snippet": {
        "title": "TAP - The Aman Podcast",
        "description": "Honest conversations with founders and artists. "
                       "Hosted by Aman Verma.",
    }}
    titles = ["Ep 22 ft. Rhea Kapoor", "Ep 21 w/ Vikram Shah", "Ep 20 with Dr. Neha Rao"]
    facts = "53,000 subscribers; median upload 62 min; ~4 uploads/month"

    log(f"\nProbing {len(chain)} configured attempt(s) with one real request each.\n")
    working = 0
    for kind, label, api_key, model in chain:
        caller = llm_classify._CALLERS[kind]
        try:
            text, err = caller(model, api_key, "\n".join(
                [f"Channel name: {channel['snippet']['title']}",
                 f"Description: {channel['snippet']['description']}",
                 f"Metadata: {facts}",
                 "Recent video titles:"] + [f"- {t}" for t in titles]
            ), sx.llm_timeout, 1, sx.llm_max_tokens)
        except Exception as exc:  # noqa: BLE001
            text, err = None, f"{type(exc).__name__}: {exc}"

        if err:
            log(f"  [FAIL] {label:<14} {model}")
            log(f"         {err}")
            continue
        info = llm_classify.parse_reply(text or "")
        if info is None:
            log(f"  [FAIL] {label:<14} {model}")
            log(f"         answered, but nothing usable in it: "
                f"{' '.join((text or '').split())[:120]}")
            continue
        working += 1
        log(f"  [ OK ] {label:<14} {model}")
        log(f"         genre={info.genre} format={info.fmt} "
            f"host={info.host!r} language={info.language!r}")

    log("")
    if working:
        log(f"{working}/{len(chain)} attempt(s) working -- classification will run.")
    else:
        log(f"0/{len(chain)} attempts working.")
        log("  * HTTP 404          -> the model id is retired. Update "
            "classify.llm_model_* in config.yaml.")
        log("  * HTTP 401/403      -> the key is bad or the project is blocked. "
            "Replace or remove it from .env.")
        log("  * HTTP 429          -> free tier exhausted; it will work again later.")
        log("  * empty reply       -> raise classify.llm_max_tokens.")
        log("\nNone of this blocks a run: leads still get every contact detail, "
            "and the offline reader still fills in host name and language.")

    offline = llm_classify.offline_profile(
        podcast.evaluate(channel, video_titles=titles), channel
    )
    log(f"\nOffline reader (no API, always available): "
        f"host={offline.host!r} language={offline.language or '(latin script)'!r} "
        f"genre={offline.genre}")
    return 0 if working else 1


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
        ("rejected: NOT A PODCAST", st.rejected_not_podcast),
        ("rejected: no contact info", st.rejected_no_contact),
        ("rejected: duplicate email", st.rejected_duplicate_contact),
        ("QUALIFIED PODCASTER LEADS", st.qualified),
        ("  ...with an email", st.with_email),
        ("  ...with a Spotify/Apple/RSS link", st.with_platform),
        ("  ...with a guest-booking form", st.with_booking),
        ("  ...pending genre classification", st.pending_classification),
        ("quota units spent", st.quota_used),
    ]
    for label, value in rows:
        log(f"    {label:<38} {value:>8,}")
    if st.sheet_rows:
        log(f"    {'rows written to sheets':<38} {sum(st.sheet_rows.values()):>8,}")
    log(f"    {'total channels known (never re-run)':<38} {len(store.known_ids()):>8,}")
    log("=" * 78)
    if st.pending_classification:
        log(f"\n{st.pending_classification} lead(s) need genre classification -- every "
            f"configured API key failed or rate-limited for them. Run "
            f"`python main.py reclassify` once keys have room again.")


# -- argparse -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="YouTube podcaster lead scraper for outreach.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="discover, gate, enrich and export")
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
                   help="skip About-page enrichment (faster, but loses most "
                        "Spotify/Apple links -- the best podcast signal there is)")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("export", help="push DB leads to Google Sheets")
    e.add_argument("--all", action="store_true",
                   help="resync every qualified lead, not just unsynced ones")
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("csv", help="dump leads to CSV (last run by default)")
    c.add_argument("--out", default=None, help="output path")
    c.add_argument("--all", action="store_true",
                   help="dump every lead in the database, not just the last run's")
    c.set_defaults(func=cmd_csv)

    rc = sub.add_parser("reclassify",
                        help="re-run LLM profiling on existing leads (0 YouTube quota)")
    rc.set_defaults(func=cmd_reclassify)

    s = sub.add_parser("stats", help="database and quota summary")
    s.set_defaults(func=cmd_stats)

    t = sub.add_parser("test-llm",
                       help="probe every configured LLM provider and say what is wrong")
    t.set_defaults(func=cmd_test_llm)

    i = sub.add_parser("init-sheet", help="create tabs and headers")
    i.add_argument("--all-tabs", action="store_true",
                   help="pre-create a tab for every genre")
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
