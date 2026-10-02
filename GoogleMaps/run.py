#!/usr/bin/env python3
# LeadForge · asbaq000
"""Google Maps lead scraper — command line.

Find every business of a given type in a city that has no website, recover
their phone and (where possible) email, and push them into a Google Sheet,
one tab per client type.

    python run.py --city "Karachi, Pakistan" --niche dentist --target 1000
    python run.py --city Karachi,Lahore --country Pakistan --niche dentist,gym

Or skip all this and use the point-and-click version:

    python ui.py

Runs are resumable: stop with Ctrl+C and re-run the same command to pick up
where it left off. Nothing is ever collected or written twice.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Business names are frequently non-Latin; the Windows console defaults to
# cp1252 and would crash on them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import (
    BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn,
)
from rich.table import Table

from gmaps_leads.pipeline import RunConfig, Runner
from gmaps_leads.sheets import tab_title
from gmaps_leads.store import LeadStore

console = Console()

_STYLES = {
    "info": "dim",
    "good": "green",
    "warn": "yellow",
    "error": "red",
    "step": "bold",
}


def split_list(value: str) -> list[str]:
    """'dentist, gym' -> ['dentist', 'gym']."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Scrape Google Maps for businesses with no website.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python run.py --city "Karachi, Pakistan" --niche dentist --target 1000\n'
            '  python run.py --city Karachi,Lahore --country Pakistan --niche dentist,gym\n'
            '  python run.py --city "Dubai, UAE" --niche salon --csv-only\n'
        ),
    )
    p.add_argument("--city", required=True,
                   help='e.g. "Karachi, Pakistan", or Karachi,Lahore with --country')
    p.add_argument("--country", default="", help="appended to each city that does not name one")
    p.add_argument("--niche", required=True, help="e.g. dentist, or dentist,gym,salon")
    p.add_argument("--target", type=int, default=1000,
                   help="stop at N leads per client type per city (default 1000)")
    p.add_argument("--queries", default="", help="comma-separated extra search phrasings")
    p.add_argument("--tile-km", type=float, default=3.0, help="grid tile size (default 3.0)")
    p.add_argument("--max-tiles", type=int, default=400, help="cap on grid points")

    p.add_argument("--no-email", action="store_true", help="skip email enrichment")
    p.add_argument("--email-limit", type=int, default=50,
                   help="max email lookups per run (~75s each, low yield; "
                        "most-reviewed leads first). Use --no-email to skip")
    p.add_argument("--no-phone-lookup", action="store_true",
                   help="do not open listings to recover missing phones")
    p.add_argument("--phone-lookup-cap", type=int, default=250,
                   help="max detail pages to open per run")

    p.add_argument("--no-headless", action="store_true",
                   help="show the browser (needed to solve a CAPTCHA)")
    p.add_argument("--min-delay", type=float, default=1.8)
    p.add_argument("--max-delay", type=float, default=4.0)

    p.add_argument("--sheet", default="", help="Google Sheet name (default from .env)")
    p.add_argument("--sheet-key", default="", help="write into an existing sheet by key or URL")
    p.add_argument("--share-with", default="", help="your Google account, for a new sheet")
    p.add_argument("--csv-only", action="store_true", help="skip Sheets, write CSV only")
    p.add_argument("--csv", default="", help="CSV output path (single city + niche only)")
    p.add_argument("--out-dir", default="out", help="where CSVs are written")
    p.add_argument("--db", default="data/leads.db", help="SQLite store path")
    p.add_argument("--fresh", action="store_true", help="ignore previous progress")
    return p.parse_args()


def build_config(args: argparse.Namespace) -> RunConfig:
    cities = split_list(args.city)
    country = args.country.strip()
    if country:
        cities = [
            city if country.lower() in city.lower() else f"{city}, {country}"
            for city in cities
        ]
    return RunConfig(
        niches=split_list(args.niche),
        cities=cities,
        country=country,
        target=args.target,
        extra_queries=split_list(args.queries),
        tile_km=args.tile_km,
        max_tiles=args.max_tiles,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        headless=not args.no_headless,
        phone_lookup=not args.no_phone_lookup,
        phone_cap=args.phone_lookup_cap,
        find_email=not args.no_email,
        email_limit=args.email_limit,
        push_to_sheets=not args.csv_only,
        sheet_key=args.sheet_key or os.getenv("GOOGLE_SHEET_KEY", ""),
        sheet_name=args.sheet or os.getenv("GOOGLE_SHEET_NAME", "Maps Leads"),
        share_with=args.share_with or os.getenv("GOOGLE_SHARE_WITH", ""),
        write_csv=True,
        out_dir=args.out_dir,
        db=args.db,
        fresh=args.fresh,
    )


def summary(config: RunConfig, results) -> None:
    table = Table(title="Leads with no website", header_style="bold")
    table.add_column("Client type")
    table.add_column("City")
    table.add_column("Leads", justify="right")
    table.add_column("Phone", justify="right")
    table.add_column("Email", justify="right")
    table.add_column("To sheet", justify="right")
    for r in results:
        table.add_row(
            tab_title(r.niche), r.city, str(r.found), str(r.with_phone),
            str(r.with_email), str(r.pushed) if config.push_to_sheets else "-",
        )
    console.print()
    console.print(table)

    short = [r for r in results if r.found < config.target]
    if short:
        console.print(
            f"\n[dim]Some lists came up short of {config.target}. The city may "
            "simply not have that many. Try --tile-km 2 for a finer grid, or add "
            "phrasings with --queries.[/dim]"
        )


def main() -> int:
    load_dotenv()
    args = parse_args()
    config = build_config(args)

    tasks = config.tasks()
    if not tasks:
        console.print("[red]Give at least one --city and one --niche.[/red]")
        return 1

    console.rule("[bold]Google Maps lead scraper")
    console.print(f"Lists   : {len(tasks)} ({', '.join(f'{n} in {c}' for n, c in tasks)})")
    console.print(f"Target  : {config.target} leads per list")
    console.print(
        f"Output  : CSV in {config.out_dir}/"
        + ("" if config.push_to_sheets else " (Sheets skipped)")
    )
    console.print()

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )
    bars: dict[str, int] = {}

    def emit(event: str, **data) -> None:
        if event == "log":
            console.print(data["message"], style=_STYLES.get(data["level"], ""))
        elif event == "task_started":
            bars.clear()
            console.rule(
                f"[bold]{data['niche']} in {data['city']} "
                f"[dim]({data['index']}/{data['total']}, tab: {data['tab']})"
            )
        elif event == "progress":
            phase = data.get("phase", "")
            label = {"scrape": "Searching", "phones": "Phones", "emails": "Emails"}.get(
                phase, phase
            )
            if phase not in bars:
                bars[phase] = progress.add_task(label, total=data.get("total") or 1)
            progress.update(
                bars[phase],
                completed=data.get("done", 0),
                total=data.get("total") or 1,
                description=f"{label}: {data.get('label', '')[:60]}",
            )
        elif event == "blocked":
            console.print(f"\n[bold red]{data['message']}[/bold red]")
        elif event == "sheet":
            console.print(f"[green]Sheet:[/green] {data['url']}")

    runner = Runner(config, emit=emit)
    try:
        import asyncio

        with progress:
            asyncio.run(runner.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Progress is saved; re-run to resume.[/yellow]")
        return 130
    finally:
        runner.close()

    # --csv is a convenience for the single-list case; multi-list runs get one
    # file per list in --out-dir either way.
    if args.csv and len(tasks) == 1:
        niche, city = tasks[0]
        with LeadStore(config.db) as store:
            rows = store.export_csv(args.csv, city, niche)
        console.print(f"[green]CSV written:[/green] {Path(args.csv).resolve()} ({rows} rows)")

    summary(config, runner.results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
