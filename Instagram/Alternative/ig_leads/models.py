"""Data shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class Candidate:
    """An account seen posting under a niche hashtag, before any profile call."""

    username: str
    niche: str = ""
    hashtag: str = ""
    is_private: bool | None = None
    is_verified: bool | None = None
    seen_post_at: datetime | None = None


@dataclass(slots=True)
class Account:
    """A profile that has actually been looked up."""

    username: str
    full_name: str = ""
    biography: str = ""
    followers: int = 0
    following: int = 0
    total_posts: int = 0
    is_private: bool = False
    is_verified: bool = False
    is_business: bool = False
    is_professional: bool = False
    category: str = ""
    external_url: str = ""
    user_id: str = ""

    # Filled by the post-cadence check; None means "not looked up yet".
    recent_post_dates: list[datetime] = field(default_factory=list)
    posts_in_window: int | None = None

    # True when the counts came from the page fallback, where Instagram
    # rounds them ("14K"). Fine for a 1k-500k band, not for a boundary.
    approximate_counts: bool = False

    # Discovery provenance
    niche: str = ""
    hashtag: str = ""

    # Verdict
    verdict: str = "pending"
    reasons: list[str] = field(default_factory=list)
    reject_reason: str = ""

    @property
    def profile_url(self) -> str:
        return f"https://www.instagram.com/{self.username}/"

    @property
    def is_lead(self) -> bool:
        return self.verdict == "lead"

    @property
    def days_since_last_post(self) -> float | None:
        if not self.recent_post_dates:
            return None
        newest = max(self.recent_post_dates)
        return (datetime.now(timezone.utc) - newest).total_seconds() / 86400

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "profile_url": self.profile_url,
            "full_name": self.full_name,
            "followers": self.followers,
            "followers_approximate": self.approximate_counts,
            "following": self.following,
            "total_posts": self.total_posts,
            "posts_last_14d": self.posts_in_window,
            "days_since_last_post": (
                round(self.days_since_last_post, 1)
                if self.days_since_last_post is not None else None
            ),
            "category": self.category,
            "is_business": self.is_business,
            "is_verified": self.is_verified,
            "is_private": self.is_private,
            "biography": " ".join((self.biography or "").split()),
            "external_url": self.external_url,
            "niche": self.niche,
            "hashtag": self.hashtag,
            "verdict": self.verdict,
            "reasons": "; ".join(self.reasons),
            "reject_reason": self.reject_reason,
        }


CSV_FIELDS = [
    "username", "profile_url", "full_name", "followers",
    "followers_approximate", "following",
    "total_posts", "posts_last_14d", "days_since_last_post", "category",
    "is_business", "is_verified", "is_private", "biography", "external_url",
    "niche", "hashtag", "verdict", "reasons", "reject_reason",
]
