"""Data shapes for authors, tweets and scored leads.

Plain dataclasses rather than pydantic: the only untrusted input is X's
GraphQL payload, and `search/parser.py` already validates that field by field.
Keeping the dependency list to "playwright" alone makes this much easier to
deploy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class Author:
    handle: str = ""
    display_name: str = ""
    bio: str = ""
    location: str = ""
    followers: int = 0
    following: int = 0
    tweets: int = 0
    verified: bool = False
    blue_verified: bool = False
    # X's own self-declared profession, e.g. "Editor" or "Video Creator".
    # A far cleaner supply signal than anything guessable from the bio.
    professional_category: str = ""
    website: str = ""
    user_id: str = ""

    @property
    def profile_url(self) -> str:
        return f"https://x.com/{self.handle}" if self.handle else ""


@dataclass(slots=True)
class Tweet:
    tweet_id: str
    text: str
    created_at: datetime | None = None
    lang: str = ""
    author: Author = field(default_factory=Author)
    likes: int = 0
    replies: int = 0
    retweets: int = 0
    quotes: int = 0
    bookmarks: int = 0
    views: int = 0
    is_reply: bool = False
    is_quote: bool = False
    is_retweet: bool = False
    # Which generated search query surfaced it. Useful for pruning phrases that
    # only ever return junk.
    matched_queries: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        handle = self.author.handle or "i"
        return f"https://x.com/{handle}/status/{self.tweet_id}"

    @property
    def age_hours(self) -> float | None:
        if not self.created_at:
            return None
        delta = datetime.now(timezone.utc) - self.created_at
        return delta.total_seconds() / 3600


# Ordered worst to best so comparisons read naturally.
VERDICTS = ("rejected", "cold", "warm", "hot")


@dataclass(slots=True)
class Lead:
    """A tweet plus the classifier's reasoning about it."""

    tweet: Tweet
    score: int = 0
    verdict: str = "rejected"
    # Human-readable reasons, e.g. "demand:hiring", "supply:offers-own-services".
    # Emitted so a bad call can be traced to the rule that made it rather than
    # to a bare number.
    signals: list[str] = field(default_factory=list)
    reject_reason: str = ""
    # The money the post named, already formatted for reading: "$500/video".
    # Empty when the post never said — which is most of them.
    budget: str = ""
    # Which niche preset the run was searching. Constant within a run, but a
    # CSV outlives the run that made it and gets pasted next to another one.
    niche: str = ""
    # Canonical country for the author, and which signal produced it
    # ("location", "flag", "website", "bio-phone", "bio-mention"). Both empty
    # when the author could not be placed — the common case on X, where the
    # profile location field is optional and often blank. Filled by
    # `leads/location.py`; the source is carried so a run can be audited per
    # signal rather than only in aggregate.
    location_country: str = ""
    location_source: str = ""

    @property
    def is_lead(self) -> bool:
        return self.verdict != "rejected"

    def to_dict(self) -> dict[str, Any]:
        t = self.tweet
        return {
            "verdict": self.verdict,
            "score": self.score,
            "budget": self.budget,
            "niche": self.niche,
            "signals": self.signals,
            "reject_reason": self.reject_reason,
            "tweet_url": t.url,
            "text": t.text,
            "posted_at": t.created_at.isoformat() if t.created_at else None,
            "age_hours": round(t.age_hours, 1) if t.age_hours is not None else None,
            "handle": t.author.handle,
            "display_name": t.author.display_name,
            "profile_url": t.author.profile_url,
            "bio": t.author.bio,
            # The raw profile field alongside the country derived from it, so a
            # wrong call can be traced to the text that caused it.
            "location": t.author.location,
            "location_country": self.location_country,
            "location_source": self.location_source,
            "followers": t.author.followers,
            "verified": t.author.verified or t.author.blue_verified,
            "professional_category": t.author.professional_category,
            "website": t.author.website,
            "likes": t.likes,
            "replies": t.replies,
            "views": t.views,
            "matched_queries": t.matched_queries,
        }


def to_json_dict(obj: Any) -> dict:
    return asdict(obj)
