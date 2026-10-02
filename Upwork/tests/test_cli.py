import csv
import io
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from upwork_scraper.cli import (
    CSV_FIELDS,
    build_parser,
    main,
    build_enricher,
    ensure_signed_in,
    render,
    resolve_country_filter,
    resolve_keywords,
    resolve_max_age,
    run_enrichment,
    use_utf8_streams,
)
from upwork_scraper.models.job_models import Job


def _job(cipher: str) -> Job:
    return Job.model_validate(
        {
            "title": "Scrape, please",
            "description": "line one\nline two, with a comma",
            "ontologySkills": [{"prefLabel": "Python"}, {"prefLabel": "curl"}],
            "jobTile": {
                "job": {
                    "ciphertext": cipher,
                    "jobType": "FIXED",
                    "publishTime": 1700000000000,
                    "hourlyBudgetMin": None,
                    "hourlyBudgetMax": None,
                    "contractorTier": "2",
                    "hourlyEngagementDuration": None,
                    "fixedPriceAmount": {"amount": "500"},
                    "fixedPriceEngagementDuration": {"weeks": 2},
                }
            },
        }
    )


class TestRender:

    def test_json_roundtrips(self):
        parsed = json.loads(render([_job("~a"), _job("~b")], "json"))

        assert [row["cipher"] for row in parsed] == ["~a", "~b"]
        assert parsed[0]["budget"] == 500

    def test_jsonl_is_one_object_per_line(self):
        lines = render([_job("~a"), _job("~b")], "jsonl").splitlines()

        assert len(lines) == 2
        assert json.loads(lines[1])["cipher"] == "~b"

    def test_csv_has_header_and_flattened_skills(self):
        rows = list(csv.DictReader(io.StringIO(render([_job("~a")], "csv"))))

        assert list(rows[0].keys()) == CSV_FIELDS
        assert rows[0]["skills"] == "Python|curl"
        assert rows[0]["cipher"] == "~a"

    def test_csv_survives_commas_and_newlines(self):
        rows = list(csv.DictReader(io.StringIO(render([_job("~a"), _job("~b")], "csv"))))

        assert len(rows) == 2
        assert "line two, with a comma" in rows[0]["description"]

    def test_empty_job_list(self):
        assert json.loads(render([], "json")) == []
        assert render([], "jsonl") == ""
        assert render([], "csv").strip() == ",".join(CSV_FIELDS)

    def test_unknown_format(self):
        with pytest.raises(ValueError, match="Unknown format"):
            render([], "xml")


class TestParser:

    def test_defaults(self):
        args = build_parser().parse_args([])

        assert args.fmt == "json"
        assert args.watch is False
        assert args.query is None

    def test_flags(self):
        args = build_parser().parse_args(
            ["--pages", "7", "--query", "react", "--format", "csv", "--no-proxy"]
        )

        assert (args.pages, args.query, args.fmt, args.no_proxy) == (
            7, ["react"], "csv", True
        )


class TestMain:

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_writes_file(self, mock_scraper_cls, tmp_path):
        mock_scraper_cls.return_value.scrape.return_value = [_job("~a")]
        out = tmp_path / "jobs.json"

        assert main(["--no-proxy", "--pages", "1", "--out", str(out)]) == 0

        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert parsed[0]["cipher"] == "~a"

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_no_proxy_flag_skips_webshare(self, mock_scraper_cls, tmp_path):
        mock_scraper_cls.return_value.scrape.return_value = []

        main(["--no-proxy", "--out", str(tmp_path / "x.json")])

        proxy_manager = mock_scraper_cls.call_args.kwargs["proxy_manager"]
        assert proxy_manager.get_proxy() is None

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_passes_query_through(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock(**{"scrape.return_value": []})
        mock_scraper_cls.return_value = scraper

        main(["--no-proxy", "--query", "django", "--out", str(tmp_path / "x.json")])

        assert scraper.scrape.call_args.kwargs["query"] == ["django"]


class TestParseQueries:

    def test_none(self):
        from upwork_scraper.cli import parse_queries

        assert parse_queries(None) is None

    def test_repeated_flags(self):
        from upwork_scraper.cli import parse_queries

        assert parse_queries(["react", "django"]) == ["react", "django"]

    def test_comma_separated(self):
        from upwork_scraper.cli import parse_queries

        assert parse_queries(["react,django, web scraping"]) == [
            "react", "django", "web scraping"
        ]

    def test_mixed_and_blank(self):
        from upwork_scraper.cli import parse_queries

        assert parse_queries(["react,", " ", "django"]) == ["react", "django"]


class TestRealtimeFlags:

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_multiple_queries_reach_scraper(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock(**{"scrape.return_value": []})
        mock_scraper_cls.return_value = scraper

        main([
            "--no-proxy", "--query", "react,django", "--query", "python",
            "--out", str(tmp_path / "x.json"),
        ])

        assert scraper.scrape.call_args.kwargs["query"] == ["react", "django", "python"]

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_max_age_reaches_scraper(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock(**{"scrape.return_value": []})
        mock_scraper_cls.return_value = scraper

        main(["--no-proxy", "--max-age", "15", "--out", str(tmp_path / "x.json")])

        assert scraper.scrape.call_args.kwargs["max_age_minutes"] == 15.0

    def test_skip_backlog_defaults_off(self):
        assert build_parser().parse_args([]).skip_backlog is False

    def test_matched_query_is_a_csv_column(self):
        assert "matched_query" in CSV_FIELDS


class TestNicheFlags:

    def test_niche_supplies_keywords_and_filter(self, tmp_path):
        args = build_parser().parse_args(["--niche", "video"])
        queries, niche = resolve_keywords(args)

        assert niche is not None
        assert "video editing" in queries
        assert len(queries) == len(niche.keywords)

    def test_query_adds_to_niche_keywords(self):
        args = build_parser().parse_args(
            ["--niche", "video", "--query", "drone footage,color grading"]
        )
        queries, niche = resolve_keywords(args)

        assert "video editing" in queries          # preset kept
        assert "drone footage" in queries          # yours added
        assert "color grading" in queries

    def test_keywords_file_adds_too(self, tmp_path):
        path = tmp_path / "kw.txt"
        path.write_text("# mine\nanimation studio\n", encoding="utf-8")

        args = build_parser().parse_args(
            ["--niche", "video", "--keywords-file", str(path)]
        )
        queries, _ = resolve_keywords(args)

        assert "animation studio" in queries
        assert "video editing" in queries

    def test_no_niche_means_no_filter(self):
        args = build_parser().parse_args(["--query", "react"])
        queries, niche = resolve_keywords(args)

        assert queries == ["react"]
        assert niche is None

    def test_keywords_file_without_niche(self, tmp_path):
        path = tmp_path / "kw.txt"
        path.write_text("video editing\ntiktok\n", encoding="utf-8")

        args = build_parser().parse_args(["--keywords-file", str(path)])
        queries, niche = resolve_keywords(args)

        assert queries == ["video editing", "tiktok"]
        assert niche is None

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_no_filter_flag_disables_relevance(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock(**{"scrape.return_value": []})
        mock_scraper_cls.return_value = scraper

        main(["--no-proxy", "--niche", "video", "--no-filter",
              "--out", str(tmp_path / "x.json")])

        assert scraper.scrape.call_args.kwargs["job_filter"] is None

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_filter_applied_by_default(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock(**{"scrape.return_value": []})
        mock_scraper_cls.return_value = scraper

        main(["--no-proxy", "--niche", "video", "--out", str(tmp_path / "x.json")])

        assert callable(scraper.scrape.call_args.kwargs["job_filter"])

    def test_list_niches_exits_cleanly(self, capsys):
        assert main(["--list-niches"]) == 0
        assert "video" in capsys.readouterr().out


class TestBackfill:

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_backfill_uses_backfill_pages(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock(**{"scrape.return_value": []})
        mock_scraper_cls.return_value = scraper

        main(["--no-proxy", "--backfill", "--backfill-pages", "40",
              "--out", str(tmp_path / "x.json")])

        assert scraper.scrape.call_args.kwargs["max_pages"] == 40

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_backfill_without_watch_does_not_loop(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock(**{"scrape.return_value": []})
        mock_scraper_cls.return_value = scraper

        assert main(["--no-proxy", "--backfill", "--out", str(tmp_path / "x.json")]) == 0
        assert scraper.scrape_loop.call_count == 0

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_backfill_writes_results(self, mock_scraper_cls, tmp_path):
        mock_scraper_cls.return_value.scrape.return_value = [_job("~a"), _job("~b")]
        out = tmp_path / "back.json"

        main(["--no-proxy", "--backfill", "--out", str(out)])

        assert len(json.loads(out.read_text(encoding="utf-8"))) == 2

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_backfilled_jobs_seed_the_watch_loop(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock()
        scraper.scrape.return_value = [_job("~a"), _job("~b")]
        scraper.scrape_loop.return_value = iter([])
        mock_scraper_cls.return_value = scraper

        main(["--no-proxy", "--backfill", "--watch", "--out", str(tmp_path / "f.jsonl")])

        assert scraper.scrape_loop.call_args.kwargs["initial_seen"] == ["~a", "~b"]


class TestUtf8Streams:

    def test_reconfigure_is_attempted(self):
        stdout, stderr = MagicMock(), MagicMock()
        with patch("upwork_scraper.cli.sys.stdout", stdout), \
             patch("upwork_scraper.cli.sys.stderr", stderr):
            use_utf8_streams()

        stdout.reconfigure.assert_called_once_with(encoding="utf-8")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8")

    def test_survives_streams_that_cannot_reconfigure(self):
        broken = MagicMock()
        broken.reconfigure.side_effect = ValueError("captured")
        with patch("upwork_scraper.cli.sys.stdout", broken), \
             patch("upwork_scraper.cli.sys.stderr", broken):
            use_utf8_streams()  # must not raise


class TestMaxAgeDays:

    def test_days_convert_to_minutes(self):
        args = build_parser().parse_args(["--max-age-days", "2"])

        assert resolve_max_age(args) == 2880

    def test_minutes_still_work(self):
        args = build_parser().parse_args(["--max-age", "30"])

        assert resolve_max_age(args) == 30

    def test_days_win_when_both_given(self):
        args = build_parser().parse_args(["--max-age-days", "2", "--max-age", "30"])

        assert resolve_max_age(args) == 2880

    def test_absent_by_default(self):
        assert resolve_max_age(build_parser().parse_args([])) is None

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_two_day_filter_reaches_the_scraper(self, mock_scraper_cls, tmp_path):
        scraper = MagicMock(**{"scrape.return_value": []})
        mock_scraper_cls.return_value = scraper

        main(["--no-proxy", "--max-age-days", "2", "--out", str(tmp_path / "x.json")])

        assert scraper.scrape.call_args.kwargs["max_age_minutes"] == 2880


class TestEnrichmentWiring:

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_off_by_default(self, mock_scraper_cls, tmp_path):
        mock_scraper_cls.return_value.scrape.return_value = []

        main(["--no-proxy", "--out", str(tmp_path / "x.json")])
        # nothing to assert beyond it not blowing up; no clients file appears
        assert not (tmp_path / "x.json.clients.jsonl").exists()

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.config")
    def test_uses_a_browser_by_default(self, mock_config, mock_browser):
        """Plain HTTP is always 403d on job pages, so a browser is the default."""
        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11
        args = build_parser().parse_args(["--enrich-clients"])

        enricher = build_enricher(args, MagicMock())

        assert enricher is not None
        assert enricher.uses_browser is True
        assert mock_browser.return_value.start.called

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.config")
    def test_offscreen_unless_headful(self, mock_config, mock_browser):
        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11

        build_enricher(build_parser().parse_args(["--enrich-clients"]), MagicMock())
        assert mock_browser.call_args.kwargs["offscreen"] is True

        build_enricher(
            build_parser().parse_args(["--enrich-clients", "--headful"]), MagicMock()
        )
        assert mock_browser.call_args.kwargs["offscreen"] is False

    @patch("upwork_scraper.cli.config")
    def test_no_browser_flag_warns_it_will_be_blocked(self, mock_config, caplog):
        mock_config.UPWORK_COOKIE = None
        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11
        args = build_parser().parse_args(["--enrich-clients", "--no-browser"])

        with caplog.at_level("WARNING"):
            enricher = build_enricher(args, MagicMock())

        assert enricher.uses_browser is False
        assert "blocked" in caplog.text.lower()

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.config")
    def test_custom_delay_is_applied(self, mock_config, mock_browser):
        mock_config.UPWORK_COOKIE = "session=abc"
        args = build_parser().parse_args(
            ["--enrich-clients", "--enrich-delay", "15", "40"]
        )

        enricher = build_enricher(args, MagicMock())

        assert (enricher.delay.min_seconds, enricher.delay.max_seconds) == (15, 40)

    def test_clients_go_to_their_own_file(self, tmp_path):
        """Job output must stay exactly as it was — client data is separate."""
        from upwork_scraper.models.client_models import ClientInfo

        out = tmp_path / "jobs.jsonl"
        args = build_parser().parse_args(
            ["--format", "jsonl", "--out", str(out), "--separate-clients"]
        )
        enricher = MagicMock()
        enricher.enrich_all.return_value = [
            ClientInfo(cipher="~a", total_hires=5),
            ClientInfo(cipher="~b", fetch_status="failed", fetch_error="HTTP 403"),
        ]

        run_enrichment(enricher, [_job("~a"), _job("~b")], args)

        clients = tmp_path / "jobs.jsonl.clients.jsonl"
        assert clients.exists()
        assert not out.exists()          # job file untouched by enrichment
        rows = [json.loads(l) for l in clients.read_text(encoding="utf-8").splitlines() if l]
        assert [r["cipher"] for r in rows] == ["~a", "~b"]
        assert rows[1]["fetch_status"] == "failed"

    def test_enrich_limit_caps_the_batch(self, tmp_path):
        args = build_parser().parse_args(
            ["--enrich-limit", "2", "--out", str(tmp_path / "j.jsonl")]
        )
        enricher = MagicMock()
        enricher.enrich_all.return_value = []

        run_enrichment(enricher, [_job(f"~{i}") for i in range(10)], args)

        assert len(enricher.enrich_all.call_args.args[0]) == 2

    def test_no_enricher_is_a_noop(self, tmp_path):
        args = build_parser().parse_args(["--out", str(tmp_path / "j.jsonl")])

        run_enrichment(None, [_job("~a")], args)  # must not raise


class TestResolveCountryFilter:

    def _args(self, argv):
        return build_parser().parse_args(argv)

    @patch("upwork_scraper.cli.config")
    def test_default_excludes_the_five(self, mock_config):
        mock_config.EXCLUDED_COUNTRIES = None

        f = resolve_country_filter(self._args(["--enrich-clients"]))

        assert f.names == [
            "Bangladesh", "Egypt", "India", "Pakistan", "Philippines"
        ]
        assert not f.drop_unknown

    @patch("upwork_scraper.cli.config")
    def test_no_country_filter_turns_it_off(self, mock_config):
        mock_config.EXCLUDED_COUNTRIES = None

        f = resolve_country_filter(self._args(["--no-country-filter"]))

        assert not f.is_active

    @patch("upwork_scraper.cli.config")
    def test_exclude_country_adds(self, mock_config):
        mock_config.EXCLUDED_COUNTRIES = None

        f = resolve_country_filter(
            self._args(["--enrich-clients", "--exclude-country", "Nepal,Kenya"])
        )

        assert f.status("Nepal") == "blocked"
        assert f.status("Kenya") == "blocked"
        assert f.status("India") == "blocked"

    @patch("upwork_scraper.cli.config")
    def test_allow_country_removes(self, mock_config):
        mock_config.EXCLUDED_COUNTRIES = None

        f = resolve_country_filter(
            self._args(["--enrich-clients", "--allow-country", "India"])
        )

        assert f.status("India") == "allowed"
        assert f.status("Pakistan") == "blocked"

    @patch("upwork_scraper.cli.config")
    def test_env_list_replaces_the_default(self, mock_config):
        mock_config.EXCLUDED_COUNTRIES = "Nepal, Latvia"

        f = resolve_country_filter(self._args(["--enrich-clients"]))

        assert f.names == ["Latvia", "Nepal"]
        assert f.status("India") == "allowed"

    @patch("upwork_scraper.cli.config")
    def test_env_none_turns_it_off(self, mock_config):
        mock_config.EXCLUDED_COUNTRIES = "none"

        assert not resolve_country_filter(self._args(["--enrich-clients"])).is_active

    @patch("upwork_scraper.cli.config")
    def test_drop_unknown_country_flag(self, mock_config):
        mock_config.EXCLUDED_COUNTRIES = None

        f = resolve_country_filter(
            self._args(["--enrich-clients", "--drop-unknown-country"])
        )

        assert f.drop_unknown

    @patch("upwork_scraper.cli.config")
    def test_warns_when_nothing_will_be_enriched(self, mock_config, caplog):
        """Without enrichment there is no country, so the filter cannot act."""
        mock_config.EXCLUDED_COUNTRIES = None

        with caplog.at_level(logging.WARNING, logger="upwork_scraper.cli"):
            resolve_country_filter(self._args([]))

        assert "does not return the client's country" in caplog.text

    @patch("upwork_scraper.cli.config")
    def test_no_warning_with_enrichment(self, mock_config, caplog):
        mock_config.EXCLUDED_COUNTRIES = None

        with caplog.at_level(logging.WARNING, logger="upwork_scraper.cli"):
            resolve_country_filter(self._args(["--enrich-clients"]))

        assert "does not return" not in caplog.text


class TestCountryFilterInEnrichment:

    def _enricher(self, *countries):
        from upwork_scraper.models.client_models import ClientInfo

        enricher = MagicMock()
        enricher.enrich_all.return_value = [
            ClientInfo(cipher=f"~{i}", country=c)
            for i, c in enumerate(countries)
        ]
        return enricher

    def test_excluded_countries_are_dropped_from_the_output(self, tmp_path):
        from upwork_scraper.country_filter import CountryFilter

        args = build_parser().parse_args(["--out", str(tmp_path / "j.jsonl")])
        jobs = [_job("~0"), _job("~1"), _job("~2")]

        kept = run_enrichment(
            self._enricher("United States", "India", "Germany"),
            jobs, args, CountryFilter.default(),
        )

        assert [j.cipher for j in kept] == ["~0", "~2"]

    def test_kept_jobs_still_carry_their_client(self, tmp_path):
        from upwork_scraper.country_filter import CountryFilter

        args = build_parser().parse_args(["--out", str(tmp_path / "j.jsonl")])
        jobs = [_job("~0"), _job("~1")]

        kept = run_enrichment(
            self._enricher("Canada", "PHL"), jobs, args, CountryFilter.default()
        )

        assert [j.cipher for j in kept] == ["~0"]
        assert kept[0].client.country == "Canada"

    def test_no_filter_keeps_everything(self, tmp_path):
        args = build_parser().parse_args(["--out", str(tmp_path / "j.jsonl")])
        jobs = [_job("~0"), _job("~1")]

        kept = run_enrichment(self._enricher("India", "Egypt"), jobs, args, None)

        assert [j.cipher for j in kept] == ["~0", "~1"]

    def test_separate_client_file_excludes_dropped_clients(self, tmp_path):
        """Both outputs must describe the same set of jobs."""
        from upwork_scraper.country_filter import CountryFilter

        out = tmp_path / "jobs.jsonl"
        args = build_parser().parse_args(
            ["--format", "jsonl", "--out", str(out), "--separate-clients"]
        )

        kept = run_enrichment(
            self._enricher("United States", "India"),
            [_job("~0"), _job("~1")], args, CountryFilter.default(),
        )

        rows = [
            json.loads(line)
            for line in (tmp_path / "jobs.jsonl.clients.jsonl")
            .read_text(encoding="utf-8").splitlines() if line
        ]
        assert [r["cipher"] for r in rows] == ["~0"]
        assert [j.cipher for j in kept] == ["~0"]

    def test_unenriched_jobs_survive_by_default(self, tmp_path):
        from upwork_scraper.country_filter import CountryFilter

        args = build_parser().parse_args(["--out", str(tmp_path / "j.jsonl")])
        jobs = [_job("~a"), _job("~b")]

        kept = run_enrichment(None, jobs, args, CountryFilter.default())

        assert [j.cipher for j in kept] == ["~a", "~b"]

    def test_drop_unknown_removes_unenriched_jobs(self, tmp_path):
        from upwork_scraper.country_filter import CountryFilter

        args = build_parser().parse_args(["--out", str(tmp_path / "j.jsonl")])

        kept = run_enrichment(
            None, [_job("~a")], args, CountryFilter.default(drop_unknown=True)
        )

        assert kept == []


class TestClientOutputFormat:

    def _run(self, argv, tmp_path):
        from upwork_scraper.models.client_models import ClientInfo

        args = build_parser().parse_args(argv + ["--separate-clients"])
        enricher = MagicMock()
        enricher.enrich_all.return_value = [
            ClientInfo(cipher="~a", country="USA", total_spent=3800.0),
            ClientInfo(cipher="~b", country="India"),
        ]
        run_enrichment(enricher, [_job("~a"), _job("~b")], args)
        return args

    def test_json_gives_a_parseable_array(self, tmp_path):
        out = tmp_path / "jobs.json"
        self._run(["--format", "json", "--out", str(out)], tmp_path)

        clients = tmp_path / "jobs.json.clients.json"
        records = json.loads(clients.read_text(encoding="utf-8"))

        assert [r["cipher"] for r in records] == ["~a", "~b"]
        assert records[0]["total_spent"] == 3800.0

    def test_jsonl_stays_one_per_line(self, tmp_path):
        out = tmp_path / "jobs.jsonl"
        self._run(["--format", "jsonl", "--out", str(out)], tmp_path)

        clients = tmp_path / "jobs.jsonl.clients.jsonl"
        lines = [l for l in clients.read_text(encoding="utf-8").splitlines() if l]

        assert len(lines) == 2
        assert json.loads(lines[1])["cipher"] == "~b"

    def test_watch_mode_uses_jsonl_even_with_json(self, tmp_path):
        """Cycles append, and appended JSON arrays would not parse."""
        out = tmp_path / "feed.json"
        self._run(["--format", "json", "--watch", "--out", str(out)], tmp_path)

        assert (tmp_path / "feed.json.clients.jsonl").exists()
        assert not (tmp_path / "feed.json.clients.json").exists()

    def test_explicit_clients_out_is_respected(self, tmp_path):
        target = tmp_path / "my-clients.json"
        self._run(
            ["--format", "json", "--out", str(tmp_path / "j.json"),
             "--clients-out", str(target)],
            tmp_path,
        )

        assert json.loads(target.read_text(encoding="utf-8"))[0]["cipher"] == "~a"


class TestMergedClientOutput:
    """Client details ride along inside each job record."""

    def _enriched_jobs(self):
        from upwork_scraper.models.client_models import ClientInfo

        a, b = _job("~a"), _job("~b")
        a.client = ClientInfo(
            cipher="~a", country="United States", city="Sandown",
            member_since="Jun 16, 2022", total_spent=34000.0, total_hires=21,
            active_hires=4, industry="Real Estate",
            company_size="Small company (2-9 people)", proposals="Less than 5",
            interviewing=0, invites_sent=0, job_location="Worldwide",
        )
        return [a, b]

    def test_json_nests_client_inside_the_job(self):
        records = json.loads(render(self._enriched_jobs(), "json"))

        assert records[0]["client"]["country"] == "United States"
        assert records[0]["client"]["total_spent"] == 34000.0
        assert records[0]["client"]["proposals"] == "Less than 5"

    def test_unenriched_job_has_client_null(self):
        records = json.loads(render(self._enriched_jobs(), "json"))

        assert records[1]["client"] is None

    def test_job_fields_are_unchanged_alongside_client(self):
        records = json.loads(render(self._enriched_jobs(), "json"))

        assert records[0]["title"] == "Scrape, please"
        assert records[0]["budget"] == 500
        assert records[0]["cipher"] == "~a"

    def test_jsonl_carries_client_too(self):
        line = render(self._enriched_jobs(), "jsonl").splitlines()[0]

        assert json.loads(line)["client"]["city"] == "Sandown"

    def test_csv_flattens_client_into_columns(self):
        rows = list(csv.DictReader(io.StringIO(render(self._enriched_jobs(), "csv"))))

        assert rows[0]["client_country"] == "United States"
        assert rows[0]["client_total_spent"] == "34000.0"
        assert rows[0]["client_company_size"] == "Small company (2-9 people)"
        assert rows[1]["client_country"] == ""

    def test_csv_has_no_client_columns_without_enrichment(self):
        rows = list(csv.DictReader(io.StringIO(render([_job("~a")], "csv"))))

        assert list(rows[0].keys()) == CSV_FIELDS

    def test_csv_exports_every_client_field(self):
        """CSV must not quietly export less than JSON.

        An earlier CLIENT_CSV_FIELDS listed 14 of the 29 client fields and
        dropped the rest in silence — including hire_rate, rating,
        total_reviews and payment_verified, which are the whole reason the
        enrichment stage (and signing in) exists. Anyone who exported CSV
        instead of JSON lost exactly the columns they qualify a client on.
        """
        from upwork_scraper.cli import CLIENT_CSV_FIELDS
        from upwork_scraper.models.client_models import ClientInfo

        exported = set(CLIENT_CSV_FIELDS)
        available = set(ClientInfo.model_fields) | set(
            ClientInfo.model_computed_fields
        )
        # `cipher` is the join key and already on the job row.
        missing = available - exported - {"cipher"}

        assert not missing, (
            f"client fields present in JSON but absent from CSV: "
            f"{sorted(missing)}. Add them to CLIENT_CSV_FIELDS."
        )

    def test_csv_client_columns_all_resolve(self):
        """Every declared column must name a real field, not a typo."""
        from upwork_scraper.cli import CLIENT_CSV_FIELDS
        from upwork_scraper.models.client_models import ClientInfo

        known = set(ClientInfo.model_fields) | set(ClientInfo.model_computed_fields)
        unknown = [f for f in CLIENT_CSV_FIELDS if f not in known]

        assert not unknown, f"CLIENT_CSV_FIELDS names fields that do not exist: {unknown}"

    def test_csv_exposes_the_signed_in_only_fields(self):
        rows = list(csv.DictReader(io.StringIO(render(self._enriched_jobs(), "csv"))))

        for column in ("client_hire_rate", "client_rating", "client_total_reviews",
                       "client_payment_verified", "client_avg_spend_per_hire"):
            assert column in rows[0], f"{column} missing from the CSV export"

    def test_enrichment_attaches_by_cipher(self, tmp_path):
        from upwork_scraper.models.client_models import ClientInfo

        jobs = [_job("~a"), _job("~b")]
        enricher = MagicMock()
        enricher.enrich_all.return_value = [ClientInfo(cipher="~b", country="India")]
        args = build_parser().parse_args(["--out", str(tmp_path / "j.json")])

        run_enrichment(enricher, jobs, args)

        assert jobs[0].client is None
        assert jobs[1].client.country == "India"

    def test_no_separate_file_by_default(self, tmp_path):
        from upwork_scraper.models.client_models import ClientInfo

        out = tmp_path / "jobs.json"
        enricher = MagicMock()
        enricher.enrich_all.return_value = [ClientInfo(cipher="~a", country="USA")]
        args = build_parser().parse_args(["--out", str(out)])

        run_enrichment(enricher, [_job("~a")], args)

        assert not (tmp_path / "jobs.json.clients.json").exists()

    def test_separate_clients_flag_still_writes_the_file(self, tmp_path):
        from upwork_scraper.models.client_models import ClientInfo

        out = tmp_path / "jobs.json"
        enricher = MagicMock()
        enricher.enrich_all.return_value = [ClientInfo(cipher="~a", country="USA")]
        args = build_parser().parse_args(
            ["--out", str(out), "--separate-clients"]
        )

        run_enrichment(enricher, [_job("~a")], args)

        records = json.loads((tmp_path / "jobs.json.clients.json").read_text(encoding="utf-8"))
        assert records[0]["country"] == "USA"


class TestLimit:
    """--limit caps the job list itself, so enrichment covers every job kept."""

    def _args(self, argv):
        return build_parser().parse_args(argv)

    def test_absent_by_default(self):
        from upwork_scraper.cli import apply_limit

        jobs = [_job(f"~{i}") for i in range(10)]
        assert apply_limit(jobs, self._args([])) is jobs

    def test_keeps_the_newest_n(self):
        from upwork_scraper.cli import apply_limit

        jobs = [_job(f"~{i}") for i in range(10)]
        kept = apply_limit(jobs, self._args(["--limit", "3"]))

        assert [j.cipher for j in kept] == ["~0", "~1", "~2"]

    def test_shorter_list_is_untouched(self):
        from upwork_scraper.cli import apply_limit

        jobs = [_job("~a")]
        assert apply_limit(jobs, self._args(["--limit", "50"])) == jobs

    @patch("upwork_scraper.cli.UpworkScraper")
    def test_output_contains_only_the_limited_jobs(self, mock_scraper_cls, tmp_path):
        mock_scraper_cls.return_value.scrape.return_value = [
            _job(f"~{i}") for i in range(20)
        ]
        out = tmp_path / "jobs.json"

        main(["--no-proxy", "--limit", "5", "--out", str(out)])

        assert len(json.loads(out.read_text(encoding="utf-8"))) == 5

    def test_every_kept_job_gets_enriched_when_no_enrich_limit(self, tmp_path):
        from upwork_scraper.cli import apply_limit
        from upwork_scraper.models.client_models import ClientInfo

        args = self._args(["--limit", "5", "--out", str(tmp_path / "j.json")])
        jobs = apply_limit([_job(f"~{i}") for i in range(20)], args)

        enricher = MagicMock()
        enricher.enrich_all.return_value = [
            ClientInfo(cipher=j.cipher, country="USA") for j in jobs
        ]
        run_enrichment(enricher, jobs, args)

        assert len(enricher.enrich_all.call_args.args[0]) == 5
        assert all(j.client is not None for j in jobs)


class TestLoginCommands:

    def test_login_flags_default_off(self):
        args = build_parser().parse_args([])

        assert args.login is False
        assert args.login_status is False

    def test_gated_field_report_detects_unlocked(self):
        """postedCount present means hire rate became computable."""
        from upwork_scraper.cli import _report_gated_fields
        from tests.test_enrich_parsing import _payload_page

        assert _report_gated_fields(_payload_page(counts=(200, 5)), "~c") is True

    def test_gated_field_report_detects_still_locked(self):
        from upwork_scraper.cli import _report_gated_fields
        from tests.test_enrich_parsing import _payload_page

        assert _report_gated_fields(_payload_page(), "~c") is False

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli._sample_job_url", return_value="https://u/~a")
    def test_login_status_reports_signed_out(self, _url, mock_browser, capsys):
        from upwork_scraper.cli import report_login_status

        mock_browser.return_value.start.return_value.fetch.return_value = (
            200, '<html data-qa="global-signup-desktop-login"></html>'
        )
        args = build_parser().parse_args([])

        code = report_login_status(args)

        assert code == 1
        assert "Signed in: False" in capsys.readouterr().out

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli._sample_job_url", return_value="https://u/~a")
    def test_login_status_reports_signed_in(self, _url, mock_browser, capsys):
        from upwork_scraper.cli import report_login_status

        mock_browser.return_value.start.return_value.fetch.return_value = (
            200, "<html>no signup nav here</html>"
        )
        args = build_parser().parse_args([])

        code = report_login_status(args)

        assert code == 0
        assert "Signed in: True" in capsys.readouterr().out

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli._sample_job_url", return_value=None)
    def test_login_status_without_a_sample_job(self, _url, mock_browser):
        from upwork_scraper.cli import report_login_status

        assert report_login_status(build_parser().parse_args([])) == 1


class TestProfileIsolation:
    """Anonymous and signed-in runs must not share a browser profile."""

    def test_profiles_are_different_directories(self):
        from upwork_scraper.enrich.browser_fetcher import (
            DEFAULT_PROFILE_DIR,
            LOGIN_PROFILE_DIR,
            profile_for,
        )

        assert DEFAULT_PROFILE_DIR != LOGIN_PROFILE_DIR
        assert profile_for(False) == DEFAULT_PROFILE_DIR
        assert profile_for(True) == LOGIN_PROFILE_DIR

    def test_logged_in_defaults_off(self):
        assert build_parser().parse_args([]).logged_in is False

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.config")
    def test_enrichment_uses_the_anonymous_profile_by_default(
        self, mock_config, mock_browser
    ):
        from upwork_scraper.enrich.browser_fetcher import DEFAULT_PROFILE_DIR

        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11
        build_enricher(build_parser().parse_args(["--enrich-clients"]), MagicMock())

        assert mock_browser.call_args.kwargs["profile_dir"] == DEFAULT_PROFILE_DIR

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.ensure_signed_in", return_value=True)
    @patch("upwork_scraper.cli.config")
    def test_logged_in_flag_switches_profile(self, mock_config, _ensure, mock_browser):
        from upwork_scraper.enrich.browser_fetcher import LOGIN_PROFILE_DIR

        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11
        build_enricher(
            build_parser().parse_args(["--enrich-clients", "--logged-in"]), MagicMock()
        )

        assert mock_browser.call_args.kwargs["profile_dir"] == LOGIN_PROFILE_DIR

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli._sample_job_url", return_value="https://u/~a")
    def test_login_status_checks_the_signed_in_profile(self, _url, mock_browser):
        from upwork_scraper.cli import report_login_status
        from upwork_scraper.enrich.browser_fetcher import LOGIN_PROFILE_DIR

        mock_browser.return_value.start.return_value.fetch.return_value = (200, "<html>")
        report_login_status(build_parser().parse_args([]))

        assert mock_browser.call_args.kwargs["profile_dir"] == LOGIN_PROFILE_DIR


class TestProfileIsolation:
    """Anonymous and signed-in runs must not share a browser profile."""

    def test_profiles_are_different_directories(self):
        from upwork_scraper.enrich.browser_fetcher import (
            DEFAULT_PROFILE_DIR,
            LOGIN_PROFILE_DIR,
            profile_for,
        )

        assert DEFAULT_PROFILE_DIR != LOGIN_PROFILE_DIR
        assert profile_for(False) == DEFAULT_PROFILE_DIR
        assert profile_for(True) == LOGIN_PROFILE_DIR

    def test_logged_in_defaults_off(self):
        assert build_parser().parse_args([]).logged_in is False

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.config")
    def test_enrichment_uses_the_anonymous_profile_by_default(
        self, mock_config, mock_browser
    ):
        from upwork_scraper.enrich.browser_fetcher import DEFAULT_PROFILE_DIR

        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11
        build_enricher(build_parser().parse_args(["--enrich-clients"]), MagicMock())

        assert mock_browser.call_args.kwargs["profile_dir"] == DEFAULT_PROFILE_DIR

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.ensure_signed_in", return_value=True)
    @patch("upwork_scraper.cli.config")
    def test_logged_in_flag_switches_profile(self, mock_config, _ensure, mock_browser):
        from upwork_scraper.enrich.browser_fetcher import LOGIN_PROFILE_DIR

        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11
        build_enricher(
            build_parser().parse_args(["--enrich-clients", "--logged-in"]), MagicMock()
        )

        assert mock_browser.call_args.kwargs["profile_dir"] == LOGIN_PROFILE_DIR

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli._sample_job_url", return_value="https://u/~a")
    def test_login_status_checks_the_signed_in_profile(self, _url, mock_browser):
        from upwork_scraper.cli import report_login_status
        from upwork_scraper.enrich.browser_fetcher import LOGIN_PROFILE_DIR

        mock_browser.return_value.start.return_value.fetch.return_value = (200, "<html>")
        report_login_status(build_parser().parse_args([]))

        assert mock_browser.call_args.kwargs["profile_dir"] == LOGIN_PROFILE_DIR


class TestResetLogin:

    def test_flag_defaults_off(self):
        assert build_parser().parse_args([]).reset_login is False

    def test_reports_when_there_is_nothing_to_reset(self, tmp_path, capsys, monkeypatch):
        import upwork_scraper.cli as cli_mod
        from upwork_scraper.cli import reset_login_profile

        monkeypatch.setattr(cli_mod, "LOGIN_PROFILE_DIR", tmp_path / "missing")

        assert reset_login_profile() == 0
        assert "Nothing to reset" in capsys.readouterr().out

    def test_deletes_the_profile(self, tmp_path, capsys, monkeypatch):
        import upwork_scraper.cli as cli_mod
        from upwork_scraper.cli import reset_login_profile

        profile = tmp_path / "browser_profile_login"
        (profile / "Default").mkdir(parents=True)
        (profile / "session.json").write_text("[]", encoding="utf-8")
        monkeypatch.setattr(cli_mod, "LOGIN_PROFILE_DIR", profile)

        assert reset_login_profile() == 0
        assert not profile.exists()
        assert "Deleted" in capsys.readouterr().out

    @patch("upwork_scraper.cli.BrowserFetcher")
    def test_login_saves_the_session(self, mock_browser, monkeypatch, capsys):
        """The save call was missing once — this pins it."""
        import upwork_scraper.cli as cli_mod
        from upwork_scraper.cli import run_login

        started = mock_browser.return_value.start.return_value
        started.wait_for_login.return_value = True
        started.save_session.return_value = 7
        started.session_path = "C:/profile/session.json"
        monkeypatch.setattr(cli_mod, "_sample_job_url", lambda: None)

        run_login(build_parser().parse_args(["--login"]))

        assert started.save_session.called
        assert "Session saved (7 cookies)" in capsys.readouterr().out


class TestAutoLogin:
    """--logged-in should heal a dead session by itself, within limits."""

    def _args(self, extra=()):
        return build_parser().parse_args(["--enrich-clients", "--logged-in", *extra])

    @patch("upwork_scraper.cli.run_login")
    @patch("upwork_scraper.cli.probe_session_state", return_value="live")
    def test_live_session_signs_in_nothing(self, _probe, mock_login):
        from upwork_scraper.cli import ensure_signed_in

        assert ensure_signed_in(self._args()) is True
        assert mock_login.call_count == 0

    @patch("upwork_scraper.cli.run_login", return_value=0)
    @patch("upwork_scraper.cli.reset_login_profile")
    @patch("upwork_scraper.cli.probe_session_state")
    def test_signed_out_triggers_login_without_reset(
        self, mock_probe, mock_reset, mock_login
    ):
        """No session is not a broken profile — do not throw the profile away."""
        from upwork_scraper.cli import ensure_signed_in

        mock_probe.side_effect = ["signed_out", "live"]

        assert ensure_signed_in(self._args()) is True
        assert mock_login.call_count == 1
        assert mock_reset.call_count == 0

    @patch("upwork_scraper.cli.run_login", return_value=0)
    @patch("upwork_scraper.cli.reset_login_profile")
    @patch("upwork_scraper.cli.probe_session_state")
    def test_rate_limited_resets_before_signing_in(
        self, mock_probe, mock_reset, mock_login
    ):
        """A flagged profile stays flagged; sign in on a clean one."""
        from upwork_scraper.cli import ensure_signed_in

        mock_probe.side_effect = ["rate_limited", "live"]

        assert ensure_signed_in(self._args()) is True
        assert mock_reset.call_count == 1
        assert mock_login.call_count == 1

    @patch("upwork_scraper.cli.run_login", return_value=0)
    @patch("upwork_scraper.cli.probe_session_state")
    def test_only_one_sign_in_attempt(self, mock_probe, mock_login):
        """A refused sign-in must not loop."""
        from upwork_scraper.cli import ensure_signed_in

        mock_probe.side_effect = ["signed_out", "signed_out"]

        assert ensure_signed_in(self._args()) is False
        assert mock_login.call_count == 1

    @patch("upwork_scraper.cli.run_login", return_value=1)
    @patch("upwork_scraper.cli.probe_session_state", return_value="signed_out")
    def test_failed_login_falls_back(self, _probe, mock_login):
        from upwork_scraper.cli import ensure_signed_in

        assert ensure_signed_in(self._args()) is False

    @patch("upwork_scraper.cli.run_login")
    @patch("upwork_scraper.cli.probe_session_state", return_value="signed_out")
    def test_watch_mode_never_opens_a_window(self, _probe, mock_login):
        """An unattended loop must not block on a window nobody will see."""
        from upwork_scraper.cli import ensure_signed_in

        assert ensure_signed_in(self._args(["--watch"])) is False
        assert mock_login.call_count == 0

    @patch("upwork_scraper.cli.run_login")
    @patch("upwork_scraper.cli.probe_session_state", return_value="signed_out")
    def test_no_auto_login_opts_out(self, _probe, mock_login):
        from upwork_scraper.cli import ensure_signed_in

        assert ensure_signed_in(self._args(["--no-auto-login"])) is False
        assert mock_login.call_count == 0

    @patch("upwork_scraper.cli.run_login")
    @patch("upwork_scraper.cli.probe_session_state", return_value="unknown")
    def test_unknown_proceeds_without_signing_in(self, _probe, mock_login):
        """A network blip is not a reason to open a login window."""
        from upwork_scraper.cli import ensure_signed_in

        assert ensure_signed_in(self._args()) is True
        assert mock_login.call_count == 0

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.ensure_signed_in", return_value=False)
    @patch("upwork_scraper.cli.config")
    def test_failed_healing_uses_the_anonymous_profile(
        self, mock_config, _ensure, mock_browser
    ):
        from upwork_scraper.enrich.browser_fetcher import DEFAULT_PROFILE_DIR

        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11
        enricher = build_enricher(self._args(), MagicMock())

        assert enricher is not None
        assert mock_browser.call_args.kwargs["profile_dir"] == DEFAULT_PROFILE_DIR

    @patch("upwork_scraper.cli.BrowserFetcher")
    @patch("upwork_scraper.cli.ensure_signed_in", return_value=True)
    @patch("upwork_scraper.cli.config")
    def test_anonymous_runs_never_call_the_healer(
        self, mock_config, mock_ensure, mock_browser
    ):
        mock_config.ENRICH_MIN_DELAY, mock_config.ENRICH_MAX_DELAY = 4, 11
        build_enricher(build_parser().parse_args(["--enrich-clients"]), MagicMock())

        assert mock_ensure.call_count == 0


class TestSessionPrompt:
    """With someone at the keyboard, a dead session is their call to make.

    `--reset-login` then `--logged-in` silently produced an anonymous run: the
    file looked complete and every hire-rate cell was empty. Asking is the
    point — the flag is only ever passed when hire rate is what the run is for.
    """

    def _args(self, extra=()):
        return build_parser().parse_args(["--enrich-clients", "--logged-in", *extra])

    @patch("upwork_scraper.cli.can_ask", return_value=True)
    @patch("upwork_scraper.cli.run_login", return_value=0)
    @patch("upwork_scraper.cli.reset_login_profile")
    @patch("upwork_scraper.cli.probe_session_state")
    def test_choosing_reset_wipes_the_profile_then_signs_in(
        self, mock_probe, mock_reset, mock_login, _ask
    ):
        from upwork_scraper.cli import ensure_signed_in

        mock_probe.side_effect = ["signed_out", "live"]
        with patch("upwork_scraper.cli.ask_about_session", return_value="reset"):
            assert ensure_signed_in(self._args()) is True

        assert mock_reset.call_count == 1
        assert mock_login.call_count == 1

    @patch("upwork_scraper.cli.can_ask", return_value=True)
    @patch("upwork_scraper.cli.run_login", return_value=0)
    @patch("upwork_scraper.cli.reset_login_profile")
    @patch("upwork_scraper.cli.probe_session_state")
    def test_choosing_sign_in_keeps_the_profile(
        self, mock_probe, mock_reset, mock_login, _ask
    ):
        from upwork_scraper.cli import ensure_signed_in

        mock_probe.side_effect = ["signed_out", "live"]
        with patch("upwork_scraper.cli.ask_about_session", return_value="login"):
            assert ensure_signed_in(self._args()) is True

        assert mock_reset.call_count == 0
        assert mock_login.call_count == 1

    @patch("upwork_scraper.cli.can_ask", return_value=True)
    @patch("upwork_scraper.cli.run_login")
    @patch("upwork_scraper.cli.probe_session_state", return_value="signed_out")
    def test_choosing_anonymous_runs_without_signing_in(
        self, _probe, mock_login, _ask
    ):
        from upwork_scraper.cli import ensure_signed_in

        with patch("upwork_scraper.cli.ask_about_session", return_value="anonymous"):
            assert ensure_signed_in(self._args()) is False
        assert mock_login.call_count == 0

    @patch("upwork_scraper.cli.can_ask", return_value=True)
    @patch("upwork_scraper.cli.run_login")
    @patch("upwork_scraper.cli.probe_session_state", return_value="signed_out")
    def test_choosing_stop_aborts_the_run(self, _probe, mock_login, _ask):
        from upwork_scraper.cli import RunAborted, ensure_signed_in

        with patch("upwork_scraper.cli.ask_about_session", return_value="stop"):
            with pytest.raises(RunAborted):
                ensure_signed_in(self._args())
        assert mock_login.call_count == 0

    @patch("upwork_scraper.cli.can_ask", return_value=True)
    @patch("upwork_scraper.cli.probe_session_state", return_value="unknown")
    def test_an_unconfirmed_session_asks_too(self, _probe, _ask):
        """'Could not confirm' is exactly the state that burned this before."""
        from upwork_scraper.cli import ensure_signed_in

        with patch("upwork_scraper.cli.ask_about_session") as mock_ask:
            mock_ask.return_value = "anonymous"
            ensure_signed_in(self._args())

        assert mock_ask.call_args.args[0] == "unknown"

    @patch("upwork_scraper.cli.probe_session_state", return_value="signed_out")
    def test_watch_mode_never_asks(self, _probe):
        """An unattended loop must not stop on a question nobody will read."""
        from upwork_scraper.cli import can_ask

        assert can_ask(self._args(["--watch"])) is False
        assert can_ask(self._args(["--no-auto-login"])) is False

    def test_a_redirected_stdin_is_not_a_keyboard(self):
        from upwork_scraper.cli import can_ask

        with patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            assert can_ask(self._args()) is False
            stdin.isatty.return_value = True
            assert can_ask(self._args()) is True

    def test_the_default_answer_depends_on_the_diagnosis(self, monkeypatch, capsys):
        """Signing in on top of a flagged profile just reproduces the block."""
        from upwork_scraper.cli import ask_about_session

        monkeypatch.setattr("builtins.input", lambda _: "")
        assert ask_about_session("signed_out") == "login"
        assert ask_about_session("rate_limited") == "reset"

    def test_an_unreadable_answer_does_not_pick_anonymous(self, monkeypatch):
        from upwork_scraper.cli import ask_about_session

        monkeypatch.setattr("builtins.input", lambda _: "yes please")
        assert ask_about_session("signed_out") == "login"

    def test_ctrl_c_at_the_prompt_stops(self, monkeypatch):
        from upwork_scraper.cli import ask_about_session

        def interrupt(_):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", interrupt)
        assert ask_about_session("signed_out") == "stop"

    def test_every_option_is_reachable_by_number(self, monkeypatch):
        from upwork_scraper.cli import SESSION_OPTIONS, ask_about_session

        for i, (key, _) in enumerate(SESSION_OPTIONS, 1):
            monkeypatch.setattr("builtins.input", lambda _, n=i: str(n))
            assert ask_about_session("signed_out") == key


class TestAbortedRun:
    @patch("upwork_scraper.cli.build_enricher")
    def test_main_reports_the_abort_and_scrapes_nothing(self, mock_build, capsys):
        from upwork_scraper.cli import RunAborted, main

        mock_build.side_effect = RunAborted("Stopped before scraping.")

        assert main(["--enrich-clients", "--logged-in"]) == 3
        assert "Stopped before scraping." in capsys.readouterr().err


class TestEnrichmentDeliveredWhatItPromised:
    """A signed-in run that produced no hire rate did not do its job."""

    def _args(self):
        args = build_parser().parse_args(["--enrich-clients", "--logged-in"])
        args.logged_in_active = True
        return args

    def _result(self, cipher, hire_rate):
        from upwork_scraper.models.client_models import ClientInfo

        return ClientInfo(cipher=cipher, hire_rate=hire_rate, fetch_status="ok")

    def test_warns_when_every_hire_rate_is_empty(self, caplog):
        from upwork_scraper.cli import run_enrichment

        enricher = MagicMock()
        enricher.enrich_all.return_value = [self._result("a", None)]
        jobs = [MagicMock(cipher="a")]

        with caplog.at_level(logging.WARNING):
            run_enrichment(enricher, jobs, self._args())

        assert "not one client came back with a hire rate" in caplog.text

    def test_silent_when_the_session_delivered(self, caplog):
        from upwork_scraper.cli import run_enrichment

        enricher = MagicMock()
        enricher.enrich_all.return_value = [
            self._result("a", None), self._result("b", 89),
        ]
        jobs = [MagicMock(cipher="a"), MagicMock(cipher="b")]

        with caplog.at_level(logging.WARNING):
            run_enrichment(enricher, jobs, self._args())

        assert "not one client came back" not in caplog.text
