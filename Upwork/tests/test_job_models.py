from datetime import datetime, timezone

import pytest

from upwork_scraper.models.job_models import Job, JobList


def _make_graphql_result(**overrides):
    """Build a minimal GraphQL result dict for a single job."""
    base = {
        "title": "Build a REST API",
        "description": "We need a Python developer...",
        "ontologySkills": [
            {"prefLabel": "Python"},
            {"prefLabel": "Django"},
        ],
        "jobTile": {
            "job": {
                "ciphertext": "~abc123cipher",
                "jobType": "HOURLY",
                "publishTime": 1700000000000,
                "hourlyBudgetMin": "25",
                "hourlyBudgetMax": "50",
                "contractorTier": "2",
                "hourlyEngagementDuration": {"weeks": 12},
                "fixedPriceAmount": None,
                "fixedPriceEngagementDuration": None,
            }
        },
    }
    base.update(overrides)
    return base


def _wrap_in_graphql_envelope(results: list[dict]) -> dict:
    return {
        "data": {
            "search": {
                "universalSearchNuxt": {
                    "visitorJobSearchV1": {
                        "results": results,
                    }
                }
            }
        }
    }


class TestJobParsing:

    def test_basic_hourly_job(self):
        job = Job.model_validate(_make_graphql_result())

        assert job.title == "Build a REST API"
        assert job.cipher == "~abc123cipher"
        assert job.link == "https://www.upwork.com/jobs/~abc123cipher"
        assert job.is_hourly is True
        assert job.hourly_low == 25
        assert job.hourly_high == 50
        assert job.budget is None
        assert job.duration_weeks == 12
        assert job.contractor_tier == "2"
        assert job.skills == ["Python", "Django"]
        assert job.job_type == "HOURLY"

    def test_fixed_price_job(self):
        data = _make_graphql_result()
        data["jobTile"]["job"]["jobType"] = "FIXED"
        data["jobTile"]["job"]["hourlyBudgetMin"] = None
        data["jobTile"]["job"]["hourlyBudgetMax"] = None
        data["jobTile"]["job"]["hourlyEngagementDuration"] = None
        data["jobTile"]["job"]["fixedPriceAmount"] = {"amount": "5000"}
        data["jobTile"]["job"]["fixedPriceEngagementDuration"] = {"weeks": 4}

        job = Job.model_validate(data)

        assert job.is_hourly is False
        assert job.budget == 5000
        assert job.hourly_low is None
        assert job.hourly_high is None
        assert job.duration_weeks == 4

    def test_timestamp_milliseconds(self):
        data = _make_graphql_result()
        data["jobTile"]["job"]["publishTime"] = 1700000000000

        job = Job.model_validate(data)
        assert job.published_date == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)

    def test_timestamp_seconds(self):
        data = _make_graphql_result()
        data["jobTile"]["job"]["publishTime"] = 1700000000

        job = Job.model_validate(data)
        assert job.published_date == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)

    def test_cipher_extracted_from_link(self):
        data = _make_graphql_result()
        data["jobTile"]["job"]["ciphertext"] = "~xyz789"

        job = Job.model_validate(data)
        assert job.cipher == "~xyz789"
        assert job.link == "https://www.upwork.com/jobs/~xyz789"

    def test_skills_from_dict_list(self):
        data = _make_graphql_result()
        data["ontologySkills"] = [
            {"prefLabel": "React"},
            {"prefLabel": ""},
            {"prefLabel": "TypeScript"},
        ]

        job = Job.model_validate(data)
        assert job.skills == ["React", "TypeScript"]

    def test_skills_empty_list(self):
        data = _make_graphql_result()
        data["ontologySkills"] = []

        job = Job.model_validate(data)
        assert job.skills is None

    def test_skills_none(self):
        data = _make_graphql_result()
        data["ontologySkills"] = None

        job = Job.model_validate(data)
        assert job.skills is None

    def test_str2int_converts_float_strings(self):
        data = _make_graphql_result()
        data["jobTile"]["job"]["hourlyBudgetMin"] = "25.00"
        data["jobTile"]["job"]["hourlyBudgetMax"] = "75.50"

        job = Job.model_validate(data)
        assert job.hourly_low == 25
        assert job.hourly_high == 75

    def test_str2int_handles_none(self):
        data = _make_graphql_result()
        data["jobTile"]["job"]["hourlyBudgetMin"] = None
        data["jobTile"]["job"]["hourlyBudgetMax"] = None

        job = Job.model_validate(data)
        assert job.hourly_low is None
        assert job.hourly_high is None

    def test_nullable_fields(self):
        data = _make_graphql_result()
        data["title"] = None
        data["description"] = None
        data["jobTile"]["job"]["contractorTier"] = None
        data["jobTile"]["job"]["hourlyEngagementDuration"] = None

        job = Job.model_validate(data)
        assert job.title is None
        assert job.description is None
        assert job.contractor_tier is None
        assert job.duration_weeks is None


class TestJobList:

    def test_parses_graphql_envelope(self):
        results = [_make_graphql_result(), _make_graphql_result()]
        envelope = _wrap_in_graphql_envelope(results)

        job_list = JobList.model_validate(envelope)
        assert len(job_list.jobs) == 2

    def test_empty_results(self):
        envelope = _wrap_in_graphql_envelope([])
        job_list = JobList.model_validate(envelope)
        assert job_list.jobs == []
