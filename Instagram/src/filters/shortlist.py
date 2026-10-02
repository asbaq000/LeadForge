"""
Turn scraped profiles into a shortlist.

The brief: accounts under 500k followers that posted at least 5 times in the
last 7 days. Both thresholds are configurable — the defaults encode the brief.

One subtlety worth knowing: the post feed returns roughly the 24 most recent
posts, pinned ones first and out of chronological order. That's plenty to
answer "at least 5 in the last week" (if all 24 fall inside the window, the
answer is yes regardless of what came before), but it means `posts_last_week`
saturates rather than being a true total. Never read it as a full count.
"""

from datetime import datetime, timedelta, timezone

DEFAULT_MAX_FOLLOWERS = 500_000
DEFAULT_MIN_FOLLOWERS = 10_000
DEFAULT_MIN_POSTS = 5
DEFAULT_WINDOW_DAYS = 7

# Verdicts, in the order a human wants to read them.
QUALIFIED = "qualified"
REJECTED = "rejected"
UNKNOWN = "unknown"


def count_posts_since(timestamps, cutoff_epoch):
    return sum(1 for ts in timestamps or [] if ts >= cutoff_epoch)


def evaluate(
    record,
    max_followers=DEFAULT_MAX_FOLLOWERS,
    min_followers=DEFAULT_MIN_FOLLOWERS,
    min_posts=DEFAULT_MIN_POSTS,
    window_days=DEFAULT_WINDOW_DAYS,
    now=None,
):
    """
    Judge one scraped record and return it annotated with the verdict.

    Adds: posts_last_week, latest_post, passes_followers, passes_frequency,
    verdict, verdict_reason.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    cutoff_epoch = cutoff.timestamp()

    followers = record.get("follower_count", 0)
    timestamps = record.get("post_timestamps")

    result = dict(record)
    result["passes_followers"] = min_followers <= followers < max_followers

    if record.get("is_private"):
        result.update(
            posts_last_week=0,
            latest_post="",
            passes_frequency=False,
            verdict=REJECTED,
            verdict_reason="private account — posts not visible",
        )
        return result

    # Distinguish "we looked and there were none" from "we never got to look".
    # An empty list is only trustworthy when media_count is a confident 0;
    # it's None whenever the fallback app id served the profile, and guessing
    # zero there would brand active accounts as dead.
    feed_unavailable = not timestamps and record.get("media_count") != 0
    if feed_unavailable:
        result.update(
            posts_last_week=None,
            latest_post="",
            passes_frequency=None,
            verdict=UNKNOWN,
            verdict_reason="post feed unavailable — recency could not be checked",
        )
        return result

    recent = count_posts_since(timestamps, cutoff_epoch)
    result["posts_last_week"] = recent
    result["latest_post"] = (
        datetime.fromtimestamp(timestamps[0], timezone.utc).isoformat()
        if timestamps
        else ""
    )
    result["passes_frequency"] = recent >= min_posts

    if result["passes_followers"] and result["passes_frequency"]:
        result["verdict"] = QUALIFIED
        result["verdict_reason"] = (
            f"{followers:,} followers, {recent} posts in the last {window_days}d"
        )
        return result

    reasons = []
    if not result["passes_followers"]:
        bound = "limit" if followers >= max_followers else "floor"
        limit = max_followers if followers >= max_followers else min_followers
        reasons.append(f"{followers:,} followers ({bound} {limit:,})")
    if not result["passes_frequency"]:
        reasons.append(f"only {recent} posts in the last {window_days}d (need {min_posts})")

    result["verdict"] = REJECTED
    result["verdict_reason"] = "; ".join(reasons)
    return result


def evaluate_all(records, **kwargs):
    """Annotate a list of records and split it into the three buckets."""
    judged = [evaluate(r, **kwargs) for r in records]
    return {
        QUALIFIED: [r for r in judged if r["verdict"] == QUALIFIED],
        REJECTED: [r for r in judged if r["verdict"] == REJECTED],
        UNKNOWN: [r for r in judged if r["verdict"] == UNKNOWN],
    }
