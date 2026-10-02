import json

import pytest

from upwork_scraper.models.job_models import Job
from upwork_scraper.niches import (
    Niche,
    available_niches,
    load_niche,
    read_keywords_file,
)


def _job(title, skills=None) -> Job:
    return Job.model_validate(
        {
            "title": title,
            "description": "some description",
            "ontologySkills": [{"prefLabel": s} for s in (skills or [])],
            "jobTile": {
                "job": {
                    "ciphertext": "~c1",
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
    )


class TestVideoNichePreset:

    def test_ships_with_the_package(self):
        assert "video" in available_niches()

    def test_loads_keywords_and_terms(self):
        niche = load_niche("video")

        assert niche.name == "video"
        assert "video editing" in niche.keywords
        assert "youtube" in niche.keywords
        assert niche.match_terms and niche.exclude_terms

    @pytest.mark.parametrize(
        "title",
        [
            "YouTube Video Editor & Post-Production Editor",
            "Creative Short-Form Beauty Video Editor",
            "Mumbai-Based Videographer + Video Editor for Social Media",
            "Motion Designer/Animator for Startup",
            "Youtube Thumbnails",
            "UGC Editor",
            "Shorts Video Editor for Content Creator",
            "Podcast editing and audio cleanup",
        ],
    )
    def test_keeps_real_video_jobs(self, title):
        assert load_niche("video").is_relevant(_job(title)) is True

    @pytest.mark.parametrize(
        "title",
        [
            "Executive Assistant",
            "Facebook Ads Specialist for App Installs",
            "LinkedIn Growth Manager - Build My Personal Brand",
            "Affiliate Program Builder for Wellness Retreat Center",
            "Health & Wellness Partner Outreach Specialist",
            "Account Manager",
            "Web2App Quiz Funnel Expert",
        ],
    )
    def test_drops_off_niche_jobs(self, title):
        assert load_niche("video").is_relevant(_job(title)) is False

    def test_matches_on_skills_too(self):
        niche = load_niche("video")

        assert niche.is_relevant(_job("Freelancer needed", ["Video Editing"])) is True

    def test_excludes_are_title_only(self):
        """A real video job can carry an unrelated skill tag."""
        niche = load_niche("video")
        job = _job("YouTube Video Editor", ["Lead Generation", "Video Editing"])

        assert niche.is_relevant(job) is True

    def test_exclusion_beats_a_match_in_the_title(self):
        niche = load_niche("video")

        assert niche.is_relevant(_job("Web developer for video site")) is False

    def test_handles_missing_title_and_skills(self):
        assert load_niche("video").is_relevant(_job(None)) is False


class TestNicheBehaviour:

    def test_no_match_terms_keeps_everything(self):
        niche = Niche(name="all", keywords=["x"])

        assert niche.is_relevant(_job("Anything at all")) is True

    def test_with_extra_keywords_appends(self):
        niche = Niche(name="n", keywords=["a"]).with_extra_keywords(["b", "c"])

        assert niche.keywords == ["a", "b", "c"]

    def test_with_extra_keywords_ignores_duplicates(self):
        niche = Niche(name="n", keywords=["Video Editing"]).with_extra_keywords(
            ["video editing", "new one"]
        )

        assert niche.keywords == ["Video Editing", "new one"]

    def test_with_extra_keywords_none_is_a_noop(self):
        niche = Niche(name="n", keywords=["a"])

        assert niche.with_extra_keywords(None) is niche

    def test_extra_keywords_keep_the_filter(self):
        niche = load_niche("video").with_extra_keywords(["drone"])

        assert "drone" in niche.keywords
        assert niche.is_relevant(_job("Executive Assistant")) is False


class TestLoading:

    def test_loads_a_custom_file(self, tmp_path):
        path = tmp_path / "mine.json"
        path.write_text(
            json.dumps({"name": "mine", "keywords": ["logo design"]}), encoding="utf-8"
        )

        niche = load_niche(str(path))

        assert niche.name == "mine"
        assert niche.keywords == ["logo design"]

    def test_unknown_name_lists_built_ins(self):
        with pytest.raises(FileNotFoundError, match="video"):
            load_niche("does-not-exist")

    def test_file_without_keywords_is_rejected(self, tmp_path):
        path = tmp_path / "empty.json"
        path.write_text(json.dumps({"name": "x", "keywords": []}), encoding="utf-8")

        with pytest.raises(ValueError, match="no 'keywords'"):
            load_niche(str(path))


class TestKeywordsFile:

    def test_reads_one_per_line(self, tmp_path):
        path = tmp_path / "kw.txt"
        path.write_text("video editing\nyoutube shorts\n", encoding="utf-8")

        assert read_keywords_file(str(path)) == ["video editing", "youtube shorts"]

    def test_ignores_comments_and_blanks(self, tmp_path):
        path = tmp_path / "kw.txt"
        path.write_text(
            "# my keywords\n\nvideo editing  # inline comment\n\n  tiktok\n",
            encoding="utf-8",
        )

        assert read_keywords_file(str(path)) == ["video editing", "tiktok"]
