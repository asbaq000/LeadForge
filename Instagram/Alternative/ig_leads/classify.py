"""Deciding whether an account is worth pitching to.

The hard filters are arithmetic and come straight from the brief: 1k-500k
followers, at least 5 posts in the last 14 days, not private.

The judgement call is the same one the X scraper had to make, in mirror image.
There, the noise was editors advertising *to* buyers. Here, the noise is
editors and agencies showing up under creator hashtags — they post to
#contentcreator and #videoediting all day, they have the right follower counts,
and they post constantly. They are competitors, and pitching editing to a
video editor wastes the outreach.

The distinction that works is **subject versus service**:

    a creator posts ABOUT something   wildlife, a car restoration, a house
    a supplier posts ABOUT the craft   "edits for creators", "DM for reels"

So a wildlife filmmaker is a lead even though they film for a living, while
"video editor | I edit your reels" is not. `SERVICE_PATTERNS` matches the offer
of a service, not the possession of a skill — "filmmaker" is fine, "editing
services" is not.
"""

from __future__ import annotations

import re
import unicodedata

from ig_leads.config import Filters
from ig_leads.models import Account

_FLAGS = re.IGNORECASE | re.VERBOSE


def normalise(text: str) -> str:
    """NFKC-folded lowercase. Instagram bios are full of styled unicode."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.replace("’", "'").split()).lower()


def _compile(patterns: dict[str, str]) -> list[tuple[str, re.Pattern]]:
    return [(name, re.compile(p, _FLAGS)) for name, p in patterns.items()]


# Someone selling editing, design or social-media production. A competitor.
SERVICE_PATTERNS = _compile({
    "sells:editing": r"""\b(?: video \s* editor | videoeditor | editing \s+ services?
                             | edits? \s+ for \s+ (?:you|your|creators?|brands?|clients?)
                             | we \s+ edit | i \s+ edit \s+ (?:your|videos?|reels?)
                             | dm \s+ (?:me \s+)? for \s+ (?:edits?|editing|reels?|videos?)
                             | reels? \s+ editor | shorts? \s+ editor
                             | post[- ]production \s+ (?:services?|studio)
                             | thumbnail \s+ designer )""",
    "sells:agency": r"""\b(?: (?:video|content|media|marketing|social) \s+ agency
                            | agency \s+ owner | smma
                            | we \s+ help \s+ (?:creators?|brands?|coaches|businesses)
                            | scaling \s+ (?:creators?|brands?)
                            | done[- ]for[- ]you )""",
    "sells:freelance": r"""\b(?: freelance \s+ (?:editor|designer|videographer)
                               | available \s+ for \s+ (?:work|hire|projects?)
                               | open \s+ for \s+ (?:work|commissions?|collabs?)
                               | hire \s+ me | book \s+ (?:me|a \s+ call)
                               | portfolio \s* (?::|👉|->) )""",
})

# Not a person or a brand we can pitch: aggregators, fan pages, shops, bots.
JUNK_PATTERNS = _compile({
    "junk:fanpage": r"""\b(?: fan \s* (?:page|account|club) | not \s+ affiliated
                            | daily \s+ (?:posts?|updates?) \s+ of
                            | backup \s+ account | parody )""",
    "junk:store": r"""\b(?: shop \s+ now | free \s+ shipping | worldwide \s+ shipping
                          | order \s+ (?:now|via|on \s+ whatsapp) | dm \s+ to \s+ order
                          | \b sale \b .{0,15} \b off \b )""",
    "junk:growth": r"""\b(?: follow \s+ for \s+ follow | f4f | l4l
                           | grow \s+ your \s+ (?:account|instagram|followers)
                           | buy \s+ followers | telegram \s+ channel )""",
})

# Positive signals that this is a genuine content operation. None of these are
# required - the hard filters already did the real work - but they rank the
# output so the best accounts to contact are at the top.
CREATOR_PATTERNS = _compile({
    "creator:youtube": r"""\b(?: youtube | yt \b | new \s+ video | subscribe
                               | link \s+ in \s+ bio | watch \s+ now
                               | full \s+ video | on \s+ my \s+ channel )""",
    "creator:selfdescribed": r"""\b(?: content \s+ creator | creator \b | vlogger
                                     | youtuber | film \s* maker | documentar
                                     | storyteller | host \b | presenter )""",
    "creator:craft": r"""\b(?: restoration | restoring | building | build \s+ series
                            | woodwork | workshop | studio | homestead | vlog
                            | photograph | cinematograph | archive | history
                            | wildlife | adventure | travel )""",
})

# Instagram's own self-declared category. Cleaner than bio prose because the
# account picked it from a fixed list.
CREATOR_CATEGORIES = {
    "digital creator", "content creator", "video creator", "blogger", "vlogger",
    "public figure", "artist", "musician/band", "photographer", "writer",
    "journalist", "entrepreneur", "personal blog", "podcast", "gamer",
    "film director", "filmmaker", "producer", "author", "athlete",
    "travel company", "media/news company", "tv show", "magazine",
}
SUPPLIER_CATEGORIES = {
    "graphic designer", "advertising/marketing", "marketing agency",
    "media agency", "designer", "editor",
}

# Bio links that all but prove a content operation off-platform.
CREATOR_LINK = re.compile(
    r"(youtube\.com|youtu\.be|linktr\.ee|beacons\.|stan\.store|patreon\.com|"
    r"substack\.com|tiktok\.com|twitch\.tv|open\.spotify)",
    re.IGNORECASE,
)


def _hits(text: str, rules) -> list[str]:
    return [name for name, pattern in rules if pattern.search(text)]


def classify(account: Account, filters: Filters | None = None) -> Account:
    """Set `verdict`, `reasons` and `reject_reason` on the account.

    Hard filters run first and in cheapest-first order, so the expensive
    judgement only happens for accounts that could actually qualify.
    """
    f = filters or Filters()
    bio = normalise(account.biography)
    name = normalise(account.full_name)
    category = normalise(account.category)
    blob = f"{bio} {name}"

    if account.is_private and f.skip_private:
        account.verdict = "rejected"
        account.reject_reason = "private account"
        return account

    if account.followers < f.min_followers:
        account.verdict = "rejected"
        account.reject_reason = f"{account.followers:,} followers (min {f.min_followers:,})"
        return account

    if account.followers > f.max_followers:
        account.verdict = "rejected"
        account.reject_reason = f"{account.followers:,} followers (max {f.max_followers:,})"
        return account

    # None means the cadence check never ran; that is not the same as failing it.
    if account.posts_in_window is None:
        account.verdict = "rejected"
        account.reject_reason = "post history not checked"
        return account

    if account.posts_in_window < f.min_recent_posts:
        account.verdict = "rejected"
        account.reject_reason = (
            f"{account.posts_in_window} posts in {f.recent_days}d "
            f"(min {f.min_recent_posts})"
        )
        return account

    service = _hits(blob, SERVICE_PATTERNS)
    junk = _hits(blob, JUNK_PATTERNS)

    if category in SUPPLIER_CATEGORIES:
        service.append(f"category:{category}")

    if service:
        account.verdict = "rejected"
        account.reject_reason = f"sells the service ({service[0]})"
        account.reasons = service
        return account

    if junk:
        account.verdict = "rejected"
        account.reject_reason = f"not a creator account ({junk[0]})"
        account.reasons = junk
        return account

    reasons = _hits(blob, CREATOR_PATTERNS)
    if category in CREATOR_CATEGORIES:
        reasons.append(f"category:{category}")
    if account.external_url and CREATOR_LINK.search(account.external_url):
        reasons.append("link:offplatform-channel")
    if account.is_professional or account.is_business:
        reasons.append("professional account")
    if account.posts_in_window >= f.min_recent_posts * 2:
        reasons.append("posts very frequently")

    account.verdict = "lead"
    account.reasons = reasons
    return account


def rank_key(account: Account) -> tuple:
    """Best-first ordering for the output.

    More corroborating signals first, then posting cadence, then followers.
    Followers rank last on purpose: within a 1k-500k band, an account that
    posts ten times a fortnight is a better prospect than one with more
    followers that posts twice.
    """
    return (
        len(account.reasons),
        account.posts_in_window or 0,
        account.followers,
    )
