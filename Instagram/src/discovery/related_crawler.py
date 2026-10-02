"""
Breadth-first crawl over Instagram's "related profiles" graph.

Each successful profile fetch hands back up to ~50 accounts Instagram itself
considers similar, which makes the graph a workable substitute for the search
endpoint we aren't allowed to call. Seeds come from keyword_seeds; the frontier
only expands through accounts that matched a keyword, so the crawl stays in
the niche instead of drifting into whatever is globally popular.

Two things learned the hard way and designed around:

  * Seed handles named exactly after a niche are mostly dead squatter
    accounts. They're still worth fetching — their *related* edges point at
    the real accounts — but they must not eat the whole request budget, so
    seeds and expansion get separate allowances.

  * The anonymous request quota is roughly a hundred-odd calls before
    Instagram starts answering 401. A long crawl *will* hit it, so every
    match is checkpointed to disk as it's found and the crawl can resume.
"""

import json
import os
import time
from collections import deque

from instagram_parser import (
    ProfileNotFound,
    RateLimited,
    ScrapeError,
    embedded_post_timestamps,
    fetch_profile,
    parse_profile,
    related_usernames,
)
from keyword_seeds import candidate_handles, matching_keywords

# Accounts this big are never the answer and their related-profile edges
# point at other giants, so they're not worth expanding through either.
EXPANSION_FOLLOWER_CEILING = 5_000_000

# Share of the budget spent resolving seed guesses. The rest goes to walking
# the graph, which is where the accounts worth finding actually live.
SEED_BUDGET_SHARE = 0.35


def load_checkpoint(path):
    """Return (records, visited_usernames) from a previous run, if any."""
    records, visited = [], set()
    if not path or not os.path.exists(path):
        return records, visited
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            visited.add(entry.get("username", ""))
            if entry.get("_matched"):
                entry.pop("_matched", None)
                records.append(entry)
    return records, visited


def _dead_path(checkpoint_path):
    if not checkpoint_path:
        return None
    return os.path.join(os.path.dirname(checkpoint_path), "dead-handles.txt")


def load_dead_handles(checkpoint_path):
    """
    Handles we've already confirmed don't exist.

    Seed guessing invents names like `wildlife_documentaryhq`, and most of
    them are 404s. Without remembering that, every run re-spends a request
    proving the same handles still don't exist — on a ~100-request anonymous
    budget that is a big fraction of the day's quota burned on nothing.
    """
    path = _dead_path(checkpoint_path)
    if not path or not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {ln.strip() for ln in f if ln.strip()}


def record_dead_handle(checkpoint_path, username):
    path = _dead_path(checkpoint_path)
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(username + "\n")


def _checkpoint(path, record, matched):
    if not path:
        return
    entry = dict(record)
    entry["_matched"] = matched
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def crawl(
    session,
    keywords,
    settings=None,
    max_profiles=400,
    max_depth=2,
    delay=1.0,
    log=print,
    checkpoint_path=None,
    resume=True,
    skip=None,
    on_candidate=None,
    target=None,
):
    """
    Walk the related-profiles graph and return every account that matched at
    least one keyword.

    max_profiles caps total fetches — the graph is effectively infinite, so
    this is the real cost control. Returns (matches, stats).

    on_candidate(record) is called for each keyword match and should return
    True if that account is a keeper. Once `target` keepers exist the crawl
    stops immediately. That early exit is the difference between spending 30
    requests and spending the entire quota, so pass it whenever you only need
    a fixed number of leads.

    skip is a set of usernames to never fetch — previously delivered leads.
    """
    settings = settings or {}
    skip = skip or set()

    wanted = set(keywords)
    matches, visited = ([], set())
    if resume:
        everything, visited = load_checkpoint(checkpoint_path)
        # The checkpoint is shared by every keyword, so keep only the part
        # that belongs to this crawl. Without this, hunting one niche would
        # inherit every other niche's frontier and spend its whole budget
        # fetching accounts that can't possibly match.
        matches = [m for m in everything
                   if wanted & set(m.get("keywords") or [])
                   and m.get("username", "").lower() not in skip]
        if visited:
            log(f"    resuming: {len(visited)} already checked, "
                f"{len(matches)} relevant match(es) carried over.")
    visited |= skip
    dead = load_dead_handles(checkpoint_path)
    if dead:
        log(f"    skipping {len(dead)} handle(s) already known not to exist.")
    visited |= dead

    seeds = deque()
    queued = set(visited)
    for keyword in keywords:
        for handle in candidate_handles(keyword):
            if handle not in queued:
                queued.add(handle)
                seeds.append((handle, 0, keyword))

    # Accounts already fetched on an earlier run that match this keyword and
    # were never delivered. Re-offering them costs nothing — the profile is
    # already on disk — so do it before spending a single request.
    stats_qualified = 0
    if on_candidate:
        for record in matches:
            if on_candidate(record):
                stats_qualified += 1
                if target and stats_qualified >= target:
                    log(f"    target met from the checkpoint — no requests needed.")
                    break

    # Re-arm expansion from matches recovered off the checkpoint.
    frontier = deque()
    for record in matches:
        depth = record.get("found_at_depth", 0)
        if depth >= max_depth:
            continue
        for neighbour in record.get("_related") or []:
            if neighbour not in queued:
                queued.add(neighbour)
                frontier.append((neighbour, depth + 1, record["username"]))

    seed_budget = int(max_profiles * SEED_BUDGET_SHARE)
    log(f"Seeded {len(seeds)} candidate handles from {len(keywords)} keywords "
        f"(seed budget {seed_budget}, expansion budget {max_profiles - seed_budget}).")

    stats = {"fetched": 0, "hits": len(matches), "seed_hits": 0,
             "missing": 0, "errors": 0, "rate_limited": False,
             "qualified": stats_qualified,
             "target_reached": bool(target and stats_qualified >= target)}

    def take_next():
        """Seeds until their allowance runs out, then the graph frontier."""
        if seeds and stats["fetched"] < seed_budget:
            return seeds.popleft()
        if frontier:
            return frontier.popleft()
        if seeds:
            return seeds.popleft()
        return None

    while stats["fetched"] < max_profiles and not stats["target_reached"]:
        nxt = take_next()
        if nxt is None:
            break
        username, depth, via = nxt

        try:
            user = fetch_profile(session, username, settings)
        except ProfileNotFound:
            stats["fetched"] += 1
            stats["missing"] += 1
            record_dead_handle(checkpoint_path, username)
            continue
        except RateLimited as exc:
            stats["fetched"] += 1  # the request was actually sent and burned quota
            log(f"  ! rate limited on @{username} — stopping cleanly. {exc}")
            stats["rate_limited"] = True
            break
        except ScrapeError:
            stats["fetched"] += 1
            stats["errors"] += 1
            continue

        stats["fetched"] += 1

        record = parse_profile(user)
        record["user_id"] = str(user.get("id") or "")
        record["is_private"] = bool(user.get("is_private"))
        record["found_at_depth"] = depth
        record["found_via"] = via
        # Free when the primary app id served this profile; the enrichment
        # step only pays for a request when this comes back empty.
        record["post_timestamps"] = embedded_post_timestamps(user)

        neighbours = related_usernames(user)
        hit_keywords = matching_keywords(record, keywords)

        if hit_keywords:
            record["keywords"] = hit_keywords
            record["_related"] = neighbours
            matches.append(record)
            stats["hits"] += 1
            if depth == 0:
                stats["seed_hits"] += 1

            kept = on_candidate(record) if on_candidate else None
            mark = "*" if kept else "+"
            if kept:
                stats["qualified"] += 1
            log(f"  {mark} @{record['username']:<28} {record['follower_count']:>9,} followers"
                f"  d{depth}  [{', '.join(hit_keywords[:2])}]")

        _checkpoint(checkpoint_path, record, bool(hit_keywords))

        if target and stats["qualified"] >= target:
            log(f"  Target of {target} reached — stopping after "
                f"{stats['fetched']} fetches.")
            stats["target_reached"] = True
            break

        # Only walk onward from on-topic, human-sized accounts.
        if hit_keywords and depth < max_depth and \
                record["follower_count"] < EXPANSION_FOLLOWER_CEILING:
            for neighbour in neighbours:
                if neighbour not in queued:
                    queued.add(neighbour)
                    frontier.append((neighbour, depth + 1, record["username"]))

        # No sleep here: the HTTP client paces every request itself, with
        # jitter. Adding a second delay on top would just halve throughput
        # without making the traffic any less conspicuous.

    stats["queued"] = len(queued)
    stats["frontier_left"] = len(frontier) + len(seeds)
    for record in matches:
        record.pop("_related", None)
    return matches, stats
