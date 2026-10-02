"""Country exclusion: normalisation, the default list, and how it is resolved."""

import pytest

from upwork_scraper.country_filter import (
    DEFAULT_EXCLUDED_COUNTRIES,
    CountryFilter,
    normalize_country,
    parse_country_list,
)
from upwork_scraper.models.client_models import ClientInfo
from upwork_scraper.models.job_models import Job


def _job(cipher: str, country: str | None = None, enriched: bool = True) -> Job:
    job = Job.model_validate({
        "title": f"job {cipher}",
        "description": "d",
        "jobTile": {"job": {
            "ciphertext": cipher, "jobType": "HOURLY", "publishTime": 1_700_000_000,
        }},
    })
    if enriched:
        job.client = ClientInfo(cipher=job.cipher, country=country)
    return job


class TestNormalizeCountry:

    @pytest.mark.parametrize("value", ["India", "india", " INDIA ", "IND", "IN"])
    def test_india_spellings_collapse(self, value):
        assert normalize_country(value) == "india"

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("Philippines", "philippines"),
            ("The Philippines", "philippines"),
            ("PHL", "philippines"),
            ("Republic of the Philippines", "philippines"),
            ("Bangladesh", "bangladesh"),
            ("BGD", "bangladesh"),
            ("Egypt", "egypt"),
            ("EGY", "egypt"),
            ("Arab Republic of Egypt", "egypt"),
            ("Pakistan", "pakistan"),
            ("PAK", "pakistan"),
            ("Islamic Republic of Pakistan", "pakistan"),
        ],
    )
    def test_aliases(self, value, expected):
        assert normalize_country(value) == expected

    def test_punctuation_and_spacing_are_ignored(self):
        assert normalize_country("People's  Republic of Bangladesh") == "bangladesh"

    def test_unknown_country_keeps_its_own_text(self):
        assert normalize_country("Latvia") == "latvia"

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_is_none(self, value):
        assert normalize_country(value) is None

    def test_country_codes_from_other_regions_are_distinct(self):
        # The parser has produced "AUS", "NLD" and "USA" from real pages; none
        # of them may collide with an excluded country.
        for code in ("AUS", "NLD", "USA", "GBR"):
            assert normalize_country(code) not in DEFAULT_EXCLUDED_COUNTRIES


class TestParseCountryList:

    def test_unset_means_use_the_default(self):
        assert parse_country_list(None) is None

    @pytest.mark.parametrize("raw", ["", "   ", "none", "NONE", "off", "0", "false"])
    def test_explicit_off_is_an_empty_list(self, raw):
        assert parse_country_list(raw) == []

    def test_splits_and_strips(self):
        assert parse_country_list("India, Pakistan ,Nepal") == [
            "India", "Pakistan", "Nepal"
        ]


class TestDefaults:

    def test_the_five_countries_are_excluded(self):
        f = CountryFilter.default()
        for country in ("India", "Pakistan", "Bangladesh", "Egypt", "Philippines"):
            assert f.status(country) == "blocked", country

    def test_other_countries_pass(self):
        f = CountryFilter.default()
        for country in ("United States", "Australia", "Germany", "Latvia"):
            assert f.status(country) == "allowed", country

    def test_codes_are_blocked_too(self):
        f = CountryFilter.default()
        assert f.status("IND") == "blocked"
        assert f.status("PHL") == "blocked"

    def test_missing_country_is_unknown(self):
        assert CountryFilter.default().status(None) == "unknown"

    def test_empty_filter_is_inactive(self):
        assert not CountryFilter().is_active
        assert CountryFilter.default().is_active

    def test_names_are_readable(self):
        assert CountryFilter.default().names == [
            "Bangladesh", "Egypt", "India", "Pakistan", "Philippines"
        ]


class TestBuild:

    def test_base_none_uses_the_default(self):
        assert CountryFilter.build().excluded == frozenset(DEFAULT_EXCLUDED_COUNTRIES)

    def test_base_empty_starts_from_nothing(self):
        assert CountryFilter.build(base=[]).excluded == frozenset()

    def test_base_replaces_rather_than_extends(self):
        f = CountryFilter.build(base=["Nepal"])
        assert f.status("Nepal") == "blocked"
        assert f.status("India") == "allowed"

    def test_add_extends_the_default(self):
        f = CountryFilter.build(add=["Nepal", "Sri Lanka"])
        assert f.status("Nepal") == "blocked"
        assert f.status("India") == "blocked"

    def test_remove_takes_one_off_the_default(self):
        f = CountryFilter.build(remove=["India"])
        assert f.status("India") == "allowed"
        assert f.status("Pakistan") == "blocked"

    def test_remove_matches_by_alias_not_spelling(self):
        f = CountryFilter.build(remove=["IND"])
        assert f.status("India") == "allowed"

    def test_remove_wins_over_add(self):
        f = CountryFilter.build(add=["Nepal"], remove=["Nepal"])
        assert f.status("Nepal") == "allowed"


class TestAllows:

    def test_blocked_country_is_dropped(self):
        assert not CountryFilter.default().allows(_job("~a", "India"))

    def test_allowed_country_is_kept(self):
        assert CountryFilter.default().allows(_job("~a", "United States"))

    def test_unenriched_job_is_kept_by_default(self):
        assert CountryFilter.default().allows(_job("~a", enriched=False))

    def test_failed_lookup_is_kept_by_default(self):
        assert CountryFilter.default().allows(_job("~a", country=None))

    def test_drop_unknown_drops_both(self):
        f = CountryFilter.default(drop_unknown=True)
        assert not f.allows(_job("~a", enriched=False))
        assert not f.allows(_job("~b", country=None))

    def test_drop_unknown_still_keeps_a_known_good_country(self):
        f = CountryFilter.default(drop_unknown=True)
        assert f.allows(_job("~a", "Canada"))

    def test_works_as_a_scrape_job_filter(self):
        # Same signature as Niche.is_relevant, so it can be passed to
        # UpworkScraper.scrape(job_filter=...) — a no-op before enrichment.
        f = CountryFilter.default()
        jobs = [_job("~a", enriched=False), _job("~b", enriched=False)]
        assert [j for j in jobs if f.allows(j)] == jobs


class TestPartitionAndApply:

    def _mixed(self):
        return [
            _job("~a", "United States"),
            _job("~b", "India"),
            _job("~c", "Germany"),
            _job("~d", "PHL"),
            _job("~e", enriched=False),
        ]

    def test_partition_preserves_order(self):
        kept, dropped = CountryFilter.default().partition(self._mixed())
        assert [j.cipher for j in kept] == ["~a", "~c", "~e"]
        assert [j.cipher for j in dropped] == ["~b", "~d"]

    def test_apply_returns_the_kept_jobs(self):
        kept = CountryFilter.default().apply(self._mixed())
        assert [j.cipher for j in kept] == ["~a", "~c", "~e"]

    def test_inactive_filter_keeps_everything(self):
        jobs = self._mixed()
        assert CountryFilter().apply(jobs) == jobs

    def test_apply_logs_a_breakdown(self, caplog):
        with caplog.at_level("INFO", logger="upwork_scraper.country_filter"):
            CountryFilter.default().apply(self._mixed())
        assert "dropped 2 of 5" in caplog.text
        assert "India 1" in caplog.text
        assert "Philippines 1" in caplog.text

    def test_apply_is_quiet_when_nothing_is_dropped(self, caplog):
        with caplog.at_level("INFO", logger="upwork_scraper.country_filter"):
            CountryFilter.default().apply([_job("~a", "Canada")])
        assert "dropped" not in caplog.text


class TestDescribe:

    def test_lists_countries_and_unknown_policy(self):
        text = CountryFilter.default().describe()
        assert "India" in text and "Philippines" in text
        assert "unknown country: kept" in text

    def test_drop_unknown_is_stated(self):
        assert "unknown country: dropped" in CountryFilter.default(
            drop_unknown=True
        ).describe()

    def test_off(self):
        assert CountryFilter().describe() == "country filter off"
