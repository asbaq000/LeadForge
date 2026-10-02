"""Standalone Upwork job scraper.

Fetches public Upwork job listings via their GraphQL API using curl_cffi with
Chrome TLS fingerprint impersonation. No database, no API server — just the
scraping core.

Typical use:

    from upwork_scraper import UpworkScraper

    scraper = UpworkScraper()
    jobs = scraper.scrape(max_pages=2)
    for job in jobs:
        print(job.title, job.link)
"""

from upwork_scraper.auth.token_manager import TokenManager
from upwork_scraper.country_filter import (
    DEFAULT_EXCLUDED_COUNTRIES,
    CountryFilter,
    normalize_country,
    parse_country_list,
)
from upwork_scraper.enrich import ClientEnricher, CircuitBreaker, HumanDelay
from upwork_scraper.errors import TokenExpired, TokenFetchFailed
from upwork_scraper.log_config import init_logger
from upwork_scraper.models.client_models import ClientInfo
from upwork_scraper.models.job_models import Job, JobList
from upwork_scraper.models.proxy_models import ProxyConfig
from upwork_scraper.niches import Niche, available_niches, load_niche
from upwork_scraper.proxies.proxy_manager import (
    NoProxyManager,
    WebshareProxyManager,
    build_proxy_manager,
)
from upwork_scraper.scraper import UpworkScraper
from upwork_scraper.scrapers.job_fetcher import fetch_all_jobs, fetch_jobs_page

__version__ = "0.1.0"

__all__ = [
    "CircuitBreaker",
    "ClientEnricher",
    "ClientInfo",
    "CountryFilter",
    "DEFAULT_EXCLUDED_COUNTRIES",
    "HumanDelay",
    "Job",
    "JobList",
    "Niche",
    "NoProxyManager",
    "ProxyConfig",
    "TokenExpired",
    "TokenFetchFailed",
    "TokenManager",
    "available_niches",
    "UpworkScraper",
    "WebshareProxyManager",
    "build_proxy_manager",
    "fetch_all_jobs",
    "fetch_jobs_page",
    "init_logger",
    "load_niche",
    "normalize_country",
    "parse_country_list",
]
