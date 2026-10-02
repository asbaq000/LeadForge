"""Configuration for the standalone scraper.

Everything is optional — importing this module never raises. Values come from
the environment (or a `.env` file), and each one has a working default so the
scraper can run with no configuration at all.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Proxies — leave both unset to scrape from your own IP.
# WEBSHARE_API_KEY is the key from the Webshare dashboard; WEBSHARE_URL is the
# older "download link" form. The API key wins when both are set.
WEBSHARE_API_KEY = os.getenv("WEBSHARE_API_KEY") or None
WEBSHARE_URL = os.getenv("WEBSHARE_URL") or None

# Scraping
MAX_PAGES = int(os.getenv("MAX_PAGES", "3"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "50"))
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "120"))

# Country filter — jobs whose poster is in one of these are dropped.
# Unset uses the built-in default (India, Pakistan, Bangladesh, Egypt,
# Philippines); a comma-separated list replaces it; "none" turns it off.
# The country itself comes from enrichment, so this only bites with
# --enrich-clients. See upwork_scraper/country_filter.py.
EXCLUDED_COUNTRIES = os.getenv("EXCLUDED_COUNTRIES")

# Client enrichment (separate stage — see upwork_scraper/enrich/)
# Full `Cookie:` header from a logged-in browser session. Without it the
# enrichment stage cannot fetch anything; job scraping is unaffected.
UPWORK_COOKIE = os.getenv("UPWORK_COOKIE") or None
ENRICH_MIN_DELAY = float(os.getenv("ENRICH_MIN_DELAY", "4"))
ENRICH_MAX_DELAY = float(os.getenv("ENRICH_MAX_DELAY", "11"))
