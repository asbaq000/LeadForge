"""
Instagram account DISCOVERY via hashtag search — one keyword at a time.

There is no true keyword-search API on Instagram, so this works by:
  1. Converting your keyword into a hashtag (spaces stripped)
  2. Pulling N recent/top posts under that hashtag
  3. Collecting the unique post authors as candidate usernames

Run this once per keyword (that's why it's built for ONE keyword per run,
not a loop over your whole list) — safer on rate limits, and lets you
review/adjust each keyword's hashtag mapping before running the next.

Output: candidate usernames appended to a CSV, ready to feed into
insta_frequency_check.py for the enrichment/filter phase.

Install:
    pip install instaloader --break-system-packages

Usage:
    python insta_discover_by_keyword.py "Wildlife Documentary"
"""

import instaloader
import csv
import sys
import time
import re
import os

# ---------------- CONFIG ----------------

POSTS_PER_KEYWORD = 40        # how many hashtag posts to scan for authors
USE_TOP_POSTS = True          # True = hashtag's "top posts", False = most recent
DELAY_BETWEEN_POSTS = 2       # seconds between post fetches, be polite

USE_LOGIN = False             # hashtag browsing needs login far more often than profile lookups
LOGIN_USERNAME = "your_throwaway_username"

OUTPUT_CSV = "/mnt/user-data/outputs/discovered_accounts.csv"

# -----------------------------------------


def keyword_to_hashtag(keyword: str) -> str:
    """'Wildlife Documentary' -> 'wildlifedocumentary'"""
    return re.sub(r"[^a-z0-9]", "", keyword.lower())


def get_loader():
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
    )
    if USE_LOGIN:
        try:
            L.load_session_from_file(LOGIN_USERNAME)
            print(f"Loaded saved session for {LOGIN_USERNAME}")
        except FileNotFoundError:
            print("No saved session found — logging in interactively.")
            L.interactive_login(LOGIN_USERNAME)
            L.save_session_to_file()
    return L


def discover(L, keyword: str):
    hashtag_str = keyword_to_hashtag(keyword)
    print(f"Keyword: '{keyword}'  ->  hashtag: #{hashtag_str}")

    try:
        hashtag = instaloader.Hashtag.from_name(L.context, hashtag_str)
    except instaloader.exceptions.LoginRequiredException:
        print("  ERROR: this hashtag requires login to browse. Set USE_LOGIN = True.")
        return []
    except Exception as e:
        print(f"  ERROR fetching hashtag: {e}")
        return []

    post_iter = hashtag.get_top_posts() if USE_TOP_POSTS else hashtag.get_posts()

    seen_usernames = set()
    candidates = []

    try:
        for i, post in enumerate(post_iter):
            if i >= POSTS_PER_KEYWORD:
                break
            username = post.owner_username
            if username not in seen_usernames:
                seen_usernames.add(username)
                candidates.append({
                    "keyword": keyword,
                    "hashtag": hashtag_str,
                    "username": username,
                    "found_via_post_date": post.date_utc.isoformat(),
                })
            time.sleep(DELAY_BETWEEN_POSTS)
    except instaloader.exceptions.ConnectionException as e:
        print(f"  Rate limited partway through — keeping {len(candidates)} candidates found so far. ({e})")

    print(f"  Found {len(candidates)} unique candidate accounts.")
    return candidates


def append_to_csv(rows):
    fieldnames = ["keyword", "hashtag", "username", "found_via_post_date"]
    file_exists = os.path.exists(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    if len(sys.argv) < 2:
        print('Usage: python insta_discover_by_keyword.py "Wildlife Documentary"')
        sys.exit(1)

    keyword = sys.argv[1]
    L = get_loader()
    candidates = discover(L, keyword)

    if candidates:
        append_to_csv(candidates)
        print(f"\nAppended {len(candidates)} candidates to {OUTPUT_CSV}")
    else:
        print("\nNo candidates found — nothing written.")


if __name__ == "__main__":
    main()
