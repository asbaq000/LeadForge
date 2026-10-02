"""Discovery -> profile -> cadence -> verdict.

The ordering here is a budget decision, not a style one. Each account costs up
to two API calls, and the daily allowance is a few hundred, so the checks run
cheapest-first and each one that fails saves the calls after it:

    free      hashtag payload already says `is_private` -> drop
    1 call    profile: followers outside 1k-500k -> drop, no feed call
    2 calls   feed: fewer than 5 posts in 14 days -> drop
    free      bio/category: sells editing -> drop

Follower band before post cadence matters most: it is the filter that rejects
the largest share of candidates, and doing it first means the majority of
accounts never cost a second request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ig_leads.budget import RequestBudget
from ig_leads.classify import classify, rank_key
from ig_leads.client import IGClient, ProfileUnavailable
from ig_leads.config import Settings
from ig_leads.errors import AccountAtRisk, BudgetExhausted
from ig_leads.models import Account, Candidate
from ig_leads.niches import Niche
from ig_leads.parsing import (
    parse_hashtag_authors,
    parse_post_dates,
    parse_profile,
    parse_profile_page,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class RunStats:
    hashtags_searched: int = 0
    candidates_found: int = 0
    profiles_checked: int = 0
    feeds_checked: int = 0
    skipped_private_early: int = 0
    skipped_duplicate: int = 0
    # Accounts the profile API refused but the page fallback rescued.
    api_unavailable: int = 0
    unreadable: int = 0
    # Candidates dropped for free because they are already in the store.
    skipped_known_leads: int = 0
    stopped_early: str = ""

    def summary(self) -> str:
        return (
            f"{self.hashtags_searched} hashtags -> {self.candidates_found} candidates "
            f"-> {self.profiles_checked} profiles checked "
            f"({self.feeds_checked} post-history lookups)"
        )


@dataclass
class RunResult:
    leads: list[Account] = field(default_factory=list)
    rejected: list[Account] = field(default_factory=list)
    stats: RunStats = field(default_factory=RunStats)
    budget_summary: str = ""


class IGLeadScraper:
    def __init__(
        self,
        settings: Settings | None = None,
        budget: RequestBudget | None = None,
        known_leads: set[str] | None = None,
    ):
        self.settings = settings or Settings()
        p = self.settings.pacing
        self.budget = budget or RequestBudget(p.daily_requests, p.run_requests)
        # Lower-cased usernames already recorded as leads, skipped for free.
        self.known_leads = known_leads or set()

    async def run(
        self,
        storage_state: dict,
        niches: list[Niche],
        max_hashtags: int | None = None,
        max_accounts: int | None = None,
    ) -> RunResult:
        result = RunResult()
        f = self.settings.filters
        cutoff = datetime.now(timezone.utc) - timedelta(days=f.recent_days)

        # One hashtag per niche per run. Sweeping every variant of one niche
        # returns heavily overlapping accounts for the same cost as covering
        # more niches.
        plan: list[tuple[Niche, str]] = [(n, n.hashtags[0]) for n in niches if n.hashtags]
        if max_hashtags:
            plan = plan[:max_hashtags]

        # Pace for the size of THIS run, not for the worst case. Most accounts
        # cost 1-2 requests, so 1.5 is the realistic multiplier.
        planned = len(plan) + int((max_accounts or len(plan) * 30) * 1.5)
        pacing = self.settings.pacing.for_run(planned)
        if pacing is not self.settings.pacing:
            LOGGER.info(
                "Small run (~%d requests) — using %.0f-%.0fs gaps",
                planned, pacing.min_gap_s, pacing.max_gap_s,
            )

        async with IGClient(
            storage_state, self.budget, pacing, self.settings.headless
        ) as ig:
            try:
                candidates = await self._discover(ig, plan, result)
                await self._qualify(ig, candidates, result, cutoff, max_accounts)
            except AccountAtRisk as e:
                # Not an error to swallow: keep whatever was gathered, and make
                # very sure the reason is the headline.
                result.stats.stopped_early = str(e)
                LOGGER.error("STOPPED: %s", e)
            except BudgetExhausted as e:
                result.stats.stopped_early = str(e)
                LOGGER.warning("Stopped: %s", e)

        result.leads.sort(key=rank_key, reverse=True)
        result.budget_summary = self.budget.summary()
        return result

    async def _discover(
        self, ig: IGClient, plan: list[tuple[Niche, str]], result: RunResult
    ) -> list[Candidate]:
        seen: dict[str, Candidate] = {}
        for niche, tag in plan:
            payload = await ig.hashtag(tag)
            result.stats.hashtags_searched += 1
            found = parse_hashtag_authors(payload, niche.name, tag)
            new = 0
            for candidate in found:
                if candidate.username in seen:
                    result.stats.skipped_duplicate += 1
                    continue
                seen[candidate.username] = candidate
                new += 1
            LOGGER.info("  #%-28s %2d accounts (%d new)", tag, len(found), new)

        result.stats.candidates_found = len(seen)
        return list(seen.values())

    async def _qualify(
        self,
        ig: IGClient,
        candidates: list[Candidate],
        result: RunResult,
        cutoff: datetime,
        max_accounts: int | None,
    ):
        f = self.settings.filters

        # Both of these are free filters applied before any request is spent.
        # Known leads matter most: a hashtag returns much the same 30 accounts
        # each time, so without this a repeat run through a niche spends its
        # budget re-confirming leads already in the store.
        workable = []
        for c in candidates:
            if f.skip_private and c.is_private:
                result.stats.skipped_private_early += 1
                continue
            if self.known_leads and c.username.lower() in self.known_leads:
                result.stats.skipped_known_leads += 1
                continue
            workable.append(c)

        if max_accounts:
            workable = workable[:max_accounts]

        LOGGER.info(
            "Checking %d accounts (%d private, %d already-known leads "
            "skipped for free)",
            len(workable), result.stats.skipped_private_early,
            result.stats.skipped_known_leads,
        )

        for i, candidate in enumerate(workable, 1):
            # Stop before starting an account we cannot finish, so the run ends
            # on a whole result rather than a half-checked one.
            if self.budget.remaining < 2:
                result.stats.stopped_early = (
                    f"budget too low to check another account ({self.budget.summary()})"
                )
                LOGGER.warning("Stopping: %s", result.stats.stopped_early)
                break

            approximate = False
            try:
                payload = await ig.profile(candidate.username)
                account = parse_profile(payload, candidate.username)
            except ProfileUnavailable:
                # Instagram's own bug on this account, not a block. The public
                # page still works, so fall back rather than lose the lead —
                # this hit 2 of 6 accounts on the first live run.
                result.stats.api_unavailable += 1
                page = await ig.profile_via_page(candidate.username)
                account = parse_profile_page(page, candidate.username)
                approximate = account is not None
                if approximate:
                    account.reasons.append("counts approximate (page fallback)")

            result.stats.profiles_checked += 1
            if account is None:
                result.stats.unreadable += 1
                LOGGER.debug("  @%s: no profile data", candidate.username)
                continue

            account.niche = candidate.niche
            account.hashtag = candidate.hashtag
            account.approximate_counts = approximate

            # Follower band first — it rejects the most, and rejecting here
            # saves the feed request entirely.
            in_band = f.min_followers <= account.followers <= f.max_followers
            if in_band and not (account.is_private and f.skip_private):
                feed = await ig.recent_posts(account.username)
                result.stats.feeds_checked += 1
                dates = parse_post_dates(feed)
                account.recent_post_dates = dates
                account.posts_in_window = sum(1 for d in dates if d >= cutoff)

                # 12 posts is one page. If every one is inside the window the
                # true count is higher, but the answer to "at least 5?" is
                # already yes, so a second page would buy nothing.
                if dates and account.posts_in_window == len(dates) >= 12:
                    account.reasons.append("posts more than one page in the window")

            classify(account, f)
            (result.leads if account.is_lead else result.rejected).append(account)

            mark = "LEAD" if account.is_lead else "----"
            LOGGER.info(
                "  [%d/%d] %s @%-24s %7s followers  %s",
                i, len(workable), mark, account.username,
                f"{account.followers:,}",
                f"{account.posts_in_window} posts/14d" if account.posts_in_window is not None
                else account.reject_reason,
            )
