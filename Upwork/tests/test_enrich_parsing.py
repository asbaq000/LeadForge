"""Parser and enricher tests, written against real logged-out Upwork markup.

The fixtures below were captured from live job pages on 2026-08-12 via a
browser, so the field names and nesting are the real ones, not invented.
"""

from upwork_scraper.enrich import ClientEnricher, CircuitBreaker, HumanDelay
from upwork_scraper.enrich.client_fetcher import parse_client_info
from upwork_scraper.models.job_models import Job

from tests.test_enrich import FakeSleep


def _job(cipher="~c1") -> Job:
    return Job.model_validate(
        {
            "title": "Video Editor",
            "description": "d",
            "ontologySkills": None,
            "jobTile": {
                "job": {
                    "ciphertext": cipher,
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


# Real structure from a logged-out job page (client: Australia, member Aug 2025).
REAL_PAGE = """
<html><body>
<h1>Experienced Video Editor for 41 Educational Course Videos</h1>
<div>Posted 6 minutes ago</div>
<div>
Worldwide
</div>
<section><h5>Activity on this job</h5>
  <ul>
    <li><span>Proposals:</span>
        <button aria-label="Close the tooltip">Close the tooltip</button>
        <span>5 to 10</span></li>
    <li><span>Last viewed by client:</span>
        <button aria-label="Close the tooltip">Close the tooltip</button>
        <span>5 minutes ago</span></li>
    <li><span>Interviewing:</span><span>0</span></li>
    <li><span>Invites sent:</span><span>2</span></li>
    <li><span>Unanswered invites:</span><span>2</span></li>
  </ul>
</section>
<div class="cfe-about-client-v2 air3-card-section py-4x" data-v-9bcfd89c="" data-v-f5f49154="">
  <h5 class="mb-4 d-flex" data-v-f5f49154="">About the client</h5>
  <div class="text-light-on-muted mb-3" data-qa="client-contract-date" data-v-f5f49154="">
    <small data-v-f5f49154="">Member since Aug 1, 2025</small>
  </div>
  <ul class="ac-items list-unstyled rr-mask">
    <li data-qa="client-location">
      <strong>Australia</strong>
      <div><span>Sydney</span><span>4:00 PM</span></div>
    </li>
    <li data-qa="client-company-profile"></li>
  </ul>
</div>
</body></html>
"""

# Brand-new client, same structure (real values: India / Noida).
NEW_CLIENT_PAGE = """
<div class="cfe-about-client-v2">
  <div data-qa="client-contract-date"><small>Member since Aug 11, 2026</small></div>
  <ul class="ac-items"><li data-qa="client-location">
    <strong>India</strong><div><span>Noida</span><span>11:54 AM</span></div>
  </li></ul>
</div>
<div><span>Proposals:</span><span>Less than 5</span></div>
<div><span>Interviewing:</span><span>0</span></div>
"""


class TestParseRealPage:

    def test_member_since(self):
        assert parse_client_info(REAL_PAGE, "~c1").member_since == "Aug 1, 2025"

    def test_country_city_and_local_time(self):
        info = parse_client_info(REAL_PAGE, "~c1")

        assert info.country == "Australia"
        assert info.city == "Sydney"
        assert info.local_time == "4:00 PM"

    def test_competition_on_the_job(self):
        info = parse_client_info(REAL_PAGE, "~c1")

        assert info.proposals == "5 to 10"
        assert info.last_viewed == "5 minutes ago"
        assert info.interviewing == 0
        assert info.invites_sent == 2
        assert info.unanswered_invites == 2

    def test_job_location_requirement(self):
        assert parse_client_info(REAL_PAGE, "~c1").job_location == "Worldwide"

    def test_login_only_fields_stay_none(self):
        """The public page has no spend/rating/hire rate — must not invent them."""
        info = parse_client_info(REAL_PAGE, "~c1")

        assert info.total_spent is None
        assert info.hire_rate is None
        assert info.rating is None
        assert info.payment_verified is None

    def test_record_is_usable(self):
        assert parse_client_info(REAL_PAGE, "~c1").is_usable is True

    def test_brand_new_client(self):
        info = parse_client_info(NEW_CLIENT_PAGE, "~c2")

        assert info.member_since == "Aug 11, 2026"
        assert (info.country, info.city) == ("India", "Noida")
        assert info.local_time == "11:54 AM"
        assert info.proposals == "Less than 5"

    def test_empty_page_yields_nothing_not_an_error(self):
        info = parse_client_info("<html></html>", "~c3")

        assert info.country is None
        assert info.is_usable is False

    def test_cloudflare_page_is_not_usable(self):
        info = parse_client_info("<html><title>Just a moment...</title></html>", "~c4")

        assert info.is_usable is False


class TestParseLoggedInExtras:
    """Fields only a logged-in session sees — parsed if they ever appear."""

    PAGE = """
    <div>$12,500 total spent</div><div>85% hire rate</div>
    <div>Rating 4.8 of 5</div><div>Payment method verified</div>
    """

    def test_total_spent(self):
        assert parse_client_info(self.PAGE, "~c").total_spent == 12500.0

    def test_abbreviated_spend(self):
        assert parse_client_info("<div>$10K+ total spent</div>", "~c").total_spent == 10000.0

    def test_hire_rate_and_rating(self):
        info = parse_client_info(self.PAGE, "~c")

        assert info.hire_rate == 85
        assert info.rating == 4.8

    def test_payment_verified(self):
        assert parse_client_info(self.PAGE, "~c").payment_verified is True


class TestEnricherWithPluggableTransport:

    def _enricher(self, fetcher, **breaker_kw):
        return ClientEnricher(
            html_fetcher=fetcher,
            delay=HumanDelay(sleeper=lambda s: None),
            breaker=CircuitBreaker(sleeper=lambda s: None, **breaker_kw),
        )

    def test_browser_fetcher_produces_a_record(self):
        info = self._enricher(lambda url: REAL_PAGE).enrich(_job())

        assert info.fetch_status == "ok"
        assert info.country == "Australia"

    def test_accepts_status_and_html_pair(self):
        assert self._enricher(lambda url: (200, REAL_PAGE)).enrich(_job()).fetch_status == "ok"

    def test_fetcher_receives_the_job_url(self):
        seen = []

        def fetch(url):
            seen.append(url)
            return REAL_PAGE

        self._enricher(fetch).enrich(_job("~abc"))

        assert seen == ["https://www.upwork.com/jobs/~abc"]

    def test_403_is_reported_as_blocked_with_a_useful_message(self):
        info = self._enricher(lambda url: (403, "")).enrich(_job())

        assert info.fetch_status == "blocked"
        assert "Cloudflare" in info.fetch_error

    def test_other_http_errors_fail(self):
        info = self._enricher(lambda url: (500, "")).enrich(_job())

        assert info.fetch_status == "failed"
        assert "500" in info.fetch_error

    def test_fetcher_exception_is_a_skip(self):
        def boom(url):
            raise ConnectionError("proxy died")

        info = self._enricher(boom).enrich(_job())

        assert info.fetch_status == "failed"
        assert "ConnectionError" in info.fetch_error

    def test_empty_page_is_a_failure(self):
        info = self._enricher(lambda url: (200, "<html></html>")).enrich(_job())

        assert info.fetch_status == "failed"
        assert "No client fields" in info.fetch_error

    def test_batch_skips_failures_and_keeps_going(self):
        pages = iter([REAL_PAGE, (403, ""), REAL_PAGE])
        enricher = self._enricher(lambda url: next(pages), trip_after=5)

        results = enricher.enrich_all([_job("~a"), _job("~b"), _job("~c")])

        assert [r.fetch_status for r in results] == ["ok", "blocked", "ok"]

    def test_batch_stops_once_the_breaker_trips(self):
        enricher = self._enricher(lambda url: (403, ""), trip_after=3)

        results = enricher.enrich_all([_job(f"~{i}") for i in range(20)])

        assert len(results) == 3

    def test_default_transport_is_flagged_as_not_a_browser(self):
        assert ClientEnricher().uses_browser is False
        assert ClientEnricher(html_fetcher=lambda u: "").uses_browser is True

    def test_waits_between_requests(self):
        sleep = FakeSleep()
        enricher = ClientEnricher(
            html_fetcher=lambda url: REAL_PAGE,
            delay=HumanDelay(4, 11, long_pause_every=0, sleeper=sleep),
        )

        enricher.enrich_all([_job(f"~{i}") for i in range(5)])

        assert len(sleep.calls) == 4
        assert all(4 <= s <= 11 for s in sleep.calls)


class TestLiveRegressions:
    """Bugs the live run exposed that the first fixtures missed."""

    def test_tooltip_button_is_not_mistaken_for_the_value(self):
        """A "Close the tooltip" button sits between label and value."""
        page = """
        <div>Proposals:<button>Close the tooltip</button><span>Less than 5</span></div>
        <div>Last viewed by client:<button>Close the tooltip</button>
             <span>2 hours ago</span></div>
        """
        info = parse_client_info(page, "~c")

        assert info.proposals == "Less than 5"
        assert info.last_viewed == "2 hours ago"

    def test_city_never_captures_markup(self):
        """A fixed-length window can cut mid-tag; markup must not leak through."""
        page = """
        <li data-qa="client-location"><strong>India</strong>
          <div><span>Noida</span><span>12:25 PM</span></div></li>
        <div class="air3-card-section another-very-long-class-name-that-gets-cut
        """
        info = parse_client_info(page, "~c")

        assert info.city == "Noida"
        assert "<" not in (info.city or "")
        assert "class=" not in (info.city or "")

    def test_country_code_form(self):
        page = ('<li data-qa="client-location"><strong>NLD</strong>'
                '<div><span>Amstelveen</span><span>8:56 AM</span></div></li>')
        info = parse_client_info(page, "~c")

        assert (info.country, info.city) == ("NLD", "Amstelveen")

    def test_us_only_job_location(self):
        page = "<div>\nOnly freelancers located in the U.S. may apply.\n</div>"

        assert parse_client_info(page, "~c").job_location == (
            "Only freelancers located in the U.S. may apply."
        )

    def test_proposal_buckets(self):
        for raw, expected in [
            ("Proposals:<span>Less than 5</span>", "Less than 5"),
            ("Proposals:<span>5 to 10</span>", "5 to 10"),
            ("Proposals:<span>20 to 50</span>", "20 to 50"),
            ("Proposals:<span>50+</span>", "50+"),
        ]:
            assert parse_client_info(raw, "~c").proposals == expected

    def test_missing_last_viewed_is_none_not_noise(self):
        page = "<div>Last viewed by client:<button>Close the tooltip</button></div>"

        assert parse_client_info(page, "~c").last_viewed is None


# Real "About the client" block for an established client, captured live.
ESTABLISHED_CLIENT_PAGE = """
<section><h5>Activity on this job</h5>
  <div>Proposals:<button>Close the tooltip</button>
    <p>This range includes relevant proposals, but does not include proposals
       that are withdrawn, declined, or archived.</p>
    <span>Less than 5</span></div>
  <div>Interviewing:<span>0</span></div>
  <div>Invites sent:<span>0</span></div>
  <div>Unanswered invites:<span>0</span></div>
</section>
<div class="cfe-about-client-v2">
  <h5>About the client</h5>
  <div data-qa="client-contract-date"><small>Member since Aug 1, 2024</small></div>
  <ul class="ac-items"><li data-qa="client-location">
    <strong>USA</strong><div><span>Ewa Beach</span><span>9:01 PM</span></div>
  </li></ul>
  <div><span>$3.8K</span><span> total spent</span></div>
  <div>80 hires, 8 active</div>
  <div>Art &amp; Design</div>
  <div>Small company (2-9 people)</div>
</div>
<div>Explore similar jobs on Upwork</div>
<div>Proposals that win funding</div>
"""


class TestEstablishedClient:
    """Spend, hires, industry and company size are public once a client has history."""

    def test_total_spent_with_k_suffix(self):
        assert parse_client_info(ESTABLISHED_CLIENT_PAGE, "~c").total_spent == 3800.0

    def test_hires_and_active(self):
        info = parse_client_info(ESTABLISHED_CLIENT_PAGE, "~c")

        assert info.total_hires == 80
        assert info.active_hires == 8

    def test_company_size(self):
        info = parse_client_info(ESTABLISHED_CLIENT_PAGE, "~c")

        assert info.company_size == "Small company (2-9 people)"

    def test_industry(self):
        assert parse_client_info(ESTABLISHED_CLIENT_PAGE, "~c").industry == "Art & Design"

    def test_location_and_member_since(self):
        info = parse_client_info(ESTABLISHED_CLIENT_PAGE, "~c")

        assert (info.country, info.city) == ("USA", "Ewa Beach")
        assert info.member_since == "Aug 1, 2024"

    def test_proposals_ignores_marketing_copy_elsewhere_on_the_page(self):
        """"Proposals that win funding" appears in the site nav."""
        info = parse_client_info(ESTABLISHED_CLIENT_PAGE, "~c")

        assert info.proposals == "Less than 5"

    def test_new_client_has_no_history_fields(self):
        info = parse_client_info(NEW_CLIENT_PAGE, "~c")

        assert info.total_spent is None
        assert info.total_hires is None
        assert info.company_size is None

    def test_spend_variants(self):
        for raw, expected in [
            ("$3.8K total spent", 3800.0),
            ("$10K+ total spent", 10000.0),
            ("$1.2M total spent", 1200000.0),
            ("$450 total spent", 450.0),
            ("$12,500 total spent", 12500.0),
        ]:
            page = f'<div><h5>About the client</h5><span>{raw}</span></div>'
            assert parse_client_info(page, "~c").total_spent == expected


class TestClientHistoryMetrics:
    """Spend, hires, hours — and what can stand in for a hire rate."""

    PAGE = """
    <div><h5>About the client</h5>
      <span>$34K</span><span> total spent</span>
      <div>66 hires, 33 active</div>
      <div>104 hours</div>
    </div><div>Explore similar jobs</div>
    """

    def test_total_hours(self):
        assert parse_client_info(self.PAGE, "~c").total_hours == 104.0

    def test_total_hours_with_thousands_separator(self):
        page = self.PAGE.replace("104 hours", "3,805 hours")

        assert parse_client_info(page, "~c").total_hours == 3805.0

    def test_no_hours_for_fixed_price_only_clients(self):
        page = self.PAGE.replace("<div>104 hours</div>", "")

        assert parse_client_info(page, "~c").total_hours is None

    def test_avg_spend_per_hire_is_computed(self):
        info = parse_client_info(self.PAGE, "~c")

        assert info.total_spent == 34000.0 and info.total_hires == 66
        assert info.avg_spend_per_hire == 515.15

    def test_active_hire_share_is_computed(self):
        assert parse_client_info(self.PAGE, "~c").active_hire_share == 0.5

    def test_computed_fields_are_serialised(self):
        data = parse_client_info(self.PAGE, "~c").model_dump()

        assert data["avg_spend_per_hire"] == 515.15
        assert data["active_hire_share"] == 0.5

    def test_computed_fields_are_none_without_history(self):
        info = parse_client_info(NEW_CLIENT_PAGE, "~c")

        assert info.avg_spend_per_hire is None
        assert info.active_hire_share is None

    def test_no_division_by_zero_on_zero_hires(self):
        page = '<div><h5>About the client</h5><span>$500 total spent</span>' \
               '<div>0 hires, 0 active</div></div><div>Explore similar jobs</div>'
        info = parse_client_info(page, "~c")

        assert info.avg_spend_per_hire is None
        assert info.active_hire_share is None

    def test_hire_rate_stays_none_because_it_is_not_public(self):
        """Jobs-posted is absent from public pages, so hire rate cannot be derived."""
        info = parse_client_info(self.PAGE, "~c")

        assert info.hire_rate is None
        assert info.total_posted_jobs is None


def _payload_page(stats_idx=120, counts=(None, None), values=None):
    """A page carrying a Nuxt-style flat payload, as the real ones do.

    Real shape: the stats object holds indices into one flat array, and the
    values sit immediately after it.
    """
    import json as _json

    # totalCharges points at an {"amount": <index>} wrapper, which points at
    # the number one slot later — the same double hop the real payload uses.
    values = values or [378, 9, 70699.33, 218, 4.97, 149, {"amount": stats_idx + 8}, 325900.1]
    flat = list(range(stats_idx))          # filler so indices line up
    flat.append({
        "totalAssignments": stats_idx + 1,
        "activeAssignmentsCount": stats_idx + 2,
        "hoursCount": stats_idx + 3,
        "feedbackCount": stats_idx + 4,
        "score": stats_idx + 5,
        "totalJobsWithHires": stats_idx + 6,
        "totalCharges": stats_idx + 7,
    })
    flat.extend(values)
    flat.append({"postedCount": counts[0], "openCount": counts[1]})
    return f"<html><script>{_json.dumps(flat)}</script></html>"


class TestPayloadStats:
    """The page ships a state payload with more than the card renders."""

    def test_reads_exact_spend_not_the_rounded_card_value(self):
        from upwork_scraper.enrich.client_fetcher import parse_payload_stats

        stats = parse_payload_stats(_payload_page())

        assert stats["total_spent"] == 325900.1      # card shows "$326K"

    def test_reads_hires_and_hours(self):
        from upwork_scraper.enrich.client_fetcher import parse_payload_stats

        stats = parse_payload_stats(_payload_page())

        assert stats["total_hires"] == 378
        assert stats["active_hires"] == 9
        assert stats["total_hours"] == 70699.33

    def test_reads_rating_and_reviews_which_the_card_never_shows(self):
        from upwork_scraper.enrich.client_fetcher import parse_payload_stats

        stats = parse_payload_stats(_payload_page())

        assert stats["rating"] == 4.97
        assert stats["total_reviews"] == 218

    def test_reads_jobs_with_hires(self):
        from upwork_scraper.enrich.client_fetcher import parse_payload_stats

        assert parse_payload_stats(_payload_page())["total_jobs_with_hires"] == 149

    def test_posted_count_is_absent_for_visitors(self):
        """Upwork ships postedCount as null — this is why hire rate is impossible."""
        from upwork_scraper.enrich.client_fetcher import parse_payload_stats

        stats = parse_payload_stats(_payload_page())

        assert "total_posted_jobs" not in stats

    def test_posted_count_is_read_when_ever_populated(self):
        from upwork_scraper.enrich.client_fetcher import parse_payload_stats

        stats = parse_payload_stats(_payload_page(counts=(200, 5)))

        assert stats["total_posted_jobs"] == 200
        assert stats["open_jobs"] == 5

    def test_no_payload_yields_nothing_not_an_error(self):
        from upwork_scraper.enrich.client_fetcher import parse_payload_stats

        assert parse_payload_stats("<html>nothing</html>") == {}

    def test_indices_resolve_exactly_one_level(self):
        """Resolved values are small ints; recursing reads them as indices."""
        from upwork_scraper.enrich.client_fetcher import parse_payload_stats

        stats = parse_payload_stats(_payload_page())

        assert stats["active_hires"] == 9        # not flat[9]

    def test_payload_wins_over_the_rendered_card(self):
        page = ESTABLISHED_CLIENT_PAGE + _payload_page()
        info = parse_client_info(page, "~c")

        assert info.total_spent == 325900.1      # payload, not the card's $3.8K
        assert info.total_hires == 378
        assert info.rating == 4.97

    def test_card_still_used_when_no_payload(self):
        info = parse_client_info(ESTABLISHED_CLIENT_PAGE, "~c")

        assert info.total_spent == 3800.0
        assert info.total_hires == 80

    def test_hire_rate_stays_none_even_with_the_payload(self):
        info = parse_client_info(ESTABLISHED_CLIENT_PAGE + _payload_page(), "~c")

        assert info.hire_rate is None
        assert info.total_posted_jobs is None
        assert info.total_jobs_with_hires == 149


class TestHireRateShapedMetrics:
    """What public data can and cannot say about a client's hiring behaviour."""

    def _info(self, **kw):
        from upwork_scraper.models.client_models import ClientInfo

        return ClientInfo(cipher="~c", **kw)

    def test_hires_per_job_when_staffing_several_per_post(self):
        info = self._info(total_hires=378, total_jobs_with_hires=149)

        assert info.hires_per_job == 2.54

    def test_hires_per_job_of_one_means_one_hire_per_post(self):
        info = self._info(total_hires=35, total_jobs_with_hires=35)

        assert info.hires_per_job == 1.0

    def test_hires_per_job_is_none_without_both_numbers(self):
        assert self._info(total_hires=10).hires_per_job is None
        assert self._info(total_jobs_with_hires=10).hires_per_job is None

    def test_has_ever_hired_true(self):
        assert self._info(total_jobs_with_hires=3).has_ever_hired is True

    def test_has_ever_hired_false_when_every_post_went_unfilled(self):
        assert self._info(total_jobs_with_hires=0).has_ever_hired is False

    def test_has_ever_hired_none_when_unknown(self):
        """Never fetched is not the same as never hired."""
        assert self._info().has_ever_hired is None

    def test_hire_rate_itself_stays_unavailable(self):
        """jobs_with_hires is the numerator; the denominator is withheld."""
        info = self._info(total_hires=378, total_jobs_with_hires=149)

        assert info.hire_rate is None
        assert info.total_posted_jobs is None

    def test_computed_metrics_are_serialised(self):
        data = self._info(
            total_hires=378, total_jobs_with_hires=149, total_spent=325900.1
        ).model_dump()

        assert data["hires_per_job"] == 2.54
        assert data["has_ever_hired"] is True
        assert data["avg_spend_per_hire"] == 862.17


class TestFailureModesAreDistinguished:
    """Cloudflare, Upwork's soft block, and a dead render are different things."""

    def _enricher(self, fetcher):
        return ClientEnricher(
            html_fetcher=fetcher,
            delay=HumanDelay(sleeper=lambda s: None),
            breaker=CircuitBreaker(trip_after=5, sleeper=lambda s: None),
        )

    def test_rate_limit_is_not_reported_as_cloudflare(self):
        info = self._enricher(lambda url: (429, "")).enrich(_job())

        assert info.fetch_status == "rate_limited"
        assert "rate limited" in info.fetch_error

    def test_rate_limit_trips_the_breaker_at_once(self):
        """Continuing through a soft block extends the cool-off."""
        enricher = self._enricher(lambda url: (429, ""))

        results = enricher.enrich_all([_job(f"~{i}") for i in range(10)])

        assert len(results) == 1
        assert enricher.breaker.is_open is True

    def test_cloudflare_challenge_still_reported_as_blocked(self):
        info = self._enricher(lambda url: (403, "")).enrich(_job())

        assert info.fetch_status == "blocked"
        assert "Cloudflare" in info.fetch_error

    def test_unrendered_page_is_its_own_message(self):
        info = self._enricher(lambda url: (503, "")).enrich(_job())

        assert info.fetch_status == "failed"
        assert "never rendered" in info.fetch_error

    def test_soft_block_markers_recognised(self):
        from upwork_scraper.enrich.browser_fetcher import RATE_LIMIT_MARKERS

        page = "<html><body>Upwork We'll be right back</body></html>".lower()
        assert any(m in page for m in RATE_LIMIT_MARKERS)


