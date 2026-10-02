#!/usr/bin/env python3
# LeadForge · asbaq000
"""
scrape_multi_groups.py

Daily multi-group runner: runs the same extraction/qualification pipeline as
fb_group_url_collector.py across the Facebook Group URLs the user curated in
their "FB Groups" sheet, writing qualifying video-editing leads (buyer +
video-editing context + remote/freelance, per is_qualifying_video_editing_lead)
to the "Lead Scraper" sheet as it goes -- one group at a time, not batched at
the end -- and stopping the moment the shared MAX_LEADS_PER_RUN quota (see
fb_group_url_collector.py) is reached for the day.

Meant to be run on a schedule (e.g. Windows Task Scheduler, daily at 9 AM).
The scheduled task can only launch this script; it cannot log into Facebook
for you -- see run_daily.bat, which starts the debug Chrome window pointed
at a persistent profile so a session logged in once keeps working across
days, same as any normal browser profile.

Deviates from the main tool's usual scope in one respect: it navigates
itself between groups (driver.get(url)) instead of only reading a page the
user already opened. This is done on the user's explicit instruction and
explicit group list -- it never joins a group, never logs in, and skips any
group that doesn't render post content without joining (e.g. "request to
join" gates), rather than trying to bypass that.

Each group's scan uses fb_group_url_collector's own MAX_POSTS_PER_RUN /
CONSECUTIVE_OLD_POSTS_TO_STOP settings (not a separate, smaller per-group
cap here) -- so it covers a group's FULL available post history within the
DAYS_BACK window, not just the first handful of posts.
"""

import importlib.util
import time
from datetime import datetime, timedelta

spec = importlib.util.spec_from_file_location("collector", "fb_group_url_collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

log = collector.log

GROUP_LOAD_WAIT_SECONDS = 2

GROUP_URLS = [
    ("I Need a Editor", "https://www.facebook.com/share/g/1YpD7y4noQ/"),
    ("VIDEO EDITORS for TIKTOK YOUTUBE & INSTAGRAM", "https://www.facebook.com/share/g/1SZ9Bs6Dn1/"),
    ("Looking For Video Editor UK", "https://www.facebook.com/share/g/1EN1BBuUB8/"),
    ("Professional Video Editors", "https://www.facebook.com/share/g/17sYQhzdR4/"),
    ("Freelance Video Editor Philippines Official", "https://www.facebook.com/share/g/1BiuKWizbZ/"),
    ("Hire A VIDEO EDITOR WORLDWIDE Here", "https://www.facebook.com/share/g/14fbrHUzmsZ/"),
    ("Video Editors of the Philippines", "https://www.facebook.com/share/g/1AhHHfAAdS/"),
    ("PHILIPPINES - FOR PROFESSIONAL VIDEO EDITORS ONLY", "https://www.facebook.com/share/g/1Gq6caq1u8/"),
    ("Pakistani Video Editors", "https://www.facebook.com/share/g/17wWHArQg5/"),
    ("Video editors(Philippines)", "https://www.facebook.com/share/g/1EZBXK2SzH/"),
    ("Video Editing Groups", "https://www.facebook.com/groups/294830932856749/"),
    ("Hire a Video Editor - Freelance Hub", "https://www.facebook.com/groups/2045519552480506/"),
    ("Video Editors Community", "https://www.facebook.com/groups/503066452462593/"),
    ("Video Editing Services and Community Worldwide", "https://www.facebook.com/groups/610594114897677/"),
    ("YouTube Creators - Promote Your YouTube Channel", "https://www.facebook.com/groups/ytcreatorspromo/"),
    ("Video Editor freelancer", "https://www.facebook.com/groups/396537685660594/"),
    ("EDITOR HUB", "https://www.facebook.com/groups/1380781919968166/"),
    ("Video editing", "https://www.facebook.com/groups/409392954169013/"),
    ("AI Video Editing Jobs & Services Online", "https://www.facebook.com/groups/1155257268851946/"),
    ("Freelancer Video Editing", "https://www.facebook.com/groups/1326223411530920/"),
    ("Freelance Video Editor Philippines", "https://www.facebook.com/share/g/1b6kxk64Ny/"),
    ("Video Editors of the Philippines (2)", "https://www.facebook.com/share/g/1D1Cud11Wk/"),
    ("Video editors(Philippines) (2)", "https://www.facebook.com/share/g/1NsqCBmshn/"),
    ("Video Editing Jobs & video Editors (USA/UK/Canada)", "https://www.facebook.com/share/g/17fWUHPZGW/"),
    ("Video Editing Jobs & video Editors (worldwide)", "https://www.facebook.com/groups/6100423586635776/"),
    ("Video Editing JOBS", "https://www.facebook.com/groups/510764166305355/"),
    ("Video Editing, Voice over, Whiteboard, 2D & 3D Jobs in Pakistan", "https://www.facebook.com/groups/1102047611258448/"),
    ("Video Editing", "https://www.facebook.com/groups/editingvideofilms/"),
    ("YouTube Video editing", "https://www.facebook.com/groups/302737219333992/"),
    ("Video Editors Make Money Online", "https://www.facebook.com/groups/evokeverse/"),
    ("Video Editing Jobs (India)", "https://www.facebook.com/groups/1080596700055075/"),
]


def looks_like_join_gate(driver) -> bool:
    """True if the page shows a join/request gate rather than actual post content."""
    try:
        containers = collector.find_rendered_post_containers(driver)
    except Exception:
        return True
    # Uses the shared rendered-container helper (which waits out Facebook's
    # loading skeletons) so a merely slow-loading group is never mistaken
    # for an inaccessible one. If no REAL post content rendered even after
    # that wait, treat it as inaccessible (private/needs-join) rather than
    # guessing or joining.
    return len(containers) == 0


def main():
    log.info("Daily multi-group scrape started.")

    quota_used = collector.load_daily_quota_used()
    log.info("Daily lead quota: %d/%d used today.", quota_used, collector.MAX_LEADS_PER_RUN)
    if quota_used >= collector.MAX_LEADS_PER_RUN:
        log.info("Daily lead limit already reached -- skipping this run. "
                  "Resets automatically on the next run after today.")
        return 0

    try:
        driver = collector.attach_to_user_chrome()
    except Exception:
        log.error("Could not attach to Chrome -- is the debug Chrome window running and logged in? "
                   "Script completed with errors.")
        return 1

    seen_urls = collector.load_seen_urls()
    # Cross-group dedup store: signatures of leads already saved, from any group.
    lead_signatures = collector.load_lead_signatures()
    now = datetime.now()
    cutoff = now - timedelta(days=collector.DAYS_BACK)

    total_written_this_run = 0
    groups_skipped_inaccessible = 0
    groups_errored = 0

    for i, (name, url) in enumerate(GROUP_URLS, start=1):
        remaining = collector.MAX_LEADS_PER_RUN - (quota_used + total_written_this_run)
        if remaining <= 0:
            log.info("Daily lead limit reached (%d/%d) -- stopping immediately before group %d/%d.",
                      quota_used + total_written_this_run, collector.MAX_LEADS_PER_RUN, i, len(GROUP_URLS))
            break

        log.info("[%d/%d] Visiting group: %s (%s)", i, len(GROUP_URLS), name, url)
        try:
            driver.get(url)
            # Chrome throttles hidden pages, which makes Facebook render an
            # empty feed -- restore/foreground the window before each group
            # rather than silently scraping nothing.
            collector.ensure_page_visible(driver)
            time.sleep(GROUP_LOAD_WAIT_SECONDS)

            if not collector.detect_group_page(driver):
                log.warning("[%d/%d] Not a group page after navigation, skipping: %s", i, len(GROUP_URLS), url)
                groups_skipped_inaccessible += 1
                continue

            if looks_like_join_gate(driver):
                log.warning("[%d/%d] No post content rendered (likely needs joining or is inaccessible); "
                             "skipping without joining: %s", i, len(GROUP_URLS), url)
                groups_skipped_inaccessible += 1
                continue

            # No pre-scroll here: Facebook virtualizes the feed, so scrolling
            # before scanning discards the newest posts from the DOM.
            # process_posts starts at the top and scrolls incrementally.
            result = collector.process_posts(driver, cutoff, seen_urls, max_new_leads=remaining,
                                             known_signatures=lead_signatures)

            log.info("[%d/%d] %s -- processed %d, qualifying %d, old %d, dup %d, cross-group-dup %d, "
                      "invalid %d, ts-unparseable %d, not-qualifying %d",
                      i, len(GROUP_URLS), name, result.posts_processed, len(result.collected),
                      result.old_posts_skipped, result.duplicates_skipped,
                      result.cross_group_duplicates_skipped, result.invalid_urls_skipped,
                      result.unparseable_timestamps_skipped, result.not_qualifying_lead_skipped)

            seen_urls |= result.seen_this_run
            collector.save_seen_urls(seen_urls)
            if result.signatures_this_run:
                lead_signatures.extend(result.signatures_this_run)
                collector.save_lead_signatures(lead_signatures)

            if result.collected:
                try:
                    worksheet = collector.get_sheets_worksheet()
                    existing_sheet_urls = collector.get_existing_sheet_urls(worksheet)
                    to_write = [p for p in result.collected if p.url not in existing_sheet_urls]
                    written = collector.append_posts_to_sheet(worksheet, to_write)
                    total_written_this_run += written
                    collector.save_daily_quota_used(quota_used + total_written_this_run)
                    log.info("Wrote %d lead(s) from this group. Daily quota now %d/%d.",
                              written, quota_used + total_written_this_run, collector.MAX_LEADS_PER_RUN)
                except Exception as exc:
                    log.error("Google Sheets write failed for this group's leads, preserving locally: %s", exc)
                    collector.write_local_backup(result.collected)

        except Exception as exc:
            log.error("[%d/%d] Group failed, skipping and continuing: %s", i, len(GROUP_URLS), exc)
            groups_errored += 1
            continue

    try:
        driver.quit()
    except Exception:
        pass

    log.info("Daily multi-group scrape finished. New leads saved this run: %d. "
              "Daily total: %d/%d. Groups skipped (inaccessible): %d. Groups errored: %d.",
              total_written_this_run, quota_used + total_written_this_run, collector.MAX_LEADS_PER_RUN,
              groups_skipped_inaccessible, groups_errored)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

