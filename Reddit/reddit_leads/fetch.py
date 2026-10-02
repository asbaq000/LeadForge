"""Reddit's public JSON API, read without an account.

Every Reddit listing answers on `.json` without auth. No client ID, no OAuth
token and no login are involved, so this whole module is stdlib-only.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

BASE = "https://www.reddit.com"


class FetchError(Exception):
    pass


def _get(url, timeout=30):
    """GET a JSON URL with backoff on Reddit's rate limiter."""
    request = urllib.request.Request(
        url, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    )
    delay = config.REQUEST_DELAY_SECONDS
    last_error = None
    for attempt in range(config.MAX_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 429 = rate limited, 5xx = transient. Anything else is fatal.
            if exc.code != 429 and exc.code < 500:
                raise FetchError("HTTP %s for %s" % (exc.code, url))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(delay * (2 ** attempt))
    raise FetchError("giving up on %s (%s)" % (url, last_error))


def _listing_posts(payload):
    """Flatten a Reddit listing payload into post dicts."""
    children = (payload or {}).get("data", {}).get("children", []) or []
    return [child.get("data", {}) for child in children if child.get("kind") == "t3"]


def search_subreddit(subreddit, query, sort="new", time_filter="month", limit=100):
    """Search inside one subreddit."""
    params = urllib.parse.urlencode(
        {
            "q": query,
            "restrict_sr": "1",
            "sort": sort,
            "t": time_filter,
            "limit": limit,
            "raw_json": 1,
        }
    )
    url = "%s/r/%s/search.json?%s" % (BASE, subreddit, params)
    return _listing_posts(_get(url))


def search_all(query, sort="new", time_filter="month", limit=100):
    """Search across all of Reddit."""
    params = urllib.parse.urlencode(
        {"q": query, "sort": sort, "t": time_filter, "limit": limit, "raw_json": 1}
    )
    return _listing_posts(_get("%s/search.json?%s" % (BASE, params)))


def new_posts(subreddit, limit=100):
    """The newest posts in a subreddit."""
    params = urllib.parse.urlencode({"limit": limit, "raw_json": 1})
    return _listing_posts(_get("%s/r/%s/new.json?%s" % (BASE, subreddit, params)))


def build_plan():
    """Every (label, callable) source this run will pull from."""
    plan = []
    for subreddit in config.GIG_SUBREDDITS:
        for query in config.GIG_QUERIES:
            plan.append(
                ("r/%s?q=%s" % (subreddit, query),
                 lambda s=subreddit, q=query: search_subreddit(s, q))
            )
    for subreddit in config.AI_SUBREDDITS:
        plan.append(("r/%s/new" % subreddit, lambda s=subreddit: new_posts(s)))
        for query in config.AI_QUERIES:
            plan.append(
                ("r/%s?q=%s" % (subreddit, query),
                 lambda s=subreddit, q=query: search_subreddit(s, q))
            )
    for query in config.GLOBAL_QUERIES:
        plan.append(("all?q=%s" % query, lambda q=query: search_all(q)))
    return plan


def collect(verbose=True, delay=None):
    """Run every source, tolerating individual failures."""
    delay = config.REQUEST_DELAY_SECONDS if delay is None else delay
    posts, errors = [], []
    plan = build_plan()
    for index, (label, call) in enumerate(plan, 1):
        try:
            batch = call()
            posts.extend(batch)
            if verbose:
                print("  [%2d/%d] %-46s %3d posts" % (index, len(plan), label, len(batch)))
        except FetchError as exc:
            errors.append((label, str(exc)))
            if verbose:
                print("  [%2d/%d] %-46s FAILED: %s" % (index, len(plan), label, exc))
        time.sleep(delay)
    return posts, errors
