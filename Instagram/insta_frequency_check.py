"""
Instagram profile enrichment checker.

Feed it a list of usernames -> for each one it fetches:
  - follower count (+ pass/fail against MIN_FOLLOWERS)
  - the timestamps of the latest N posts (default 5), one column each

So you can eyeball the last 5 post dates yourself and judge posting
frequency, rather than the script computing a posts/week number for you.

Install:
    pip install instaloader --break-system-packages

Usage:
    python insta_frequency_check.py

Notes on login:
  - Works WITHOUT login for light/occasional use, but Instagram rate-limits
    anonymous requests hard and fast (often after just a handful of profile
    fetches in a session). For anything beyond a quick one-off test, use a
    login (ideally a throwaway/secondary account, not your friend's main one).
  - If you don't want to hardcode a password in this file, use instaloader's
    CLI once to create a saved session, then just load it here:
        instaloader --login=your_throwaway_username
    That creates a session file on disk you can reuse with load_session_from_file().
"""

import instaloader
from datetime import datetime, timezone
import csv
import time
import sys

# ---------------- CONFIG ----------------

USERNAMES = [
    # put the usernames you want to check here, or load from a file/sheet
    "example_user_1",
    "example_user_2",
]

MIN_FOLLOWERS = 200_000
POSTS_TO_SHOW = 5             # how many recent posts' timestamps to show
DELAY_BETWEEN_ACCOUNTS = 5    # seconds, be polite / reduce rate-limit risk

# Login is optional. Leave USE_LOGIN = False to try anonymous first.
USE_LOGIN = False
LOGIN_USERNAME = "your_throwaway_username"
SESSION_FILE = None  # if you saved a session via the CLI, put its path here

OUTPUT_CSV = "/mnt/user-data/outputs/insta_results.csv"

# -----------------------------------------


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
            if SESSION_FILE:
                L.load_session_from_file(LOGIN_USERNAME, SESSION_FILE)
            else:
                L.load_session_from_file(LOGIN_USERNAME)  # default location
            print(f"Loaded saved session for {LOGIN_USERNAME}")
        except FileNotFoundError:
            print("No saved session found — logging in interactively.")
            L.interactive_login(LOGIN_USERNAME)
            L.save_session_to_file()
    return L


def check_profile(L, username):
    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except instaloader.exceptions.ProfileNotExistsException:
        return {"username": username, "error": "profile not found"}
    except instaloader.exceptions.ConnectionException as e:
        return {"username": username, "error": f"rate limited / connection error: {e}"}

    followers = profile.followers
    bio = profile.biography
    external_url = profile.external_url or ""
    is_private = profile.is_private

    # Private profiles: post data isn't accessible unless the logged-in
    # account follows them. Don't even attempt get_posts() — it'll either
    # raise or silently return nothing, so handle it explicitly instead.
    if is_private:
        ts_strs = [""] * POSTS_TO_SHOW
        result = {
            "username": username,
            "followers": followers,
            "bio": bio,
            "external_url": external_url,
            "is_private": True,
            "passes_follower_filter": followers >= MIN_FOLLOWERS,
            "error": "private account — post timestamps not accessible without following it",
        }
        for i, ts in enumerate(ts_strs):
            result[f"post_{i+1}_time"] = ts
        return result

    timestamps = []
    try:
        for i, post in enumerate(profile.get_posts()):
            timestamps.append(post.date_utc)
            if i + 1 >= POSTS_TO_SHOW:
                break
    except instaloader.exceptions.LoginRequiredException:
        return {"username": username, "is_private": is_private,
                 "error": "login required to view this account's posts"}
    except instaloader.exceptions.ConnectionException as e:
        return {"username": username, "is_private": is_private,
                 "error": f"rate limited while fetching posts: {e}"}

    # pad to fixed width so CSV columns line up
    ts_strs = [t.isoformat() for t in timestamps]
    while len(ts_strs) < POSTS_TO_SHOW:
        ts_strs.append("")

    passes_followers = followers >= MIN_FOLLOWERS

    result = {
        "username": username,
        "followers": followers,
        "bio": bio,
        "external_url": external_url,
        "is_private": is_private,
        "passes_follower_filter": passes_followers,
        "error": "",
    }
    for i, ts in enumerate(ts_strs):
        result[f"post_{i+1}_time"] = ts
    return result


def main():
    L = get_loader()
    results = []

    for i, username in enumerate(USERNAMES):
        print(f"[{i+1}/{len(USERNAMES)}] Checking @{username} ...")
        result = check_profile(L, username)
        results.append(result)
        print(f"  -> {result}")
        if i + 1 < len(USERNAMES):
            time.sleep(DELAY_BETWEEN_ACCOUNTS)

    fieldnames = (
        ["username", "followers", "is_private", "bio", "external_url", "passes_follower_filter", "error"]
        + [f"post_{i+1}_time" for i in range(POSTS_TO_SHOW)]
    )
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row = {k: r.get(k, "") for k in fieldnames}
            writer.writerow(row)

    print(f"\nDone. Results written to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
