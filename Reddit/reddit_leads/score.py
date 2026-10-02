"""Lead ranking.

A good lead is: clearly AI work, clearly someone with budget, clearly remote,
posted recently, and written like a real person rather than a spam drop.
"""

import re

from . import config, filters

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
CONTACT_RE = re.compile(
    r"(telegram|discord|whatsapp|calendly|linkedin\.com/\S+|t\.me/\S+|@[\w.]{3,})",
    re.IGNORECASE,
)


def contact_hint(post, body):
    """Where to reach the poster, preferring anything explicit in the body."""
    email = EMAIL_RE.search(body or "")
    if email:
        return email.group(0)
    handle = CONTACT_RE.search(body or "")
    if handle:
        return handle.group(0)
    lowered = filters.normalize(body)
    if filters.find_terms(lowered, ["dm me", "pm me", "send me a dm", "message me"]):
        return "DM u/%s" % post.get("author", "")
    return "Comment on post / DM u/%s" % post.get("author", "")


def score_lead(post, text, max_age_days=config.DEFAULT_MAX_AGE_DAYS, now=None):
    """Return (score, reasons) for a post that already passed the filters."""
    reasons = []
    total = 0.0

    ai_points, strong, weak = filters.ai_relevance(text)
    ai_points = min(ai_points, 12)
    total += ai_points
    if strong:
        reasons.append("AI: " + ", ".join(strong[:4]))
    elif weak:
        reasons.append("AI-adjacent: " + ", ".join(weak[:3]))

    title = filters.normalize(post.get("title"))
    if filters.has_hiring_tag(title):
        total += 5
        reasons.append("[Hiring]-tagged title")
    elif filters.demand_signals(title):
        total += 4
        reasons.append("hiring intent in title")
    else:
        total += 2
        reasons.append("hiring intent in body")

    # Several independent ways of saying "I am hiring" beats one stray phrase.
    demand = filters.demand_signals(text)
    total += min(len(demand), 3)
    if len(demand) > 1:
        reasons.append("%d demand signals" % len(demand))

    pay = filters.extract_pay(post.get("selftext", "") + " " + post.get("title", ""))
    if pay:
        total += 4
        reasons.append("budget stated (%s)" % pay)

    # Recency: a post from today is worth noticeably more than a 13-day-old one.
    age = filters.age_days(post.get("created_utc", 0), now)
    freshness = max(0.0, (max_age_days - age) / max_age_days)
    total += freshness * 5
    reasons.append("%.1f days old" % age)

    if filters.remote_signals(title):
        total += 2
        reasons.append("remote stated in title")

    body_length = len(post.get("selftext") or "")
    if body_length > 400:
        total += 2
        reasons.append("detailed post")
    elif body_length < 80:
        total -= 2
        reasons.append("very short post")

    # Engagement is weak evidence that the post is real and being answered.
    total += min(float(post.get("num_comments") or 0) * 0.1, 2.0)

    return round(total, 2), reasons
