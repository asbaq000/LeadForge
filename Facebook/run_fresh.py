#!/usr/bin/env python3
"""
run_fresh.py

FRESH full test run: every configured group, full available 2-day history,
NO 20-lead cap (this run only -- the cap is overridden in memory, the
MAX_LEADS_PER_RUN config value on disk is untouched).

"Fresh" means the persistent dedup stores are set aside first, so leads
already captured in earlier runs are collected again rather than suppressed.
They are backed up, not deleted. Dedup still applies WITHIN this run:
  - same post URL is never collected twice
  - the same person's lead cross-posted to several groups is kept once

Everything else is unchanged: posts only (Stories ignored), genuine
video-editing buyers only, freelancers/service providers excluded, all
existing sheet fields, niche + budget when present.

Writes every qualifying lead to a CSV, and also attempts the Google Sheet
(which currently fails -- the service account was deleted; the CSV is the
reliable output until that is restored).
"""

import csv
import importlib.util
import json
import os
import shutil
import time
from datetime import datetime, timedelta

spec = importlib.util.spec_from_file_location("collector", "fb_group_url_collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

mspec = importlib.util.spec_from_file_location("multi", "scrape_multi_groups.py")
multi = importlib.util.module_from_spec(mspec)
mspec.loader.exec_module(multi)

log = collector.log
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# --- this run only: no lead cap -------------------------------------------
ORIGINAL_CAP = collector.MAX_LEADS_PER_RUN
collector.MAX_LEADS_PER_RUN = 10 ** 9
log.info("FRESH RUN: lead cap disabled for this run (config value on disk stays %d).", ORIGINAL_CAP)

# --- fresh start: set aside the persistent dedup stores --------------------
for path in (collector.SEEN_FILE, collector.LEAD_SIGNATURE_FILE, collector.DAILY_QUOTA_FILE):
    if os.path.exists(path):
        backup = f"{path}.before_fresh_{STAMP}.bak"
        shutil.copy2(path, backup)
        os.remove(path)
        log.info("FRESH RUN: %s set aside as %s", path, backup)

driver = collector.attach_to_user_chrome()
collector.ensure_page_visible(driver)

seen_urls = set()          # within-run URL dedup
lead_signatures = []       # within-run cross-group person/lead dedup
cutoff = datetime.now() - timedelta(days=collector.DAYS_BACK)
log.info("FRESH RUN: cutoff %s (last %d days), %d groups.",
         cutoff, collector.DAYS_BACK, len(multi.GROUP_URLS))

collected = []
stats = dict(processed=0, old=0, dup=0, xdup=0, invalid=0, ts=0, nq=0,
             inaccessible=0, errored=0, scanned=0)
sheet_written = 0
sheet_error = None

for i, (name, url) in enumerate(multi.GROUP_URLS, start=1):
    log.info("[%d/%d] Visiting: %s", i, len(multi.GROUP_URLS), name)
    try:
        driver.get(url)
        collector.ensure_page_visible(driver)
        time.sleep(multi.GROUP_LOAD_WAIT_SECONDS)

        if not collector.detect_group_page(driver) or multi.looks_like_join_gate(driver):
            log.warning("[%d/%d] No post content rendered; skipping: %s",
                        i, len(multi.GROUP_URLS), name)
            stats["inaccessible"] += 1
            continue

        r = collector.process_posts(driver, cutoff, seen_urls,
                                    max_new_leads=None,          # no cap
                                    known_signatures=lead_signatures)
        stats["scanned"] += 1
        stats["processed"] += r.posts_processed
        stats["old"] += r.old_posts_skipped
        stats["dup"] += r.duplicates_skipped
        stats["xdup"] += r.cross_group_duplicates_skipped
        stats["invalid"] += r.invalid_urls_skipped
        stats["ts"] += r.unparseable_timestamps_skipped
        stats["nq"] += r.not_qualifying_lead_skipped

        log.info("[%d/%d] %s -- qualifying %d (running total %d), old %d, dup %d, "
                 "cross-dup %d, not-qualifying %d",
                 i, len(multi.GROUP_URLS), name, len(r.collected),
                 len(collected) + len(r.collected), r.old_posts_skipped,
                 r.duplicates_skipped, r.cross_group_duplicates_skipped,
                 r.not_qualifying_lead_skipped)

        collected.extend(r.collected)
        seen_urls |= r.seen_this_run
        lead_signatures.extend(r.signatures_this_run)

    except Exception as exc:
        log.error("[%d/%d] Group failed, continuing: %s", i, len(multi.GROUP_URLS), exc)
        stats["errored"] += 1
        continue

# --- CSV (the reliable output) --------------------------------------------
csv_path = f"LEADS_FRESH_{STAMP}.csv"
with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(collector.SHEET_HEADERS)
    for n, p in enumerate(collected, start=1):
        w.writerow([p.topic or "", p.niche or "", p.url, p.budget or "", n,
                    p.post_date.strftime("%Y-%m-%d %H:%M:%S"), p.phone_number or ""])

# --- Google Sheet (attempted; currently blocked by the deleted account) ----
if collected:
    try:
        ws = collector.get_sheets_worksheet()
        existing = collector.get_existing_sheet_urls(ws)
        to_write = [p for p in collected if p.url not in existing]
        sheet_written = collector.append_posts_to_sheet(ws, to_write)
        log.info("Wrote %d lead(s) to the Google Sheet.", sheet_written)
    except Exception as exc:
        sheet_error = str(exc)[:200]
        log.error("Google Sheets write failed: %s", sheet_error)

# --- persist dedup state so the NEXT run doesn't repeat these -------------
collector.save_seen_urls(seen_urls)
collector.save_lead_signatures(lead_signatures)

print("\n=== FRESH RUN RESULT ===")
print("groups configured   :", len(multi.GROUP_URLS))
print("groups scanned      :", stats["scanned"])
print("groups inaccessible :", stats["inaccessible"])
print("groups errored      :", stats["errored"])
print("posts evaluated     :", stats["processed"])
print("QUALIFYING LEADS    :", len(collected))
print("duplicate URLs       :", stats["dup"])
print("cross-group dups     :", stats["xdup"])
print("old posts skipped    :", stats["old"])
print("not qualifying       :", stats["nq"])
print("invalid/no permalink :", stats["invalid"])
print("timestamp unreadable :", stats["ts"])
print("leads -> Google Sheet:", sheet_written, ("(FAILED: " + sheet_error + ")") if sheet_error else "")
print("CSV                 :", csv_path)
print("=== DONE ===")
