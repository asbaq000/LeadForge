from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from upwork_scraper.auth.token_manager import TokenManager, _fetch_token
from upwork_scraper.errors import TokenFetchFailed


class TestFetchToken:

    @patch("upwork_scraper.auth.token_manager.requests")
    def test_extracts_visitor_gql_token(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.cookies.get.return_value = "abc123token"
        mock_requests.get.return_value = mock_resp

        token = _fetch_token(proxy_dict={"http": "http://proxy:8080"})

        assert token == "abc123token"
        mock_requests.get.assert_called_once_with(
            "https://www.upwork.com/",
            impersonate="chrome",
            proxies={"http": "http://proxy:8080"},
            timeout=30,
        )

    @patch("upwork_scraper.auth.token_manager.requests")
    def test_raises_on_missing_cookie(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.cookies.get.return_value = None
        mock_resp.cookies.keys.return_value = ["other_cookie"]
        mock_requests.get.return_value = mock_resp

        with pytest.raises(TokenFetchFailed, match="visitor_gql_token"):
            _fetch_token()

    @patch("upwork_scraper.auth.token_manager.requests")
    def test_raises_on_http_error(self, mock_requests):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        mock_requests.get.return_value = mock_resp

        with pytest.raises(Exception, match="403"):
            _fetch_token()


class TestTokenManager:

    @patch("upwork_scraper.auth.token_manager._fetch_token", return_value="fresh_token")
    def test_fetches_token_on_first_call(self, mock_fetch):
        mgr = TokenManager()
        token = mgr.get_token()

        assert token == "fresh_token"
        mock_fetch.assert_called_once()

    @patch("upwork_scraper.auth.token_manager._fetch_token", return_value="cached_token")
    def test_returns_cached_token(self, mock_fetch):
        mgr = TokenManager()
        mgr.get_token()
        mgr.get_token()

        assert mock_fetch.call_count == 1

    @patch("upwork_scraper.auth.token_manager._fetch_token", return_value="new_token")
    def test_force_refresh_bypasses_cache(self, mock_fetch):
        mgr = TokenManager()
        mgr.get_token()
        mgr.get_token(force_refresh=True)

        assert mock_fetch.call_count == 2

    @patch("upwork_scraper.auth.token_manager._fetch_token", return_value="refreshed")
    def test_refreshes_after_ttl_expires(self, mock_fetch):
        mgr = TokenManager()
        mgr.get_token()

        # Simulate expired token
        mgr._fetched_at = datetime.now() - timedelta(minutes=30)
        token = mgr.get_token()

        assert token == "refreshed"
        assert mock_fetch.call_count == 2

    @patch("upwork_scraper.auth.token_manager._fetch_token", return_value="after_invalidate")
    def test_invalidate_forces_refetch(self, mock_fetch):
        mgr = TokenManager()
        mgr.get_token()
        mgr.invalidate()

        assert mgr._token is None
        assert mgr._fetched_at is None

        mgr.get_token()
        assert mock_fetch.call_count == 2

    @patch("upwork_scraper.auth.token_manager._fetch_token")
    def test_retries_on_failure(self, mock_fetch):
        mock_fetch.side_effect = [Exception("fail1"), Exception("fail2"), "success"]
        mgr = TokenManager()
        token = mgr.get_token()

        assert token == "success"
        assert mock_fetch.call_count == 3

    @patch("upwork_scraper.auth.token_manager._fetch_token")
    def test_raises_after_max_retries(self, mock_fetch):
        mock_fetch.side_effect = Exception("always fails")
        mgr = TokenManager()

        with pytest.raises(TokenFetchFailed, match="3 attempts"):
            mgr.get_token()

        assert mock_fetch.call_count == 3

    @patch("upwork_scraper.auth.token_manager._fetch_token", return_value="tok")
    def test_passes_proxy_dict(self, mock_fetch):
        mgr = TokenManager()
        proxy = {"http": "http://proxy:8080", "https": "http://proxy:8080"}
        mgr.get_token(proxy_dict=proxy)

        mock_fetch.assert_called_once_with(proxy)
