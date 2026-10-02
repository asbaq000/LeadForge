"""
Hashtag discovery across the whole keyword list — the logged-in route.

Instagram answers `login_required` to every anonymous hashtag and search
call, so unlike the rest of this project this script needs a session. It
does NOT want your password: create a session file once with instaloader's
own CLI and this script reuses it.

    pip install instaloader
    instaloader --login=your_throwaway_username

Then:

    python src/discovery/hashtag_search.py -k data/keywords.txt --login your_throwaway_username

Output is a plain handles file, which is exactly what find_creators eats:

    python src/find_creators.py -i data/candidates.txt

Use a throwaway account. Hashtag browsing at this volume is the kind of
traffic Instagram rate-limits and, eventually, blocks.
"""

import argparse
import os
import re
import sys
import time

try:
    import instaloader
except ImportError:  # pragma: no cover - dependency is optional
    instaloader = None


def keyword_to_hashtag(keyword):
    """'WW2 History Documentary' -> 'ww2historydocumentary'"""
    return re.sub(r"[^a-z0-9]", "", keyword.lower())


def read_keywords(path):
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def get_loader(login_username):
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
    )
    if login_username:
        try:
            L.load_session_from_file(login_username)
            print(f"Loaded saved session for {login_username}")
        except FileNotFoundError:
            sys.exit(
                f"No saved session for {login_username}. Create one first:\n"
                f"    instaloader --login={login_username}"
            )
    return L


def discover(L, keyword, posts_per_keyword, use_top, delay):
    tag = keyword_to_hashtag(keyword)
    print(f"#{tag}  ({keyword})")

    try:
        hashtag = instaloader.Hashtag.from_name(L.context, tag)
    except Exception as exc:  # noqa: BLE001 — one bad tag shouldn't stop the sweep
        print(f"   skipped: {str(exc)[:110]}")
        return set()

    posts = hashtag.get_top_posts() if use_top else hashtag.get_posts()
    authors = set()
    try:
        for i, post in enumerate(posts):
            if i >= posts_per_keyword:
                break
            authors.add(post.owner_username)
            time.sleep(delay)
    except Exception as exc:  # noqa: BLE001
        print(f"   stopped early after {len(authors)}: {str(exc)[:80]}")

    print(f"   {len(authors)} unique authors")
    return authors


def main(argv=None):
    p = argparse.ArgumentParser(description="Hashtag discovery over a keyword list (needs login).")
    p.add_argument("-k", "--keywords", default="data/keywords.txt")
    p.add_argument("-o", "--output", default="data/candidates.txt")
    p.add_argument("--login", required=True, help="username whose saved session to load")
    p.add_argument("--posts-per-keyword", type=int, default=40)
    p.add_argument("--recent", action="store_true", help="most recent posts instead of top posts")
    p.add_argument("--delay", type=float, default=2.0)
    args = p.parse_args(argv)

    if instaloader is None:
        sys.exit("instaloader is not installed:  pip install instaloader")

    keywords = read_keywords(args.keywords)
    L = get_loader(args.login)

    # Resume support: never re-discover what's already in the output file.
    found = set()
    if os.path.exists(args.output):
        with open(args.output, "r", encoding="utf-8") as f:
            found = {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
        print(f"Resuming — {len(found)} handles already collected.\n")

    for index, keyword in enumerate(keywords, start=1):
        print(f"[{index}/{len(keywords)}] ", end="")
        authors = discover(L, keyword, args.posts_per_keyword, not args.recent, args.delay)
        fresh = authors - found
        if fresh:
            found |= fresh
            # Append as we go — a rate-limit kill mid-sweep loses nothing.
            with open(args.output, "a", encoding="utf-8") as f:
                for name in sorted(fresh):
                    f.write(name + "\n")

    print(f"\n{len(found)} unique handles in {args.output}")
    print(f"Next:  python src/find_creators.py -i {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
