from unittest.mock import MagicMock, patch

import pytest

from upwork_scraper.errors import TokenExpired
from upwork_scraper.models.job_models import JobList
from upwork_scraper.models.proxy_models import ProxyConfig
from upwork_scraper.proxies.proxy_manager import NoProxyManager
from upwork_scraper.scrapers.job_fetcher import (
    UPWORK_GRAPHQL_URL,
    fetch_all_jobs,
    fetch_jobs_page,
)

EMPTY_ENVELOPE = {
    "data": {"search": {"universalSearchNuxt": {"visitorJobSearchV1": {"results": []}}}}
}


def _mock_response(payload=None, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else EMPTY_ENVELOPE
    return resp


class TestFetchJobsPage:

    @patch("upwork_scraper.scrapers.job_fetcher.requests")
    def test_sends_bearer_token_and_paging(self, mock_requests):
        mock_requests.post.return_value = _mock_response()

        fetch_jobs_page("tok123", None, offset=100, count=25)

        args, kwargs = mock_requests.post.call_args
        assert args[0] == UPWORK_GRAPHQL_URL
        assert kwargs["headers"]["Authorization"] == "Bearer tok123"
        assert kwargs["impersonate"] == "chrome"
        assert kwargs["proxies"] is None

        variables = kwargs["json"]["variables"]["requestVariables"]
        assert variables["paging"] == {"offset": 100, "count": 25}
        assert variables["sort"] == "recency"

    @patch("upwork_scraper.scrapers.job_fetcher.requests")
    def test_no_query_keeps_highlighting_on(self, mock_requests):
        mock_requests.post.return_value = _mock_response()

        fetch_jobs_page("tok", None)

        variables = mock_requests.post.call_args.kwargs["json"]["variables"]["requestVariables"]
        assert variables["highlight"] is True
        assert "userQuery" not in variables

    @patch("upwork_scraper.scrapers.job_fetcher.requests")
    def test_query_disables_highlighting(self, mock_requests):
        """Highlighting injects literal H^...^H markers around matches."""
        mock_requests.post.return_value = _mock_response()

        fetch_jobs_page("tok", None, query="wordpress")

        variables = mock_requests.post.call_args.kwargs["json"]["variables"]["requestVariables"]
        assert variables["userQuery"] == "wordpress"
        assert variables["highlight"] is False

    @patch("upwork_scraper.scrapers.job_fetcher.requests")
    def test_passes_proxy_dict(self, mock_requests):
        mock_requests.post.return_value = _mock_response()
        proxy = ProxyConfig(host="1.2.3.4", port=80, username="u", password="p")

        fetch_jobs_page("tok", proxy.to_curl_cffi_dict())

        assert mock_requests.post.call_args.kwargs["proxies"] == proxy.to_curl_cffi_dict()

    @patch("upwork_scraper.scrapers.job_fetcher.requests")
    def test_401_raises_token_expired(self, mock_requests):
        mock_requests.post.return_value = _mock_response(status=401)

        with pytest.raises(TokenExpired):
            fetch_jobs_page("tok", None)

    @patch("upwork_scraper.scrapers.job_fetcher.requests")
    def test_http_error_propagates(self, mock_requests):
        resp = _mock_response(status=500)
        resp.raise_for_status.side_effect = RuntimeError("500 Server Error")
        mock_requests.post.return_value = resp

        with pytest.raises(RuntimeError, match="500"):
            fetch_jobs_page("tok", None)


def _envelope(count=0, total=None):
    """A GraphQL response envelope with `count` results and a paging total."""
    results = [
        {
            "title": f"Job {i}",
            "description": "d",
            "ontologySkills": None,
            "jobTile": {
                "job": {
                    "ciphertext": f"~c{i}",
                    "jobType": "HOURLY",
                    "publishTime": 1700000000000,
                    "hourlyBudgetMin": None,
                    "hourlyBudgetMax": None,
                    "contractorTier": None,
                    "hourlyEngagementDuration": None,
                    "fixedPriceAmount": None,
                    "fixedPriceEngagementDuration": None,
                }
            },
        }
        for i in range(count)
    ]
    paging = {"total": total} if total is not None else {}
    return {
        "data": {
            "search": {
                "universalSearchNuxt": {
                    "visitorJobSearchV1": {"results": results, "paging": paging}
                }
            }
        }
    }


def _first_page(count=0, total=None):
    """Stand-in for fetch_page (the page-1 probe that reveals the total)."""
    return MagicMock(return_value=JobList.model_validate(_envelope(count, total)))


class TestTotalAwarePagination:

    def test_parses_total_from_paging(self):
        page = JobList.model_validate(_envelope(count=2, total=417))

        assert page.total == 417
        assert len(page.jobs) == 2

    def test_total_is_optional(self):
        assert JobList.model_validate(_envelope(count=1)).total is None

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page", return_value=[])
    def test_stops_early_when_search_has_few_results(self, mock_rest):
        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(50, total=120)
        ):
            fetch_all_jobs("tok", NoProxyManager(), max_pages=100, workers=1)

        # 120 results = page 1 (probe) + offsets 50 and 100, not 100 pages
        assert [c.kwargs["offset"] for c in mock_rest.call_args_list] == [50, 100]

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page", return_value=[])
    def test_single_page_search_makes_no_extra_requests(self, mock_rest):
        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(30, total=30)
        ):
            jobs = fetch_all_jobs("tok", NoProxyManager(), max_pages=100, workers=1)

        assert mock_rest.call_count == 0
        assert len(jobs) == 30

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page", return_value=[])
    def test_unknown_total_falls_back_to_requested_depth(self, mock_rest):
        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(50)
        ):
            fetch_all_jobs("tok", NoProxyManager(), max_pages=3, workers=1)

        assert [c.kwargs["offset"] for c in mock_rest.call_args_list] == [50, 100]

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page", return_value=[])
    def test_huge_total_still_capped_at_api_limit(self, mock_rest):
        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(50, total=99999)
        ):
            fetch_all_jobs("tok", NoProxyManager(), max_pages=1000, workers=1)

        assert max(c.kwargs["offset"] for c in mock_rest.call_args_list) == 5000

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page", return_value=[])
    def test_max_pages_wins_when_lower_than_total(self, mock_rest):
        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(50, total=99999)
        ):
            fetch_all_jobs("tok", NoProxyManager(), max_pages=2, workers=1)

        assert [c.kwargs["offset"] for c in mock_rest.call_args_list] == [50]


class TestFetchAllJobs:

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page")
    def test_page_failure_does_not_abort_run(self, mock_rest):
        mock_rest.side_effect = [RuntimeError("boom"), []]

        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(0, total=9999)
        ):
            jobs = fetch_all_jobs("tok", NoProxyManager(), max_pages=3, workers=1)

        assert jobs == []
        assert mock_rest.call_count == 2

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page")
    def test_token_expiry_propagates(self, mock_rest):
        mock_rest.side_effect = TokenExpired("401")

        with pytest.raises(TokenExpired):
            with patch(
                "upwork_scraper.scrapers.job_fetcher.fetch_page",
                _first_page(0, total=9999),
            ):
                fetch_all_jobs("tok", NoProxyManager(), max_pages=2, workers=1)

    def test_token_expiry_on_first_page_propagates(self):
        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page",
            MagicMock(side_effect=TokenExpired("401")),
        ):
            with pytest.raises(TokenExpired):
                fetch_all_jobs("tok", NoProxyManager(), max_pages=2, workers=1)

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page", return_value=[])
    def test_pinned_proxy_used_for_every_page(self, mock_rest):
        manager = MagicMock()
        pinned = {"http": "http://pin", "https": "http://pin"}

        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(0, total=9999)
        ):
            fetch_all_jobs(
                "tok", manager, max_pages=3, workers=1, pinned_proxy_dict=pinned
            )

        assert manager.get_proxy.call_count == 0
        assert all(c.args[1] == pinned for c in mock_rest.call_args_list)

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page", return_value=[])
    def test_rotates_proxies_when_not_pinned(self, mock_rest):
        manager = MagicMock()
        manager.get_proxy.return_value = ProxyConfig(
            host="1.2.3.4", port=80, username="u", password="p"
        )

        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(0, total=9999)
        ):
            fetch_all_jobs("tok", manager, max_pages=3, workers=1)

        # one proxy for the page-1 probe, one per remaining page
        assert manager.get_proxy.call_count == 3

    @patch("upwork_scraper.scrapers.job_fetcher.fetch_jobs_page", return_value=[])
    def test_merges_first_page_with_the_rest(self, mock_rest):
        mock_rest.return_value = []

        with patch(
            "upwork_scraper.scrapers.job_fetcher.fetch_page", _first_page(50, total=9999)
        ):
            jobs = fetch_all_jobs("tok", NoProxyManager(), max_pages=3, workers=1)

        assert len(jobs) == 50
