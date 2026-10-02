import logging
from datetime import datetime, timedelta

from curl_cffi import requests

from upwork_scraper.errors import TokenFetchFailed

LOGGER = logging.getLogger(__name__)

UPWORK_URL = "https://www.upwork.com/"
TOKEN_COOKIE_NAME = "visitor_gql_token"
TOKEN_TTL = timedelta(minutes=25)
MAX_RETRIES = 3


def _fetch_token(proxy_dict: dict | None = None) -> str:
    """Fetch visitor token by hitting Upwork main page with curl_cffi."""
    LOGGER.info("Fetching visitor token via curl_cffi...")
    resp = requests.get(
        UPWORK_URL,
        impersonate="chrome",
        proxies=proxy_dict,
        timeout=30,
    )

    # A 403 through a proxy almost always means the proxy's IP range is
    # blocked, not that anything is wrong with the request. Datacenter proxies
    # (Webshare's standard plan) are blocked by Upwork; residential ones are
    # not. Say so rather than burning three retries on a generic error.
    if resp.status_code == 403 and proxy_dict:
        raise TokenFetchFailed(
            "Upwork returned 403 through the proxy. Datacenter proxy IPs are "
            "blocked — run `--check-proxies` to test them, use `--no-proxy` to "
            "go direct, or switch to residential proxies."
        )

    resp.raise_for_status()

    token = resp.cookies.get(TOKEN_COOKIE_NAME)
    if not token:
        raise TokenFetchFailed(
            f"Cookie '{TOKEN_COOKIE_NAME}' not found. "
            f"Got cookies: {list(resp.cookies.keys())}"
        )

    LOGGER.info("Visitor token obtained")
    return token


class TokenManager:

    def __init__(self):
        self._token: str | None = None
        self._fetched_at: datetime | None = None

    def get_token(
        self, proxy_dict: dict | None = None, force_refresh: bool = False
    ) -> str:
        if not force_refresh and self._token and self._is_valid():
            return self._token

        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._token = _fetch_token(proxy_dict)
                self._fetched_at = datetime.now()
                return self._token
            except Exception as e:
                last_err = e
                LOGGER.warning(
                    "Token fetch attempt %d/%d failed: %s",
                    attempt, MAX_RETRIES, e,
                )

        raise TokenFetchFailed(
            f"Failed to fetch token after {MAX_RETRIES} attempts"
        ) from last_err

    def invalidate(self):
        self._token = None
        self._fetched_at = None

    def _is_valid(self) -> bool:
        if not self._fetched_at:
            return False
        return datetime.now() - self._fetched_at < TOKEN_TTL
