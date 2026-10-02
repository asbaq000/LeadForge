"""Turn raw Reddit posts into ranked, deduplicated leads."""

import datetime
import time

from . import config, dedupe, filters, score


def post_text(post):
    """Title + body, normalized once and reused by every rule."""
    return filters.normalize((post.get("title") or "") + " \n " + (post.get("selftext") or ""))


def build_lead(post, max_age_days, now):
    """Return a lead dict, or None with the reason it was rejected."""
    text = post_text(post)
    title = post.get("title") or ""
    subreddit = post.get("subreddit") or ""

    created = post.get("created_utc")
    if created is None:
        return None, "no timestamp"
    if not filters.is_recent(created, max_age_days, now):
        return None, "older than %d days (%.1f)" % (
            max_age_days, filters.age_days(created, now))

    if post.get("removed_by_category") or post.get("selftext") == "[removed]":
        return None, "removed post"

    if filters.is_seeking_work(title, post.get("selftext", "")):
        return None, "author is offering services, not hiring"

    is_ai, ai_evidence, not_ai = filters.classify_ai_work(text, subreddit)
    if not is_ai:
        return None, not_ai

    is_gig, intent_evidence, not_a_gig = filters.classify_intent(
        title, post.get("selftext", ""), subreddit)
    if not is_gig:
        return None, not_a_gig

    is_remote, evidence, why_not = filters.classify_location(text, subreddit)
    if not is_remote:
        return None, why_not

    lead_score, reasons = score.score_lead(post, text, max_age_days, now)
    created_dt = datetime.datetime.fromtimestamp(created, datetime.timezone.utc)
    body = post.get("selftext") or ""
    snippet = " ".join(body.split())[:280]

    return {
        "post": post,
        "score": lead_score,
        "row": {
            "date_posted_utc": created_dt.strftime("%Y-%m-%d %H:%M"),
            "days_ago": round(filters.age_days(created, now), 1),
            "subreddit": "r/" + subreddit,
            "title": " ".join(title.split()),
            "lead_type": filters.lead_type(text),
            "intent_evidence": intent_evidence,
            "work_location": "Remote",
            "remote_evidence": evidence,
            "pay_or_budget": filters.extract_pay(title + " " + body) or "not stated",
            "author": "u/" + (post.get("author") or "[unknown]"),
            "post_url": "https://www.reddit.com" + (post.get("permalink") or ""),
            "contact": score.contact_hint(post, body),
            "comments": post.get("num_comments") or 0,
            "lead_score": lead_score,
            "why_it_matches": "; ".join(reasons),
            "snippet": snippet,
        },
    }, None


def build_leads(posts, max_age_days=config.DEFAULT_MAX_AGE_DAYS,
                limit=config.DEFAULT_LEAD_COUNT, now=None):
    """Filter, score, sort, deduplicate and trim to `limit` leads."""
    now = time.time() if now is None else now
    leads, rejections = [], {}
    for post in posts:
        lead, reason = build_lead(post, max_age_days, now)
        if lead:
            leads.append(lead)
        else:
            rejections[reason] = rejections.get(reason, 0) + 1

    # Sort before dedupe so the highest-scoring copy of a repost survives.
    leads.sort(key=lambda item: item["score"], reverse=True)
    unique, duplicates = dedupe.deduplicate(leads)

    # limit of 0 (or less) means "every lead that passed", not "none".
    kept = unique if not limit or limit <= 0 else unique[:limit]

    stats = {
        "fetched": len(posts),
        "passed_filters": len(leads),
        "duplicates_removed": duplicates,
        "unique": len(unique),
        "returned": len(kept),
        "rejections": rejections,
    }
    return [lead["row"] for lead in kept], stats
