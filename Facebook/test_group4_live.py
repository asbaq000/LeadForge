#!/usr/bin/env python3
"""
test_group4_live.py [group_url]

End-to-end live test of the REAL pipeline against a single group: runs
process_posts exactly as the daily runner does, prints every collected
lead, and writes qualifying ones to the existing Google Sheet (respecting
dedup and the MAX_LEADS_PER_RUN cap).
"""

import importlib.util
import sys
import time
from datetime import datetime, timedelta

spec = importlib.util.spec_from_file_location("collector", "fb_group_url_collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

group_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.facebook.com/share/g/17sYQhzdR4/"

driver = collector.attach_to_user_chrome()
driver.get(group_url)
time.sleep(3)

print("URL:", driver.current_url)
print("Is group page:", collector.detect_group_page(driver))

cutoff = datetime.now() - timedelta(days=collector.DAYS_BACK)
print("Cutoff:", cutoff)

seen = collector.load_seen_urls()
print(f"Loaded {len(seen)} previously-seen URLs.")

result = collector.process_posts(driver, cutoff, seen, max_new_leads=collector.MAX_LEADS_PER_RUN)

print()
print("=== RESULT ===")
print("posts_processed        :", result.posts_processed)
print("collected (qualifying) :", len(result.collected))
print("old_posts_skipped      :", result.old_posts_skipped)
print("duplicates_skipped     :", result.duplicates_skipped)
print("invalid_urls_skipped   :", result.invalid_urls_skipped)
print("ts_unparseable_skipped :", result.unparseable_timestamps_skipped)
print("not_qualifying_skipped :", result.not_qualifying_lead_skipped)

print("\n=== COLLECTED LEADS ===")
for p in result.collected:
    print(f"  URL   : {p.url}")
    print(f"  Date  : {p.post_date}")
    print(f"  Topic : {p.topic!r}")
    print(f"  Niche : {p.niche!r}")
    print(f"  Budget: {p.budget!r}")
    print(f"  Phone : {p.phone_number!r}")
    print()

if not result.collected:
    print("No qualifying leads collected -- nothing to write.")
    raise SystemExit(0)

print("=== WRITING TO GOOGLE SHEET ===")
ws = collector.get_sheets_worksheet()
existing = collector.get_existing_sheet_urls(ws)
print(f"Sheet currently has {len(existing)} URLs.")

to_write = [p for p in result.collected if p.url not in existing]
print(f"After sheet-level dedup, writing {len(to_write)} row(s).")

written = collector.append_posts_to_sheet(ws, to_write)
print("Rows written:", written)

collector.save_seen_urls(seen | result.seen_this_run)
print("seen_fb_urls.json updated.")

after = collector.get_existing_sheet_urls(ws)
print(f"Sheet now has {len(after)} URLs.")
for p in to_write:
    print(f"  verified in sheet: {p.url in after}  {p.url}")
print("=== DONE ===")
