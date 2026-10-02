"""Separate enrichment stage: client details for already-scraped jobs.

Kept apart from the job scraper on purpose — jobs are fetched first and in
full, then this walks the results. Nothing here can slow down or break a
job scrape.
"""

from upwork_scraper.enrich.browser_fetcher import (
    DEFAULT_PROFILE_DIR,
    INSTALL_HELP,
    LOGIN_PROFILE_DIR,
    profile_for,
    BrowserFetcher,
    PlaywrightFetcher,
    build_browser_fetcher,
)
from upwork_scraper.enrich.client_fetcher import (
    BROWSER_REQUIRED_HELP,
    ClientEnricher,
    parse_client_info,
)
from upwork_scraper.enrich.throttle import CircuitBreaker, HumanDelay

__all__ = [
    "BROWSER_REQUIRED_HELP",
    "BrowserFetcher",
    "DEFAULT_PROFILE_DIR",
    "INSTALL_HELP",
    "LOGIN_PROFILE_DIR",
    "profile_for",
    "PlaywrightFetcher",
    "build_browser_fetcher",
    "CircuitBreaker",
    "ClientEnricher",
    "HumanDelay",
    "parse_client_info",
]
