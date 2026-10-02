from unittest.mock import MagicMock, patch

import pytest

from upwork_scraper.models.proxy_models import ProxyConfig


class TestProxyConfig:

    def test_to_proxy_url(self):
        proxy = ProxyConfig(host="1.2.3.4", port=8080, username="user", password="pass")
        assert proxy.to_proxy_url() == "http://user:pass@1.2.3.4:8080"

    def test_to_curl_cffi_dict(self):
        proxy = ProxyConfig(host="1.2.3.4", port=8080, username="user", password="pass")
        d = proxy.to_curl_cffi_dict()
        assert d == {
            "http": "http://user:pass@1.2.3.4:8080",
            "https": "http://user:pass@1.2.3.4:8080",
        }

    def test_special_chars_in_password(self):
        proxy = ProxyConfig(host="proxy.io", port=3128, username="u", password="p@ss:word")
        assert proxy.to_proxy_url() == "http://u:p@ss:word@proxy.io:3128"


PROXY_RESPONSE = """
1.2.3.4:8080:user1:pass1
5.6.7.8:9090:user2:pass2

10.0.0.1:3128:user3:pass3
"""


class TestWebshareProxyManager:

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_load_proxies_parses_lines(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.text = PROXY_RESPONSE
        mock_requests.get.return_value = mock_resp

        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager
        mgr = WebshareProxyManager("https://webshare.test/list")

        assert len(mgr._proxies) == 3
        assert mgr._proxies[0].host == "1.2.3.4"
        assert mgr._proxies[0].port == 8080
        assert mgr._proxies[1].username == "user2"
        assert mgr._proxies[2].password == "pass3"

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_get_proxy_returns_valid_proxy(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.text = "1.2.3.4:8080:user:pass"
        mock_requests.get.return_value = mock_resp

        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager
        mgr = WebshareProxyManager("https://webshare.test/list")
        proxy = mgr.get_proxy()

        assert isinstance(proxy, ProxyConfig)
        assert proxy.host == "1.2.3.4"

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_skips_blank_lines(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.text = "\n\n1.2.3.4:8080:u:p\n\n"
        mock_requests.get.return_value = mock_resp

        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager
        mgr = WebshareProxyManager("https://webshare.test/list")

        assert len(mgr._proxies) == 1

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_raises_when_no_proxies(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.text = ""
        mock_requests.get.return_value = mock_resp

        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager
        mgr = WebshareProxyManager("https://webshare.test/list")

        with pytest.raises(RuntimeError, match="No proxies"):
            mgr.get_proxy()


class TestNoProxyAndBuilder:

    def test_no_proxy_manager_returns_none(self):
        from upwork_scraper.proxies.proxy_manager import NoProxyManager

        assert NoProxyManager().get_proxy() is None

    def test_proxy_dict_for_none(self):
        from upwork_scraper.proxies.proxy_manager import proxy_dict_for

        assert proxy_dict_for(None) is None

    def test_proxy_dict_for_proxy(self):
        from upwork_scraper.proxies.proxy_manager import proxy_dict_for

        proxy = ProxyConfig(host="1.2.3.4", port=80, username="u", password="p")
        assert proxy_dict_for(proxy) == proxy.to_curl_cffi_dict()

    @patch("upwork_scraper.proxies.proxy_manager.config")
    def test_builder_without_credentials_gives_no_proxy(self, mock_config):
        from upwork_scraper.proxies.proxy_manager import (
            NoProxyManager,
            build_proxy_manager,
        )

        mock_config.WEBSHARE_URL = None
        mock_config.WEBSHARE_API_KEY = None
        assert isinstance(build_proxy_manager(), NoProxyManager)

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_builder_with_url_gives_webshare(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import (
            WebshareProxyManager,
            build_proxy_manager,
        )

        mock_resp = MagicMock()
        mock_resp.text = "1.2.3.4:8080:u:p"
        mock_requests.get.return_value = mock_resp

        mgr = build_proxy_manager("https://webshare.test/list")
        assert isinstance(mgr, WebshareProxyManager)

    @patch("upwork_scraper.proxies.proxy_manager.config")
    def test_webshare_requires_some_credential(self, mock_config):
        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager

        mock_config.WEBSHARE_URL = None
        mock_config.WEBSHARE_API_KEY = None
        with pytest.raises(ValueError, match="WEBSHARE_API_KEY"):
            WebshareProxyManager()


API_RESPONSE = {
    "count": 3,
    "results": [
        {"proxy_address": "1.2.3.4", "port": 6754, "username": "u", "password": "p",
         "country_code": "GB", "city_name": "London", "valid": True},
        {"proxy_address": "5.6.7.8", "port": 7684, "username": "u", "password": "p",
         "country_code": "US", "city_name": "Seattle", "valid": True},
        {"proxy_address": "9.9.9.9", "port": 1000, "username": "u", "password": "p",
         "country_code": "JP", "city_name": "Tokyo", "valid": False},
    ],
}


class TestWebshareApiKey:

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_loads_proxies_from_the_api(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager

        mock_requests.get.return_value = MagicMock(
            **{"json.return_value": API_RESPONSE}
        )
        mgr = WebshareProxyManager(api_key="secret-key")

        assert [p.host for p in mgr._proxies] == ["1.2.3.4", "5.6.7.8"]
        assert mgr._proxies[0].port == 6754

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_sends_the_token_header(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager

        mock_requests.get.return_value = MagicMock(
            **{"json.return_value": API_RESPONSE}
        )
        WebshareProxyManager(api_key="secret-key")

        headers = mock_requests.get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Token secret-key"

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_skips_proxies_webshare_marks_invalid(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager

        mock_requests.get.return_value = MagicMock(
            **{"json.return_value": API_RESPONSE}
        )
        mgr = WebshareProxyManager(api_key="secret-key")

        assert "9.9.9.9" not in [p.host for p in mgr._proxies]

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_keeps_country_and_city(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager

        mock_requests.get.return_value = MagicMock(
            **{"json.return_value": API_RESPONSE}
        )
        mgr = WebshareProxyManager(api_key="secret-key")

        assert (mgr._proxies[0].country, mgr._proxies[0].city) == ("GB", "London")
        assert mgr._proxies[0].label == "1.2.3.4:6754 (London, GB)"

    def test_label_never_leaks_the_password(self):
        proxy = ProxyConfig(host="1.2.3.4", port=80, username="u", password="s3cret")

        assert "s3cret" not in proxy.label

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_explicit_url_is_not_overridden_by_an_env_api_key(self, mock_requests):
        """An explicit argument must win over whatever .env holds."""
        from upwork_scraper.proxies.proxy_manager import WebshareProxyManager

        mock_requests.get.return_value = MagicMock(text="1.2.3.4:8080:u:p")
        mgr = WebshareProxyManager("https://webshare.test/list")

        assert mgr._api_key is None
        assert mgr._proxies[0].host == "1.2.3.4"

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_api_key_builds_a_webshare_manager(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import (
            WebshareProxyManager,
            build_proxy_manager,
        )

        mock_requests.get.return_value = MagicMock(
            **{"json.return_value": API_RESPONSE}
        )
        assert isinstance(build_proxy_manager(api_key="k"), WebshareProxyManager)


class TestProxyHealthCheck:
    """A proxy that reaches the internet may still be refused by Upwork."""

    def _manager(self, proxies):
        mgr = MagicMock()
        mgr._proxies = proxies
        return mgr

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_working_proxy_reports_ok(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import check_proxies

        mock_requests.get.return_value = MagicMock(
            status_code=200, cookies={"visitor_gql_token": "abc"}
        )
        proxy = ProxyConfig(host="1.2.3.4", port=80, username="u", password="p")

        (_, ok, detail), = check_proxies(self._manager([proxy]))

        assert ok is True
        assert "200" in detail

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_403_is_reported_blocked(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import check_proxies

        mock_requests.get.return_value = MagicMock(status_code=403, cookies={})
        proxy = ProxyConfig(host="1.2.3.4", port=80, username="u", password="p")

        (_, ok, detail), = check_proxies(self._manager([proxy]))

        assert ok is False
        assert "403" in detail

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_200_without_a_token_is_not_ok(self, mock_requests):
        """Cloudflare interstitials return 200 with no visitor token."""
        from upwork_scraper.proxies.proxy_manager import check_proxies

        mock_requests.get.return_value = MagicMock(status_code=200, cookies={})
        proxy = ProxyConfig(host="1.2.3.4", port=80, username="u", password="p")

        (_, ok, detail), = check_proxies(self._manager([proxy]))

        assert ok is False
        assert "no token" in detail

    @patch("upwork_scraper.proxies.proxy_manager.cffi_requests")
    def test_connection_error_is_reported_not_raised(self, mock_requests):
        from upwork_scraper.proxies.proxy_manager import check_proxies

        mock_requests.get.side_effect = ConnectionError("dead")
        proxy = ProxyConfig(host="1.2.3.4", port=80, username="u", password="p")

        (_, ok, detail), = check_proxies(self._manager([proxy]))

        assert ok is False
        assert detail == "ConnectionError"


class TestProxyBlockedMessage:

    @patch("upwork_scraper.auth.token_manager.requests")
    def test_403_through_a_proxy_explains_why(self, mock_requests):
        """Generic 'failed after 3 attempts' hid the real cause."""
        from upwork_scraper.auth.token_manager import _fetch_token
        from upwork_scraper.errors import TokenFetchFailed

        mock_requests.get.return_value = MagicMock(status_code=403, cookies={})

        with pytest.raises(TokenFetchFailed, match="Datacenter proxy IPs are blocked"):
            _fetch_token(proxy_dict={"http": "http://p", "https": "http://p"})

    @patch("upwork_scraper.auth.token_manager.requests")
    def test_403_without_a_proxy_falls_through_to_raise_for_status(self, mock_requests):
        from upwork_scraper.auth.token_manager import _fetch_token

        resp = MagicMock(status_code=403, cookies={})
        resp.raise_for_status.side_effect = RuntimeError("403 Forbidden")
        mock_requests.get.return_value = resp

        with pytest.raises(RuntimeError, match="403"):
            _fetch_token()
