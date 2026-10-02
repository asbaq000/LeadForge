"""The run engine: turn a request for leads into rows in a spreadsheet.

One place holds the whole sequence — geocode, scrape, recover phones, hunt
emails, write CSV, push to Sheets — so the command line and the web UI behave
identically. Callers get progress through an `emit` callback and can stop a run
through a `should_stop` callback; neither needs to know anything about
browsers, tiles, or worksheets.

A request can name several client types and several cities. That becomes one
task per (client type, city) pair, run in order, each one landing on its own
tab in the spreadsheet.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import keywords
from .enrich import enrich_leads
from .geo import CityBox, GeocodeError, build_grid, geocode_city, zoom_for_tile
from .models import Lead
from .scraper import MapsScraper
from .sheets import SheetsError, SheetWriter, tab_title
from .store import LeadStore

# How long we will sit on a CAPTCHA waiting for a person to clear it, when the
# browser is visible and they can actually do so.
_CAPTCHA_WAIT_SECONDS = 600


class Cancelled(RuntimeError):
    """The caller asked the run to stop."""


@dataclass
class RunConfig:
    """Everything one run needs. The UI fills this in from its form."""

    niches: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    country: str = ""
    target: int = 100                 # leads per client type, per city
    extra_queries: list[str] = field(default_factory=list)

    tile_km: float = 3.0
    max_tiles: int = 400
    min_delay: float = 1.8
    max_delay: float = 4.0
    headless: bool = True

    phone_lookup: bool = True
    phone_cap: int = 250
    find_email: bool = False
    email_limit: int = 25

    push_to_sheets: bool = True
    sheet_key: str = ""
    sheet_name: str = "Maps Leads"
    share_with: str = ""
    credentials_path: str = ""

    write_csv: bool = True
    out_dir: str = "out"
    db: str = "data/leads.db"
    fresh: bool = False               # ignore which tiles were already done

    def tasks(self) -> list[tuple[str, str]]:
        """(client type, city) pairs, in the order they will be worked."""
        return [
            (niche, city)
            for city in self.cities
            for niche in self.niches
            if niche.strip() and city.strip()
        ]


@dataclass
class TaskResult:
    niche: str
    city: str
    found: int = 0
    new: int = 0
    with_phone: int = 0
    with_email: int = 0
    pushed: int = 0
    tab: str = ""
    csv_path: str = ""
    error: str = ""


class Runner:
    """Runs one request end to end, reporting as it goes."""

    def __init__(self, config: RunConfig, emit=None, should_stop=None):
        self.config = config
        self._emit = emit or (lambda event, **data: None)
        self._should_stop = should_stop or (lambda: False)
        self.store = LeadStore(config.db)
        self.results: list[TaskResult] = []
        self._boxes: dict[str, CityBox] = {}
        self._writer: SheetWriter | None = None
        self._scraper: MapsScraper | None = None
        self._sheet_url = ""

    # --- plumbing ----------------------------------------------------------

    def emit(self, event: str, **data) -> None:
        self._emit(event, **data)

    def log(self, message: str, level: str = "info") -> None:
        self.emit("log", level=level, message=message)

    def check_stop(self) -> None:
        if self._should_stop():
            raise Cancelled()

    def close(self) -> None:
        self.store.close()

    # --- CAPTCHA -----------------------------------------------------------

    async def _on_block(self, scraper: MapsScraper) -> None:
        """Google served a CAPTCHA. We never solve one; a person has to."""
        if scraper.headless:
            self.emit(
                "blocked",
                solvable=False,
                message=(
                    "Google is showing a CAPTCHA. Everything found so far is "
                    "saved. Turn on 'Show the browser window' and start again "
                    "to solve it by hand, or wait a few hours."
                ),
            )
            raise Cancelled()

        self.emit(
            "blocked",
            solvable=True,
            message=(
                "Google is showing a CAPTCHA. Solve it in the browser window "
                "that is open — the run continues by itself once you do."
            ),
        )
        deadline = time.monotonic() + _CAPTCHA_WAIT_SECONDS
        while time.monotonic() < deadline:
            self.check_stop()
            await asyncio.sleep(5)
            if not await scraper._check_blocked():
                self.log("CAPTCHA cleared, carrying on.", "good")
                self.emit("unblocked")
                return
        self.log("CAPTCHA was not cleared in time; stopping.", "warn")
        raise Cancelled()

    # --- phases ------------------------------------------------------------

    def _geocode(self, city: str) -> CityBox:
        if city not in self._boxes:
            self._boxes[city] = geocode_city(city)
        return self._boxes[city]

    async def _scrape(self, niche: str, city: str, box: CityBox) -> int:
        cfg = self.config
        grid = build_grid(box, tile_km=cfg.tile_km, max_tiles=cfg.max_tiles)
        zoom = zoom_for_tile(cfg.tile_km)
        queries = keywords.expand(niche, extra=cfg.extra_queries)

        # Interleave queries across tiles: every tile gets phrasing #1 before
        # any tile gets phrasing #2. Stopping early then leaves citywide
        # coverage rather than one exhaustively-mined neighbourhood.
        plan = [(q, lat, lng) for q in queries for (lat, lng) in grid]

        self.log(
            f"{box.width_km:.0f} x {box.height_km:.0f} km, {len(grid)} tiles, "
            f"{len(queries)} search phrasings: {', '.join(queries[:6])}"
            + ("..." if len(queries) > 6 else "")
        )

        found_before = self.store.count(city, niche)
        new_total = 0
        done = 0

        if found_before >= cfg.target:
            self.log(
                f"Already have {found_before} {niche} lead(s) in {city}, which "
                f"meets the target of {cfg.target}. Raise the target, or tick "
                "'Fresh sweep', to look for more.",
                "good",
            )
            return 0

        for query, lat, lng in plan:
            self.check_stop()
            found = self.store.count(city, niche)
            if found >= cfg.target:
                self.log(f"Target reached: {found} leads.", "good")
                break

            done += 1
            if not cfg.fresh and self.store.is_tile_done(city, query, lat, lng):
                self.emit(
                    "progress",
                    phase="scrape",
                    done=done,
                    total=len(plan),
                    found=found,
                    target=cfg.target,
                    label=f"{query} — already searched",
                )
                continue

            self.emit(
                "progress",
                phase="scrape",
                done=done,
                total=len(plan),
                found=found,
                target=cfg.target,
                label=f"{query} @ {lat:.3f}, {lng:.3f}",
            )

            try:
                leads = await self._scraper.search_tile(
                    query, lat, lng, zoom, city, only_without_website=True
                )
            except Cancelled:
                raise
            except Exception as exc:
                self.log(f"A search failed ({exc}); moving on.", "warn")
                continue

            for lead in leads:
                lead.niche = niche
                lead.country = self.config.country

            added = self.store.add_returning(leads)
            self.store.mark_tile_done(city, query, lat, lng, len(added))
            new_total += len(added)
            # The counter should move the moment a tile lands, not at the top
            # of the next one.
            self.emit(
                "stats",
                found=self.store.count(city, niche),
                target=cfg.target,
                new=new_total,
            )
            for lead in added:
                self.emit(
                    "lead",
                    name=lead.name,
                    phone=lead.phone,
                    category=lead.category,
                    address=lead.address,
                    city=lead.city,
                    niche=niche,
                    rating=lead.rating,
                    reviews=lead.reviews,
                    maps_url=lead.maps_url,
                )

        stats = self._scraper.stats
        self.log(
            f"Searched {stats.searches} tiles, saw {stats.cards_seen} listings, "
            f"{new_total} new leads without a website."
        )
        unconfirmed = self.store.count_unverified(city, niche)
        if unconfirmed and cfg.phone_lookup:
            self.log(
                f"{unconfirmed} of them came from searches that never showed a "
                "website button, so each listing gets opened to check."
            )
        elif unconfirmed:
            self.log(
                f"{unconfirmed} lead(s) are unconfirmed: Google showed no "
                "website buttons in these results, and phone lookup is off, so "
                "some may in fact have a website.",
                "warn",
            )
        return self.store.count(city, niche) - found_before

    async def _recover_phones(self, niche: str, city: str) -> None:
        """Open listings to fill in phones and settle the no-website question."""
        cfg = self.config
        pending = self.store.needs_detail_visit(city, niche)
        if not pending:
            return

        unverified = sum(1 for lead in pending if not lead.verified)
        targets = pending[: cfg.phone_cap]
        self.log(
            f"Opening {len(targets)} listing(s) to recover phone numbers"
            + (
                f" and confirm {min(unverified, len(targets))} of them really "
                "have no website"
                if unverified
                else ""
            )
            + "."
        )
        if unverified > cfg.phone_cap:
            self.log(
                f"{unverified - cfg.phone_cap} lead(s) will stay unconfirmed "
                f"this run — raise the phone lookup cap (now {cfg.phone_cap}) "
                "to check them all.",
                "warn",
            )

        for i, lead in enumerate(targets, 1):
            self.check_stop()
            self.emit(
                "progress",
                phase="phones",
                done=i,
                total=len(targets),
                label=lead.name,
            )
            # The listing page is the authority, so record what it said either
            # way — but only when we actually reached it.
            if await self._scraper._fetch_detail(lead):
                self.store.update_contact(lead)

        dropped = self.store.drop_with_website()
        if dropped:
            self.log(
                f"Dropped {dropped} lead(s) whose listing showed a website "
                "after all.",
                "warn",
            )
        merged = self.store.prune_duplicates()
        if merged:
            self.log(f"Merged {merged} duplicate listing(s).")

    async def _find_emails(self, niche: str, city: str) -> None:
        cfg = self.config
        pending = self.store.needing_enrichment(city, niche, limit=cfg.email_limit)
        if not pending:
            return
        outstanding = len(self.store.needing_enrichment(city, niche))
        self.log(
            f"Looking for emails on {len(pending)} of {outstanding} leads. "
            "These businesses have no website, so most have no email anywhere "
            "— expect a low hit rate."
        )

        done = 0

        def on_result(lead: Lead, email: str, source: str) -> None:
            nonlocal done
            done += 1
            if email:
                self.store.update_email(lead.place_id, email, source)
            else:
                self.store.mark_enriched([lead.place_id])
            self.emit(
                "progress",
                phase="emails",
                done=done,
                total=len(pending),
                label=f"{lead.name}{' — ' + email if email else ''}",
            )

        stats = await enrich_leads(
            pending, headless=cfg.headless, on_result=on_result
        )
        self.log(
            f"Found {stats.found} email(s) from {stats.attempted} lookups "
            f"({stats.rate:.0f}%)."
        )

    def _sheet_writer(self) -> SheetWriter:
        if self._writer is None:
            cfg = self.config
            writer = SheetWriter(
                credentials_path=cfg.credentials_path or None,
                sheet_name=cfg.sheet_name,
                sheet_key=cfg.sheet_key or None,
                share_with=cfg.share_with or None,
            ).connect()
            self._writer = writer
            self._sheet_url = writer.url
            self.emit("sheet", url=writer.url, title=writer.title)
        return self._writer

    def _push(self, niche: str, city: str, result: TaskResult) -> None:
        pending = self.store.unpushed(city, niche)

        # A row in the sheet is permanent — a lead later found to have a
        # website can be dropped from the database but not un-sent. So only
        # confirmed leads go in, and the rest wait for a run that checks them.
        holding = [lead for lead in pending if not lead.verified]
        pending = [lead for lead in pending if lead.verified]
        if holding:
            fix = (
                f"raise the phone lookup cap (now {self.config.phone_cap})"
                if self.config.phone_lookup
                else "switch phone lookup on"
            )
            self.log(
                f"Holding back {len(holding)} lead(s) whose listing has not "
                f"been checked yet — they are in the CSV and the database, and "
                f"go to the sheet once confirmed. To confirm them now, {fix} "
                "and run this list again.",
                "warn",
            )
        if not pending:
            return
        try:
            writer = self._sheet_writer()
            # Re-read the sheet each time: you may have edited it between runs.
            writer.load_index(force=True)
            written = writer.append_leads(pending, niche=niche)
            self.store.mark_pushed([lead.place_id for lead in written])
            result.pushed = len(written)
            skipped = len(pending) - len(written)
            note = f", {skipped} already there" if skipped else ""
            self.log(
                f"Wrote {len(written)} row(s) to the '{result.tab}' tab{note}.",
                "good",
            )
        except SheetsError as exc:
            result.error = str(exc)
            self.log(f"Google Sheets step skipped: {exc}", "warn")
            self.log("The CSV has everything; you can import it by hand.", "warn")
        except Exception as exc:
            result.error = str(exc)
            self.log(f"Could not write to Google Sheets: {exc}", "warn")
            self.log("The CSV has everything; you can import it by hand.", "warn")

    # --- the run -----------------------------------------------------------

    async def run(self) -> list[TaskResult]:
        cfg = self.config
        tasks = cfg.tasks()
        if not tasks:
            self.log("Nothing to do: pick at least one client type and city.", "warn")
            return []

        self.emit(
            "run_started",
            tasks=[{"niche": n, "city": c} for n, c in tasks],
            target=cfg.target,
        )

        self._scraper = MapsScraper(
            headless=cfg.headless,
            min_delay=cfg.min_delay,
            max_delay=cfg.max_delay,
            on_block=self._on_block,
        )

        try:
            async with self._scraper:
                for index, (niche, city) in enumerate(tasks, 1):
                    self.check_stop()
                    result = TaskResult(niche=niche, city=city)
                    self.results.append(result)
                    self.emit(
                        "task_started",
                        niche=niche,
                        city=city,
                        index=index,
                        total=len(tasks),
                        tab=tab_title(niche),
                    )

                    try:
                        box = self._geocode(city)
                    except GeocodeError as exc:
                        result.error = str(exc)
                        self.log(str(exc), "error")
                        self.emit("task_done", **result.__dict__)
                        continue

                    if cfg.fresh:
                        self.store.clear_tiles(city)

                    try:
                        result.new = await self._scrape(niche, city, box)
                        if cfg.phone_lookup:
                            await self._recover_phones(niche, city)
                        if cfg.find_email:
                            await self._find_emails(niche, city)
                    except Cancelled:
                        self._finish_task(niche, city, result)
                        raise

                    self._finish_task(niche, city, result)
        except Cancelled:
            self.log("Stopped. Everything found so far is saved.", "warn")
            self.emit("run_done", cancelled=True, results=self._results_payload())
            return self.results
        finally:
            self._scraper = None

        self.emit("run_done", cancelled=False, results=self._results_payload())
        return self.results

    def _finish_task(self, niche: str, city: str, result: TaskResult) -> None:
        """Write the CSV and push to Sheets for one finished client/city."""
        cfg = self.config
        result.found = self.store.count(city, niche)
        result.with_phone = self.store.count_with_phone(city, niche)
        result.with_email = self.store.count_with_email(city, niche)
        result.tab = tab_title(niche)

        if cfg.write_csv and result.found:
            place = city.split(",")[0].strip().replace(" ", "-")
            safe_niche = niche.strip().replace(" ", "-")
            path = Path(cfg.out_dir) / f"{place}-{safe_niche}.csv"
            self.store.export_csv(path, city, niche)
            result.csv_path = str(path.resolve())

        if cfg.push_to_sheets:
            self._push(niche, city, result)

        self.emit("task_done", **result.__dict__)

    def _results_payload(self) -> list[dict]:
        return [r.__dict__ for r in self.results]


def run_sync(config: RunConfig, emit=None, should_stop=None) -> list[TaskResult]:
    """Run to completion from ordinary synchronous code (the web worker)."""
    runner = Runner(config, emit=emit, should_stop=should_stop)
    try:
        return asyncio.run(runner.run())
    finally:
        runner.close()
