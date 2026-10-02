"""The facade: niche in, scored leads out.

    from x_leads import XLeadScraper
    leads = await XLeadScraper().run()

Everything the CLI does is available here, so the scraper can be embedded in
something bigger (a scheduler, a CRM sync) without going through argparse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from x_leads.auth.session import SessionManager, ensure_session
from x_leads.leads.classifier import LeadClassifier, Thresholds
from x_leads.leads.location import LocationFilter
from x_leads.models import Lead
from x_leads.niches import DEFAULT_NICHE, Niche, load_niche
from x_leads.search.collector import Collector, CollectStats
from x_leads.search.query import build_queries
from x_leads.state import SeenStore

LOGGER = logging.getLogger(__name__)

VERDICT_RANK = {"rejected": 0, "cold": 1, "warm": 2, "hot": 3}


@dataclass
class ScrapeConfig:
    niche: str = DEFAULT_NICHE
    hours: float = 48.0
    max_queries: int | None = None
    max_scrolls: int = 12
    max_tweets_per_query: int = 400
    concurrency: int = 2
    headless: bool = True
    min_verdict: str = "cold"
    min_followers: int = 0
    include_rejected: bool = False
    # On by default: the everyday question is "what came in since last time?",
    # not "show me the same twenty posts again". `--include-seen` turns it off.
    skip_seen: bool = True
    allow_login: bool = True
    login_timeout_s: int = 300
    verify_session: bool = True
    thresholds: Thresholds = field(default_factory=Thresholds)
    # Report-only by default, so a first run measures how many X authors can be
    # placed at all before anything is thrown away on the strength of it.
    location_filter: LocationFilter = field(default_factory=LocationFilter.default)


@dataclass
class ScrapeResult:
    leads: list[Lead]
    stats: CollectStats
    queries: list[str]
    tweets_collected: int = 0
    classified_out: int = 0
    suppressed_as_seen: int = 0
    newly_seen: int = 0
    # Coverage and per-country counts from the location pass. Populated in
    # every mode, including report-only, since measuring is the point of it.
    location_audit: dict = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for lead in self.leads:
            out[lead.verdict] = out.get(lead.verdict, 0) + 1
        return out


class XLeadScraper:
    def __init__(self, config: ScrapeConfig | None = None, seen: SeenStore | None = None):
        self.config = config or ScrapeConfig()
        self.seen = seen

    def build_queries(self, niche: Niche | None = None) -> list[str]:
        niche = niche or load_niche(self.config.niche)
        queries = build_queries(
            niche.phrases,
            lang=niche.language,
            # `since:` is date-granular, so an hours window is converted up to
            # whole days. It only ever over-fetches; the exact cutoff is
            # enforced per tweet by the collector.
            days=max(1, int(self.config.hours // 24) + 1),
            extra_operators=niche.extra_operators,
        )
        if self.config.max_queries:
            queries = queries[: self.config.max_queries]
        return queries

    async def run(self) -> ScrapeResult:
        cfg = self.config
        niche = load_niche(cfg.niche)
        queries = self.build_queries(niche)

        LOGGER.info(
            "Niche '%s': %d phrases packed into %d queries, last %g hours",
            niche.name, len(niche.phrases), len(queries), cfg.hours,
        )

        storage_state = await ensure_session(
            SessionManager(),
            allow_login=cfg.allow_login,
            login_timeout_s=cfg.login_timeout_s,
            verify_saved=cfg.verify_session,
        )

        async with Collector(
            storage_state,
            max_age_hours=cfg.hours,
            max_scrolls=cfg.max_scrolls,
            max_tweets_per_query=cfg.max_tweets_per_query,
            concurrency=cfg.concurrency,
            headless=cfg.headless,
        ) as collector:
            tweets = await collector.run_queries(queries)
            stats = collector.stats

        LOGGER.info("Collected %d unique tweets — classifying", len(tweets))

        classifier = LeadClassifier(
            thresholds=cfg.thresholds,
            min_followers=cfg.min_followers,
            niche=niche.name,
        )
        leads = classifier.classify_all(tweets)

        # Before the verdict floor, so a location rejection is enforced by the
        # same filter as every other rejection — and stays visible under
        # `--include-rejected` with its reason, instead of vanishing silently.
        audit = cfg.location_filter.annotate(leads)

        result = ScrapeResult(
            leads=leads,
            stats=stats,
            queries=queries,
            tweets_collected=len(tweets),
            location_audit=audit,
        )

        floor = VERDICT_RANK.get(cfg.min_verdict, 1)
        kept = [
            lead for lead in leads
            if cfg.include_rejected or VERDICT_RANK[lead.verdict] >= max(floor, 1)
        ]
        # Location drops are reported on their own line, so they are excluded
        # here rather than being lumped in with "sellers, noise or off-topic".
        result.classified_out = max(0, len(leads) - len(kept) - audit.get("dropped", 0))

        if cfg.skip_seen and self.seen is not None:
            before = len(kept)
            kept = self.seen.filter_new(kept)
            result.suppressed_as_seen = before - len(kept)

        # Only what was actually shown gets marked seen. Recording every lead
        # here instead would mean a single `--min-verdict warm` run silently
        # buried that day's cold leads forever — they would be "already
        # reported" on the next run without ever having been reported.
        if self.seen is not None:
            result.newly_seen = self.seen.add_all(
                lead.tweet.tweet_id for lead in kept
            )

        result.leads = kept
        return result
