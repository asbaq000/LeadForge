from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from upwork_scraper.errors import TokenExpired
from upwork_scraper.models.job_models import Job
from upwork_scraper.proxies.proxy_manager import NoProxyManager
from upwork_scraper.scraper import (
    UpworkScraper,
    _dedupe,
    _normalize_queries,
    _trim,
)


def _job(cipher: str) -> Job:
    return Job.model_validate(
        {
            "title": f"Job {cipher}",
            "description": "desc",
            "ontologySkills": [{"prefLabel": "Python"}],
            "jobTile": {
                "job": {
                    "ciphertext": cipher,
                    "jobType": "HOURLY",
                    "publishTime": 1700000000000,
                    "hourlyBudgetMin": "10",
                    "hourlyBudgetMax": "20",
                    "contractorTier": "2",
                    "hourlyEngagementDuration": {"weeks": 4},
                    "fixedPriceAmount": None,
                    "fixedPriceEngagementDuration": None,
                }
            },
        }
    )


class TestDedupe:

    def test_drops_repeated_ciphers(self):
        jobs = [_job("~a"), _job("~b"), _job("~a")]
        assert [j.cipher for j in _dedupe(jobs)] == ["~a", "~b"]

    def test_keeps_order(self):
        jobs = [_job("~c"), _job("~a"), _job("~b")]
        assert [j.cipher for j in _dedupe(jobs)] == ["~c", "~a", "~b"]


class TestUpworkScraper:

    def _scraper(self, token_manager=None):
        return UpworkScraper(
            proxy_manager=NoProxyManager(),
            token_manager=token_manager or MagicMock(**{"get_token.return_value": "tok"}),
        )

    def test_direct_connection_lowers_workers(self):
        assert self._scraper().workers == 2

    def test_explicit_workers_wins(self):
        scraper = UpworkScraper(
            proxy_manager=NoProxyManager(),
            token_manager=MagicMock(),
            workers=7,
        )
        assert scraper.workers == 7

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_scrape_returns_deduped_jobs(self, mock_fetch):
        mock_fetch.return_value = [_job("~a"), _job("~a"), _job("~b")]
        jobs = self._scraper().scrape(max_pages=1)

        assert [j.cipher for j in jobs] == ["~a", "~b"]

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_scrape_passes_no_proxy(self, mock_fetch):
        mock_fetch.return_value = []
        self._scraper().scrape(max_pages=1)

        assert mock_fetch.call_args.kwargs["pinned_proxy_dict"] is None

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_scrape_retries_once_on_token_expiry(self, mock_fetch):
        mock_fetch.side_effect = [TokenExpired("401"), [_job("~a")]]
        token_mgr = MagicMock(**{"get_token.return_value": "tok"})

        jobs = self._scraper(token_mgr).scrape(max_pages=1)

        assert [j.cipher for j in jobs] == ["~a"]
        assert token_mgr.invalidate.call_count == 1
        assert mock_fetch.call_count == 2

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_scrape_raises_after_second_expiry(self, mock_fetch):
        mock_fetch.side_effect = TokenExpired("401")

        with pytest.raises(TokenExpired):
            self._scraper().scrape(max_pages=1)

        assert mock_fetch.call_count == 2

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_scrape_loop_filters_already_seen(self, mock_fetch):
        mock_fetch.side_effect = [[_job("~a"), _job("~b")], [_job("~b"), _job("~c")]]
        stop = MagicMock(**{"is_set.side_effect": [False, False, True]})

        batches = list(self._scraper().scrape_loop(interval=0, stop_event=stop))

        assert [[j.cipher for j in b] for b in batches] == [["~a", "~b"], ["~c"]]


class TestJobSerialization:

    def test_model_dump_json_mode_is_serializable(self):
        data = _job("~a").model_dump(mode="json")

        assert data["cipher"] == "~a"
        assert data["link"] == "https://www.upwork.com/jobs/~a"
        assert isinstance(data["published_date"], str)
        assert data["skills"] == ["Python"]
        assert _job("~a").published_date == datetime(
            2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc
        )


def _job_at(cipher: str, minutes_ago: float) -> Job:
    job = _job(cipher)
    job.published_date = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return job


class TestNormalizeQueries:

    def test_none_gives_single_unfiltered_pass(self):
        assert _normalize_queries(None) == [None]

    def test_single_string(self):
        assert _normalize_queries("react") == ["react"]

    def test_list_of_keywords(self):
        assert _normalize_queries(["react", "django"]) == ["react", "django"]

    def test_strips_and_drops_blanks(self):
        assert _normalize_queries([" react ", "", "  "]) == ["react"]

    def test_all_blank_falls_back_to_unfiltered(self):
        assert _normalize_queries(["", "  "]) == [None]


class TestMultiKeyword:

    def _scraper(self):
        return UpworkScraper(
            proxy_manager=NoProxyManager(),
            token_manager=MagicMock(**{"get_token.return_value": "tok"}),
        )

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_one_search_per_keyword(self, mock_fetch):
        mock_fetch.side_effect = [[_job("~a")], [_job("~b")]]

        jobs = self._scraper().scrape(max_pages=1, query=["react", "django"])

        assert mock_fetch.call_count == 2
        assert [c.kwargs["query"] for c in mock_fetch.call_args_list] == ["react", "django"]
        assert {j.cipher for j in jobs} == {"~a", "~b"}

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_tags_matched_query(self, mock_fetch):
        mock_fetch.side_effect = [[_job("~a")], [_job("~b")]]

        jobs = {j.cipher: j for j in self._scraper().scrape(query=["react", "django"])}

        assert jobs["~a"].matched_query == "react"
        assert jobs["~b"].matched_query == "django"

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_job_matching_two_keywords_appears_once(self, mock_fetch):
        mock_fetch.side_effect = [[_job("~a")], [_job("~a")]]

        jobs = self._scraper().scrape(query=["react", "django"])

        assert len(jobs) == 1
        assert jobs[0].matched_query == "react, django"

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_no_query_leaves_matched_query_empty(self, mock_fetch):
        mock_fetch.return_value = [_job("~a")]

        assert self._scraper().scrape().pop().matched_query is None


class TestFreshness:

    def _scraper(self):
        return UpworkScraper(
            proxy_manager=NoProxyManager(),
            token_manager=MagicMock(**{"get_token.return_value": "tok"}),
        )

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_max_age_drops_older_jobs(self, mock_fetch):
        mock_fetch.return_value = [
            _job_at("~fresh", 2), _job_at("~stale", 90), _job_at("~ancient", 5000)
        ]

        jobs = self._scraper().scrape(max_age_minutes=30)

        assert [j.cipher for j in jobs] == ["~fresh"]

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_no_max_age_keeps_everything(self, mock_fetch):
        mock_fetch.return_value = [_job_at("~fresh", 2), _job_at("~ancient", 5000)]

        assert len(self._scraper().scrape()) == 2

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_results_are_newest_first(self, mock_fetch):
        mock_fetch.return_value = [_job_at("~mid", 5), _job_at("~old", 60), _job_at("~new", 1)]

        jobs = self._scraper().scrape()

        assert [j.cipher for j in jobs] == ["~new", "~mid", "~old"]

    def test_age_seconds(self):
        assert 100 < _job_at("~a", 2).age_seconds() < 140


class TestLoopBacklogAndGaps:

    def _scraper(self):
        return UpworkScraper(
            proxy_manager=NoProxyManager(),
            token_manager=MagicMock(**{"get_token.return_value": "tok"}),
        )

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_skip_backlog_swallows_first_cycle(self, mock_fetch):
        mock_fetch.side_effect = [[_job("~a"), _job("~b")], [_job("~b"), _job("~c")]]
        stop = MagicMock(**{"is_set.side_effect": [False, False, True]})

        batches = list(
            self._scraper().scrape_loop(interval=0, skip_backlog=True, stop_event=stop)
        )

        assert [[j.cipher for j in b] for b in batches] == [[], ["~c"]]

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_warns_when_whole_page_is_new(self, mock_fetch, caplog):
        mock_fetch.side_effect = [[_job("~a")], [_job("~b")]]
        stop = MagicMock(**{"is_set.side_effect": [False, False, True]})

        with caplog.at_level("WARNING"):
            list(self._scraper().scrape_loop(interval=0, stop_event=stop))

        assert "may have been missed" in caplog.text

    @patch("upwork_scraper.scraper.fetch_all_jobs")
    def test_no_warning_when_some_jobs_repeat(self, mock_fetch, caplog):
        mock_fetch.side_effect = [[_job("~a")], [_job("~a"), _job("~b")]]
        stop = MagicMock(**{"is_set.side_effect": [False, False, True]})

        with caplog.at_level("WARNING"):
            list(self._scraper().scrape_loop(interval=0, stop_event=stop))

        assert "missed" not in caplog.text


class TestSeenTrim:

    def test_forgets_oldest_beyond_limit(self):
        seen = {f"~{i}": None for i in range(10)}
        _trim(seen, limit=4)

        assert list(seen) == ["~6", "~7", "~8", "~9"]

    def test_leaves_small_sets_alone(self):
        seen = {"~a": None, "~b": None}
        _trim(seen, limit=100)

        assert list(seen) == ["~a", "~b"]
