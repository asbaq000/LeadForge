"""Job-poster and job-activity details, filled by the separate enrichment stage.

Field availability was checked against real logged-out job pages on 2026-08-12:

  Public (visible without logging in):
    member_since, country, city, local_time, job_location,
    proposals, last_viewed, interviewing, invites_sent, unanswered_invites

  Login-only (absent from the public page):
    total_spent, hire_rate, total_hires, total_reviews, rating, payment_verified

Everything is optional — a brand-new client has almost none of it.
"""

from pydantic import BaseModel, computed_field


class ClientInfo(BaseModel):
    # Join key back to the job
    cipher: str

    # --- Public: who the client is -------------------------------------
    member_since: str | None = None
    country: str | None = None
    city: str | None = None
    local_time: str | None = None       # clock time, e.g. "4:00 PM"
    timezone: str | None = None         # zone, e.g. "Europe/Prague (UTC+02:00)"

    # --- Public: competition on this job -------------------------------
    proposals: str | None = None            # bucketed, e.g. "Less than 5", "5 to 10"
    last_viewed: str | None = None          # e.g. "5 minutes ago"
    interviewing: int | None = None
    invites_sent: int | None = None
    unanswered_invites: int | None = None

    # --- Public: where the job wants the freelancer --------------------
    job_location: str | None = None         # e.g. "Worldwide"

    # --- Public, but only once a client has history --------------------
    # A brand-new client shows none of these; an established one shows
    # "$3.8K total spent", "80 hires, 8 active", industry and company size.
    total_spent: float | None = None
    total_hires: int | None = None
    active_hires: int | None = None
    total_hours: float | None = None
    total_jobs_with_hires: int | None = None
    avg_hourly_rate_paid: float | None = None
    is_top_client: bool | None = None
    total_reviews: int | None = None
    rating: float | None = None
    open_jobs: int | None = None
    industry: str | None = None
    company_size: str | None = None

    # --- Signed-in only ------------------------------------------------
    # `postedCount` is the denominator of the hire rate. Upwork leaves it null
    # for logged-out visitors, so anonymous runs cannot compute a hire rate;
    # a signed-in session gets both and `hire_rate` is derived from them.
    total_posted_jobs: int | None = None
    hire_rate: int | None = None
    payment_verified: bool | None = None

    # Bookkeeping for the run that produced this
    fetch_status: str = "ok"                # ok | failed | blocked | skipped
    fetch_error: str | None = None

    @computed_field
    @property
    def avg_spend_per_hire(self) -> float | None:
        """Average money committed per hire, rounded to the nearest unit.

        The public page has no hire rate (that needs jobs-posted, which is
        logged-in only), so this is the usable quality signal: a client at
        $515/hire is buying different work than one at $25/hire.
        """
        if not self.total_spent or not self.total_hires:
            return None
        return round(self.total_spent / self.total_hires, 2)

    @computed_field
    @property
    def hires_per_job(self) -> float | None:
        """Freelancers hired per job that resulted in a hire.

        Not a hire rate — the denominator here is jobs they *did* hire on, not
        jobs posted (Upwork withholds that). Above ~2 means they staff a
        posting with several people rather than picking one.
        """
        if not self.total_hires or not self.total_jobs_with_hires:
            return None
        return round(self.total_hires / self.total_jobs_with_hires, 2)

    @computed_field
    @property
    def has_ever_hired(self) -> bool | None:
        """Whether this client has hired anyone, ever.

        The one hire-rate-shaped question public data can answer: jobs-with-
        hires of 0 means every posting so far went unfilled.
        """
        if self.total_jobs_with_hires is None:
            return None
        return self.total_jobs_with_hires > 0

    @computed_field
    @property
    def active_hire_share(self) -> float | None:
        """Share of hires currently active — how busy the client is right now."""
        if not self.total_hires or self.active_hires is None:
            return None
        return round(self.active_hires / self.total_hires, 3)

    @property
    def is_usable(self) -> bool:
        return self.fetch_status == "ok" and any(
            v is not None
            for v in (
                self.member_since, self.country, self.proposals,
                self.job_location, self.total_spent,
            )
        )
