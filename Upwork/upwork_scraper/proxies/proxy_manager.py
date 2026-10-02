import logging
import random
import threading
import time

from curl_cffi import requests as cffi_requests

from upwork_scraper import config
from upwork_scraper.models.proxy_models import ProxyConfig

LOGGER = logging.getLogger(__name__)

PROXY_REFRESH_INTERVAL = 3600  # Reload proxy list every hour


class ProxyManager:
    """Interface every proxy source implements: hand out one proxy per call."""

    def get_proxy(self) -> ProxyConfig | None:
        raise NotImplementedError


class NoProxyManager(ProxyManager):
    """Direct connection — no proxy. Used when WEBSHARE_URL is not configured."""

    def get_proxy(self) -> None:
        return None


WEBSHARE_API_URL = "https://proxy.webshare.io/api/v2/proxy/list/"


class WebshareProxyManager(ProxyManager):
    """Loads a Webshare proxy list, either by API key or by download URL.

    The API key is the friendlier option — it is the same key shown in the
    Webshare dashboard, needs no separate download link, and reports which
    proxies Webshare currently considers valid.
    """

    def __init__(
        self, webshare_url: str | None = None, api_key: str | None = None
    ):
        # An explicit argument wins outright. Falling back per-field would let
        # an env API key silently override a URL the caller passed on purpose
        # (and would make tests depend on whatever .env happens to hold).
        if webshare_url is not None or api_key is not None:
            self._api_key = api_key
            self._webshare_url = webshare_url
        else:
            self._api_key = config.WEBSHARE_API_KEY
            self._webshare_url = config.WEBSHARE_URL

        if not self._api_key and not self._webshare_url:
            raise ValueError(
                "No Webshare credentials — set WEBSHARE_API_KEY or WEBSHARE_URL, "
                "or use NoProxyManager"
            )
        self._lock = threading.Lock()
        self._proxies: list[ProxyConfig] = []
        self._last_loaded: float = 0
        self.load_proxies()

    def _fetch_from_api(self) -> list[ProxyConfig]:
        resp = cffi_requests.get(
            WEBSHARE_API_URL,
            params={"mode": "direct", "page": 1, "page_size": 100},
            headers={"Authorization": f"Token {self._api_key}"},
            timeout=20,
        )
        resp.raise_for_status()

        proxies = []
        skipped = 0
        for entry in resp.json().get("results", []):
            # Webshare flags proxies it has failed to verify; using them just
            # burns retries.
            if not entry.get("valid", True):
                skipped += 1
                continue
            proxies.append(
                ProxyConfig(
                    host=entry["proxy_address"],
                    port=int(entry["port"]),
                    username=entry["username"],
                    password=entry["password"],
                    country=entry.get("country_code"),
                    city=entry.get("city_name"),
                )
            )
        if skipped:
            LOGGER.info("Skipped %d proxies Webshare reports as invalid", skipped)
        return proxies

    def _fetch_from_download_url(self) -> list[ProxyConfig]:
        resp = cffi_requests.get(self._webshare_url, timeout=15)
        resp.raise_for_status()

        proxies = []
        for line in resp.text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            host, port, username, password = line.split(":", 3)
            proxies.append(
                ProxyConfig(host=host, port=int(port), username=username, password=password)
            )
        return proxies

    def _fetch_proxy_list(self) -> list[ProxyConfig]:
        """Fetch the proxy list (no lock held — pure I/O)."""
        if self._api_key:
            return self._fetch_from_api()
        return self._fetch_from_download_url()

    def load_proxies(self):
        proxies = self._fetch_proxy_list()
        with self._lock:
            self._proxies = proxies
            self._last_loaded = time.monotonic()
        LOGGER.info("Loaded %d proxies from Webshare", len(proxies))

    def get_proxy(self) -> ProxyConfig:
        if time.monotonic() - self._last_loaded > PROXY_REFRESH_INTERVAL:
            with self._lock:
                # Double-check inside lock so only one thread refreshes
                if time.monotonic() - self._last_loaded > PROXY_REFRESH_INTERVAL:
                    try:
                        proxies = self._fetch_proxy_list()
                        self._proxies = proxies
                        self._last_loaded = time.monotonic()
                        LOGGER.info("Refreshed %d proxies from Webshare", len(proxies))
                    except Exception:
                        LOGGER.warning("Failed to refresh proxy list, using cached")
                        self._last_loaded = time.monotonic() - PROXY_REFRESH_INTERVAL + 300

        with self._lock:
            if not self._proxies:
                raise RuntimeError("No proxies available")
            return random.choice(self._proxies)


def build_proxy_manager(
    webshare_url: str | None = None, api_key: str | None = None
) -> ProxyManager:
    """Webshare manager when credentials exist, direct connection otherwise."""
    key = api_key or config.WEBSHARE_API_KEY
    url = webshare_url or config.WEBSHARE_URL
    if key or url:
        return WebshareProxyManager(webshare_url=url, api_key=key)
    LOGGER.info("No Webshare credentials set — scraping without a proxy")
    return NoProxyManager()


def check_proxies(manager: ProxyManager) -> list[tuple[ProxyConfig, bool, str]]:
    """Test every proxy against Upwork itself, not just an IP echo service.

    A proxy can reach the open internet and still be refused by Upwork —
    datacenter ranges usually are. Returns (proxy, ok, detail) per proxy.
    """
    proxies = getattr(manager, "_proxies", [])
    results = []

    for proxy in proxies:
        try:
            resp = cffi_requests.get(
                "https://www.upwork.com/",
                impersonate="chrome",
                proxies=proxy.to_curl_cffi_dict(),
                timeout=30,
            )
            has_token = bool(resp.cookies.get("visitor_gql_token"))
            ok = resp.status_code == 200 and has_token
            detail = f"HTTP {resp.status_code}" + ("" if has_token else ", no token")
        except Exception as e:
            ok, detail = False, type(e).__name__
        results.append((proxy, ok, detail))

    return results


def proxy_dict_for(proxy: ProxyConfig | None) -> dict | None:
    """curl_cffi `proxies=` argument for a proxy that may be absent."""
    return proxy.to_curl_cffi_dict() if proxy else None
