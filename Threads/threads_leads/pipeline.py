"""Orchestration: collect -> qualify -> dedupe -> enrich -> write to Sheets."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone

from .collector import ThreadsBlocked, ThreadsCollector, TransientNetworkError
from .config import Settings
from .enrich import (
    extract_emails,
    extract_links,
    first_name_from,
    suggest_opener,
)
from .parser import iso_posted_at, post_age_hours
from .safety import (
    BudgetExhausted,
    CooldownActive,
    InstanceLock,
    OutsideActiveHours,
    SafetyGovernor,
)
from .scorer import LeadScorer
from .sheets import SheetWriter, append_csv_backup
from .store import Store

BASE = "https://www.threads.com"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rotated_sources(settings: Settings, run_count: int) -> list[tuple[str, str]]:
    """Queries first, then hashtags - rotated per run so later entries get their turn."""
    sources = [("query", q) for q in settings.queries]
    sources += [("hashtag", h) for h in settings.hashtags]
    if not sources:
        return []
    offset = (run_count * settings.max_queries_per_run) % len(sources)
    return sources[offset:] + sources[:offset]


class Pipeline:
    def __init__(self, settings: Settings, log=print):
        self.s = settings
        self.log = log
        self.store = Store(settings.db_path)
        self.scorer = LeadScorer(settings)
        self.guard = SafetyGovernor(settings, self.store, log=log)

    # ------------------------------------------------------------------

    def run(self, limit: int | None = None, dry_run: bool = False, sync_sheet: bool = True) -> dict:
        started = _now_iso()

        # Refuse to start if we are cooling down, it is the middle of the night,
        # or the daily action budget is already spent.
        try:
            self.guard.preflight()
        except (CooldownActive, OutsideActiveHours, BudgetExhausted) as exc:
            self.log(f"[safety] run skipped: {exc}")
            return {"seen": 0, "qualified": 0, "exported": 0, "budget": 0, "note": f"skipped: {exc}"}

        writer = None
        if not dry_run and sync_sheet:
            writer = SheetWriter(self.s, log=self.log)
            seeded = self.store.import_existing_ids(writer.existing_post_ids())
            if seeded:
                self.log(f"  synced {seeded} existing post IDs from the sheet")

        remaining_today = self.store.remaining_today(self.s.daily_target)
        budget = limit if limit is not None else min(self.s.per_run_cap, remaining_today)

        if budget <= 0:
            self.log(
                f"Daily target already met ({self.store.exported_today()}/"
                f"{self.s.daily_target}). Nothing to do."
            )
            return {"seen": 0, "qualified": 0, "exported": 0, "budget": 0}

        self.log(
            f"Budget this run: {budget} leads "
            f"(today {self.store.exported_today()}/{self.s.daily_target})"
        )

        run_count = self.store.conn.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"]
        sources = _rotated_sources(self.s, run_count)

        seen = 0
        candidates: list[dict] = []
        note = "ok"

        lock = InstanceLock(self.s.lock_path, enabled=self.s.single_instance)
        selected: list[dict] = []
        tripped = False
        net_failures = 0

        try:
            lock.acquire()
            with ThreadsCollector(self.s, log=self.log) as collector:
                collector.warmup()

                for idx, (kind, term) in enumerate(sources):
                    if idx >= self.s.max_queries_per_run:
                        self.log("  reached max_queries_per_run")
                        break
                    if not self.guard.allow_search():
                        note = "stopped on safety budget"
                        break
                    if len(candidates) >= budget * 2:
                        # Plenty banked; stop early rather than hammer the site.
                        self.log("  enough candidates gathered, stopping collection")
                        break

                    try:
                        posts = self._collect_with_retry(collector, kind, term)
                        self.guard.record("search")
                        net_failures = 0
                    except TransientNetworkError as exc:
                        # Network problem, not a block. Skip this query and carry on.
                        net_failures += 1
                        self.log(f"    ! network error ({net_failures}/3): {exc}")
                        if net_failures >= 3:
                            note = "aborted: network unavailable"
                            self.log("  !! three consecutive network failures - stopping")
                            break
                        time.sleep(random.uniform(5, 12))
                        continue
                    except ThreadsBlocked as exc:
                        # First sign of trouble: stop and rest. Retrying here is
                        # precisely what turns a soft limit into a hard ban.
                        self.guard.trip(str(exc))
                        tripped = True
                        note = f"blocked: {exc}"
                        self.log(f"  !! {exc}")
                        break

                    seen += len(posts)
                    fresh = self._qualify(posts)
                    candidates.extend(fresh)
                    self.log(f"    {len(posts)} posts -> {len(fresh)} qualified")

                    collector.browse_feed_briefly()
                    lo, hi = self.s.query_pause_ms
                    time.sleep(random.uniform(lo, hi) / 1000.0)

                candidates.sort(key=lambda c: c["score"], reverse=True)
                selected = self._dedupe_authors(candidates)[:budget]

                if self.s.enrich_profiles and selected and not tripped:
                    self._enrich(collector, selected)

        except FileNotFoundError:
            lock.release()
            raise
        except Exception as exc:  # keep partial results rather than losing the run
            note = f"error: {type(exc).__name__}: {exc}"
            self.log(f"  !! collection aborted: {note}")
            candidates.sort(key=lambda c: c["score"], reverse=True)
            selected = self._dedupe_authors(candidates)[:budget]
        finally:
            lock.release()

        if not tripped and note == "ok":
            self.guard.note_clean_run()

        leads = [self._finalize(c) for c in selected]

        exported = 0
        if dry_run:
            self._print_preview(leads)
        elif leads:
            append_csv_backup(self.s.csv_backup, leads)
            if writer:
                exported = writer.append(leads)
                for lead in leads:
                    self.store.mark_exported(lead["post_id"], lead["username"])
                self.log(f"  wrote {exported} rows to Google Sheets")
            else:
                exported = len(leads)
                for lead in leads:
                    self.store.mark_exported(lead["post_id"], lead["username"])
                self.log(f"  wrote {exported} rows to {self.s.csv_backup.name} (sheet skipped)")

        self.store.log_run(started, seen, len(candidates), exported, note)

        summary = {
            "seen": seen,
            "qualified": len(candidates),
            "exported": exported,
            "budget": budget,
            "today": self.store.exported_today(),
            "target": self.s.daily_target,
            "note": note,
        }
        self.log(
            f"Done: saw {seen} posts, {len(candidates)} qualified, {exported} exported. "
            f"Today {summary['today']}/{self.s.daily_target}."
        )
        return summary

    # ------------------------------------------------------------------

    def _collect_with_retry(self, collector, kind: str, term: str) -> list[dict]:
        """One retry on a transient network fault before giving up on this query."""
        for attempt in (1, 2):
            try:
                return (
                    collector.search(term) if kind == "query" else collector.hashtag(term)
                )
            except TransientNetworkError:
                if attempt == 2:
                    raise
                self.log("    ! page load failed, retrying once in a few seconds")
                time.sleep(random.uniform(4, 9))
        return []

    def _qualify(self, posts: list[dict]) -> list[dict]:
        out = []
        for post in posts:
            pid = post["post_id"]
            if self.store.seen_post(pid):
                continue

            age = post_age_hours(post.get("taken_at", 0))
            if age is not None and age > self.s.max_post_age_hours:
                self.store.mark_seen(pid, post["username"], 0)
                continue

            verdict = self.scorer.score(post["text"])
            self.store.mark_seen(pid, post["username"], verdict.score)

            if verdict.level == "REJECT":
                continue
            if self.store.author_in_cooldown(post["username"], self.s.author_cooldown_days):
                continue

            self.store.archive_raw(pid, post)
            post.update(
                score=verdict.score,
                level=verdict.level,
                signals=verdict.signals,
                matched_terms=verdict.matched_terms,
                topics=verdict.topics,
            )
            out.append(post)
        return out

    @staticmethod
    def _dedupe_authors(candidates: list[dict]) -> list[dict]:
        """One lead per person per run - keeps the sheet clean for outreach."""
        seen_authors: set[str] = set()
        out = []
        for c in candidates:
            key = c["username"].lower()
            if key in seen_authors:
                continue
            seen_authors.add(key)
            out.append(c)
        return out

    def _enrich(self, collector: ThreadsCollector, leads: list[dict]) -> None:
        targets = [l for l in leads if l["score"] >= self.s.enrich_min_score and not l.get("followers")]
        if not targets:
            return
        self.log(f"  enriching {len(targets)} profiles...")
        for lead in targets:
            if not self.guard.allow_profile_visit():
                break
            info = collector.profile(lead["username"])
            self.guard.record("profile")
            lead["followers"] = info.get("followers") or lead.get("followers", 0)
            lead["bio"] = info.get("bio") or lead.get("bio", "")
            if info.get("external_url"):
                lead["external_url"] = info["external_url"]

    def _finalize(self, c: dict) -> dict:
        blob_sources = (c.get("text", ""), c.get("bio", ""), c.get("link_attachment", ""))
        emails = extract_emails(*blob_sources)
        links = extract_links(*blob_sources)
        if c.get("external_url"):
            links = [c["external_url"]] + [l for l in links if l != c["external_url"]]

        first = first_name_from(c.get("full_name", ""), c.get("username", ""))

        return {
            **c,
            "captured_at": _now_iso(),
            "posted_at": iso_posted_at(c.get("taken_at", 0)),
            "profile_url": f"{BASE}/@{c['username']}",
            "emails": emails,
            "links": links,
            "opener": suggest_opener(c.get("text", ""), c.get("topics", []), first),
        }

    def _print_preview(self, leads: list[dict]) -> None:
        if not leads:
            self.log("\n(dry run) No qualifying leads found this pass.")
            return
        self.log(f"\n(dry run) {len(leads)} leads that WOULD be written:\n")
        for i, l in enumerate(leads, 1):
            text = l["text"].replace("\n", " ")
            self.log(f"{i:>3}. [{l['score']:>3}] {l['level']:<5} @{l['username']}")
            self.log(f"     {text[:150]}")
            self.log(f"     topics={l['topics']} signals={l['signals']}")
            self.log(f"     {l['permalink']}\n")

    def close(self) -> None:
        self.store.close()
