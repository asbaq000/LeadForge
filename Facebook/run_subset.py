#!/usr/bin/env python3
"""
run_subset.py [N]

Runs the normal pipeline over only the LAST N groups from
scrape_multi_groups.GROUP_URLS (default 11), and writes qualifying leads to a
timestamped CSV using the exact Google Sheet column order.

Same rules as the daily runner -- 2-day window, buyers only, Stories ignored,
URL + cross-group dedup, MAX_LEADS_PER_RUN cap. The CSV exists because the
Google service account is currently unusable; nothing about lead selection
changes.
"""

import csv
import importlib.util
import sys
import time
from datetime import datetime, timedelta

spec = importlib.util.spec_from_file_location("collector", "fb_group_url_collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

mspec = importlib.util.spec_from_file_location("multi", "scrape_multi_groups.py")
multi = importlib.util.module_from_spec(mspec)
mspec.loader.exec_module(multi)

log = collector.log
# Usage:
#   run_subset.py 11            -> the LAST 11 groups
#   run_subset.py 1 20          -> groups 1..20 inclusive (1-based)
if len(sys.argv) > 2:
    start, end = int(sys.argv[1]), int(sys.argv[2])
    GROUPS = multi.GROUP_URLS[start - 1:end]
    label = f"groups {start}..{end}"
else:
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 11
    GROUPS = multi.GROUP_URLS[-N:]
    label = f"last {N} groups"

log.info("Subset run: %s (%d of %d).", label, len(GROUPS), len(multi.GROUP_URLS))

driver = collector.attach_to_user_chrome()
collector.ensure_page_visible(driver)

seen_urls = collector.load_seen_urls()
lead_signatures = collector.load_lead_signatures()
cutoff = datetime.now() - timedelta(days=collector.DAYS_BACK)

collected = []
stats = dict(processed=0, old=0, dup=0, xdup=0, invalid=0, ts=0, nq=0,
             inaccessible=0, errored=0, groups_scanned=0)

for i, (name, url) in enumerate(GROUPS, start=1):
    remaining = collector.MAX_LEADS_PER_RUN - len(collected)
    if remaining <= 0:
        log.info("Hit the %d-lead cap -- stopping before group %d/%d.",
                 collector.MAX_LEADS_PER_RUN, i, len(GROUPS))
        break

    log.info("[%d/%d] Visiting: %s", i, len(GROUPS), name)
    try:
        driver.get(url)
        collector.ensure_page_visible(driver)
        time.sleep(multi.GROUP_LOAD_WAIT_SECONDS)

        if not collector.detect_group_page(driver) or multi.looks_like_join_gate(driver):
            log.warning("[%d/%d] No post content rendered; skipping: %s", i, len(GROUPS), name)
            stats["inaccessible"] += 1
            continue

        r = collector.process_posts(driver, cutoff, seen_urls,
                                    max_new_leads=remaining,
                                    known_signatures=lead_signatures)
        stats["groups_scanned"] += 1
        stats["processed"] += r.posts_processed
        stats["old"] += r.old_posts_skipped
        stats["dup"] += r.duplicates_skipped
        stats["xdup"] += r.cross_group_duplicates_skipped
        stats["invalid"] += r.invalid_urls_skipped
        stats["ts"] += r.unparseable_timestamps_skipped
        stats["nq"] += r.not_qualifying_lead_skipped

        log.info("[%d/%d] %s -- qualifying %d, old %d, dup %d, cross-dup %d, not-qualifying %d",
                 i, len(GROUPS), name, len(r.collected), r.old_posts_skipped,
                 r.duplicates_skipped, r.cross_group_duplicates_skipped,
                 r.not_qualifying_lead_skipped)

        collected.extend(r.collected)
        seen_urls |= r.seen_this_run
        collector.save_seen_urls(seen_urls)
        if r.signatures_this_run:
            lead_signatures.extend(r.signatures_this_run)
            collector.save_lead_signatures(lead_signatures)

    except Exception as exc:
        log.error("[%d/%d] Group failed, continuing: %s", i, len(GROUPS), exc)
        stats["errored"] += 1
        continue

# Deliberately NOT calling driver.quit(): this session belongs to the user's
# own debug Chrome window. Quitting it closes their logged-in browser, and the
# next run then has nothing to attach to.

out = f"leads_subset_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
with open(out, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow(collector.SHEET_HEADERS)
    for n, p in enumerate(collected, start=1):
        w.writerow([p.topic or "", p.niche or "", p.url, p.budget or "", n,
                    p.post_date.strftime("%Y-%m-%d %H:%M:%S"), p.phone_number or ""])

print("\n=== SUBSET RUN RESULT ===")
print("groups attempted   :", len(GROUPS))
print("groups scanned     :", stats["groups_scanned"])
print("groups inaccessible:", stats["inaccessible"])
print("groups errored     :", stats["errored"])
print("qualifying leads   :", len(collected))
print("old skipped        :", stats["old"])
print("url duplicates     :", stats["dup"])
print("cross-group dups   :", stats["xdup"])
print("not qualifying     :", stats["nq"])
print("invalid urls       :", stats["invalid"])
print("ts unparseable     :", stats["ts"])
print("CSV                :", out)
print("=== DONE ===")
