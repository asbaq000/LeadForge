from datetime import datetime, timezone

from upwork_scraper.models.client_models import ClientInfo

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    Field,
    field_validator,
    model_validator,
)


class Job(BaseModel):

    title: str | None = Field(validation_alias=AliasPath("title"))
    link: str = Field(validation_alias=AliasPath("jobTile", "job", "ciphertext"))
    cipher: str | None = None
    description: str | None = Field(validation_alias=AliasPath("description"))
    skills: list[str] | None = Field(
        default=None, validation_alias=AliasPath("ontologySkills")
    )
    published_date: datetime = Field(
        validation_alias=AliasPath("jobTile", "job", "publishTime")
    )
    job_type: str = Field(validation_alias=AliasPath("jobTile", "job", "jobType"))
    is_hourly: bool | None = None
    hourly_low: int | str | None = Field(
        default=None,
        validation_alias=AliasPath("jobTile", "job", "hourlyBudgetMin"),
    )
    hourly_high: int | str | None = Field(
        default=None,
        validation_alias=AliasPath("jobTile", "job", "hourlyBudgetMax"),
    )
    budget: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            AliasPath("jobTile", "job", "fixedPriceAmount", "amount"),
            AliasPath("jobTile", "job", "fixedPriceAmount"),
        ),
    )
    duration_weeks: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            AliasPath("jobTile", "job", "hourlyEngagementDuration", "weeks"),
            AliasPath("jobTile", "job", "fixedPriceEngagementDuration", "weeks"),
        ),
    )
    contractor_tier: str | None = Field(
        default=None,
        validation_alias=AliasPath("jobTile", "job", "contractorTier"),
    )
    # Set by the scraper, not by Upwork: which keyword(s) surfaced this job.
    matched_query: str | None = None

    # Filled by the separate enrichment stage when it runs. None means the job
    # was never enriched — distinct from an enrichment that found nothing.
    client: ClientInfo | None = None

    def age_seconds(self, now: datetime | None = None) -> float:
        """Seconds since the job was published."""
        now = now or datetime.now(timezone.utc)
        return (now - self.published_date).total_seconds()

    @model_validator(mode="after")
    def _derive_fields(self):
        self.cipher = self.link.split("/")[-1]

        if self.job_type == "HOURLY":
            self.is_hourly = True
        elif self.job_type == "FIXED":
            self.is_hourly = False

        self.link = "https://www.upwork.com/jobs/" + self.link
        return self

    @field_validator("published_date", mode="before")
    @classmethod
    def _parse_timestamp(cls, value):
        if isinstance(value, (int, float)):
            if value > 1e12:
                value = value / 1000
            return datetime.fromtimestamp(value, tz=timezone.utc)
        return value

    @field_validator("hourly_low", "hourly_high", "budget", mode="after")
    @classmethod
    def _str2int(cls, value):
        if value is None:
            return None
        return int(float(value))

    @field_validator("skills", mode="before")
    @classmethod
    def _extract_skill_labels(cls, value):
        if not value:
            return None
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return [s.get("prefLabel", "") for s in value if s.get("prefLabel")]
        return value


class JobList(BaseModel):

    jobs: list[Job] = Field(
        validation_alias=AliasPath(
            "data",
            "search",
            "universalSearchNuxt",
            "visitorJobSearchV1",
            "results",
        )
    )
    # How many jobs the search matched overall — lets the fetcher stop early
    # instead of requesting pages that cannot exist.
    total: int | None = Field(
        default=None,
        validation_alias=AliasPath(
            "data",
            "search",
            "universalSearchNuxt",
            "visitorJobSearchV1",
            "paging",
            "total",
        ),
    )
