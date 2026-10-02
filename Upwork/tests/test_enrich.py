import random
from unittest.mock import MagicMock, patch

import pytest

from upwork_scraper.enrich import ClientEnricher, CircuitBreaker, HumanDelay
from upwork_scraper.enrich.client_fetcher import parse_client_info
from upwork_scraper.models.job_models import Job


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


class FakeSleep:
    """Records sleeps instead of performing them."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds):
        self.calls.append(seconds)

    @property
    def total(self):
        return sum(self.calls)


class TestHumanDelay:

    def test_first_request_is_immediate(self):
        sleep = FakeSleep()
        HumanDelay(sleeper=sleep).wait()

        assert sleep.calls == []

    def test_delays_fall_in_the_configured_range(self):
        sleep = FakeSleep()
        delay = HumanDelay(2, 5, long_pause_every=0, sleeper=sleep)

        for _ in range(30):
            delay.wait()

        assert len(sleep.calls) == 29
        assert all(2 <= s <= 5 for s in sleep.calls)

    def test_delays_are_not_constant(self):
        """A fixed interval is the easiest bot signature to spot."""
        sleep = FakeSleep()
        delay = HumanDelay(2, 9, long_pause_every=0, sleeper=sleep)

        for _ in range(25):
            delay.wait()

        assert len(set(sleep.calls)) > 15

    def test_takes_a_long_pause_periodically(self):
        sleep = FakeSleep()
        delay = HumanDelay(
            1, 2, long_pause_every=5, long_pause_seconds=(30, 40), sleeper=sleep
        )

        for _ in range(10):
            delay.wait()

        long_pauses = [s for s in sleep.calls if s >= 30]
        assert len(long_pauses) == 2

    def test_rejects_a_bad_range(self):
        with pytest.raises(ValueError):
            HumanDelay(10, 2)

    def test_is_reproducible_with_a_seeded_rng(self):
        def run():
            sleep = FakeSleep()
            delay = HumanDelay(1, 9, sleeper=sleep, rng=random.Random(42))
            for _ in range(5):
                delay.wait()
            return sleep.calls

        assert run() == run()


class TestCircuitBreaker:

    def test_starts_closed(self):
        assert CircuitBreaker().is_open is False

    def test_opens_after_consecutive_failures(self):
        sleep = FakeSleep()
        breaker = CircuitBreaker(trip_after=3, sleeper=sleep)

        for _ in range(3):
            breaker.record_failure()

        assert breaker.is_open is True

    def test_success_resets_the_streak(self):
        sleep = FakeSleep()
        breaker = CircuitBreaker(trip_after=3, sleeper=sleep)

        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()

        assert breaker.is_open is False
        assert breaker.consecutive_failures == 1

    def test_backoff_grows_with_each_failure(self):
        sleep = FakeSleep()
        breaker = CircuitBreaker(trip_after=5, base_backoff=10, sleeper=sleep)

        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()

        assert sleep.calls == [10, 20, 40]

    def test_backoff_is_capped(self):
        sleep = FakeSleep()
        breaker = CircuitBreaker(
            trip_after=99, base_backoff=10, max_backoff=45, sleeper=sleep
        )

        for _ in range(8):
            breaker.record_failure()

        assert max(sleep.calls) == 45

    def test_no_backoff_once_tripped(self):
        """Once open the caller stops, so there is nothing to wait for."""
        sleep = FakeSleep()
        breaker = CircuitBreaker(trip_after=2, base_backoff=10, sleeper=sleep)

        breaker.record_failure()
        breaker.record_failure()

        assert len(sleep.calls) == 1


class TestSoftBlockDetection:
    """Upwork's error page uses a typographic apostrophe."""

    def test_all_apostrophe_forms_detected(self):
        from upwork_scraper.enrich.browser_fetcher import (
            RATE_LIMIT_MARKERS,
            _normalise,
        )

        for page in (
            "<p>We\u2019ll be right back</p>",   # curly — what Upwork serves
            "<p>We'll be right back</p>",        # straight
            "<p>We&#39;ll be right back</p>",    # entity
            "<p>We&rsquo;ll be right back</p>",  # named entity
            "<p>We will be right back</p>",      # expanded
        ):
            assert any(m in _normalise(page) for m in RATE_LIMIT_MARKERS), page

    def test_ordinary_page_is_not_flagged(self):
        from upwork_scraper.enrich.browser_fetcher import (
            RATE_LIMIT_MARKERS,
            _normalise,
        )

        page = "<p>Video Editor wanted. We'll review applications weekly.</p>"

        assert not any(m in _normalise(page) for m in RATE_LIMIT_MARKERS)


class TestRenderedFallback:
    """The signed-out selector must not be the only proof a page rendered."""

    def test_signed_out_markup_counts(self):
        from upwork_scraper.enrich.browser_fetcher import RENDERED_MARKERS

        assert any(m in '<li data-qa="client-location">x</li>' for m in RENDERED_MARKERS)

    def test_content_without_the_data_qa_hooks_counts(self):
        from upwork_scraper.enrich.browser_fetcher import RENDERED_MARKERS

        assert any(m in "<h5>About the client</h5>" for m in RENDERED_MARKERS)
        assert any(m in '{"totalAssignments":378}' for m in RENDERED_MARKERS)

    def test_empty_shell_does_not_count(self):
        from upwork_scraper.enrich.browser_fetcher import RENDERED_MARKERS

        assert not any(m in "<html><body></body></html>" for m in RENDERED_MARKERS)


class TestProfileLockMessage:

    def test_profile_clash_explains_itself(self):
        from upwork_scraper.enrich import BrowserFetcher

        fetcher = BrowserFetcher()

        def boom():
            raise RuntimeError("Opening in existing browser session.")

        with pytest.raises(RuntimeError, match="already open"):
            fetcher._launch_context(boom)

    def test_other_launch_errors_pass_through(self):
        from upwork_scraper.enrich import BrowserFetcher

        fetcher = BrowserFetcher()

        def boom():
            raise RuntimeError("something else entirely")

        with pytest.raises(RuntimeError, match="something else"):
            fetcher._launch_context(boom)


class TestSoftBlockScanning:
    """The block text sits ~1.1MB into the page, not in the head."""

    def _fetcher(self, html, rendered):
        from upwork_scraper.enrich import BrowserFetcher

        fetcher = BrowserFetcher()
        fetcher._context = MagicMock()
        fetcher.retry_on_challenge = False
        fetcher._load_once = lambda url, sel=None: (200, html, rendered)
        return fetcher

    def test_block_text_deep_in_the_page_is_found(self):
        html = "<html>" + ("x" * 800_000) + "We\u2019ll be right back" + "</html>"

        status, _ = self._fetcher(html, rendered=False).fetch("https://u/~a")

        assert status == 429

    def test_healthy_page_shipping_the_string_is_not_flagged(self):
        """A rendered page containing the error template must stay a success."""
        html = (
            '<li data-qa="client-location">x</li>'
            + ("x" * 500_000)
            + "errorTemplate: \u2018We\u2019ll be right back\u2019"
        )

        status, _ = self._fetcher(html, rendered=True).fetch("https://u/~a")

        assert status == 200

    def test_unrendered_without_the_marker_is_503_not_429(self):
        status, _ = self._fetcher("<html></html>", rendered=False).fetch("https://u/~a")

        assert status == 503


class TestWindowPlacement:
    """A profile parked off-screen must not strand the visible login window."""

    def _args(self, **kw):
        from upwork_scraper.enrich import BrowserFetcher
        from upwork_scraper.enrich.browser_fetcher import (
            OFFSCREEN_ARGS,
            ONSCREEN_ARGS,
        )

        f = BrowserFetcher(**kw)
        if f.headless:
            return []
        return OFFSCREEN_ARGS if f.offscreen else ONSCREEN_ARGS

    def test_default_runs_are_parked_off_screen(self):
        from upwork_scraper.enrich.browser_fetcher import OFFSCREEN_ARGS

        assert self._args() == OFFSCREEN_ARGS

    def test_visible_runs_state_a_position_explicitly(self):
        """Without this, Chrome restores the profile's saved off-screen bounds."""
        args = self._args(offscreen=False)

        assert any("--window-position=80,60" in a for a in args)
        assert any("--window-size=" in a for a in args)
        assert not any("-2400" in a for a in args)

    def test_headless_takes_no_window_args(self):
        assert self._args(headless=True) == []


class TestSessionPersistence:
    """A session-only cookie dies with the browser and looks like an expiry."""

    def _fetcher(self, cookies):
        from upwork_scraper.enrich import BrowserFetcher

        f = BrowserFetcher()
        f._context = MagicMock()
        f._context.cookies.return_value = cookies
        return f

    def test_persistent_cookie_is_recognised(self):
        f = self._fetcher([{"name": "master_access_token", "expires": 1893456000}])

        assert f.session_cookies_persist() is True

    def test_session_only_cookie_is_flagged(self):
        """Playwright reports -1 for a cookie with no expiry."""
        f = self._fetcher([{"name": "master_access_token", "expires": -1}])

        assert f.session_cookies_persist() is False
        assert f.has_session_cookies() is True     # present, but temporary

    def test_missing_expiry_counts_as_session_only(self):
        f = self._fetcher([{"name": "user_uid"}])

        assert f.session_cookies_persist() is False

    def test_no_session_cookies_at_all(self):
        f = self._fetcher([{"name": "visitor_id", "expires": 1893456000}])

        assert f.session_cookies_persist() is False
        assert f.has_session_cookies() is False

    def test_one_persistent_among_several_is_enough(self):
        f = self._fetcher([
            {"name": "user_uid", "expires": -1},
            {"name": "master_access_token", "expires": 1893456000},
        ])

        assert f.session_cookies_persist() is True


class TestSessionRescue:
    """Google sign-in gives no 'keep me logged in', so save the cookies."""

    def _fetcher(self, tmp_path, cookies=None):
        from upwork_scraper.enrich import BrowserFetcher

        f = BrowserFetcher(profile_dir=tmp_path)
        f._context = MagicMock()
        f._context.cookies.return_value = cookies or []
        return f

    def test_saves_upwork_cookies_with_an_expiry(self, tmp_path):
        import json as _json

        f = self._fetcher(tmp_path, [
            {"name": "master_access_token", "domain": ".upwork.com", "expires": -1},
            {"name": "user_uid", "domain": "www.upwork.com", "expires": -1},
        ])

        assert f.save_session() == 2

        saved = _json.loads(f.session_path.read_text(encoding="utf-8"))
        assert all(c["expires"] > 0 for c in saved)      # no longer session-only

    def test_keeps_an_existing_future_expiry(self, tmp_path):
        import json as _json

        f = self._fetcher(tmp_path, [
            {"name": "master_access_token", "domain": ".upwork.com", "expires": 1893456000},
        ])
        f.save_session()

        saved = _json.loads(f.session_path.read_text(encoding="utf-8"))
        assert saved[0]["expires"] == 1893456000

    def test_ignores_cookies_from_other_sites(self, tmp_path):
        f = self._fetcher(tmp_path, [
            {"name": "x", "domain": ".google.com", "expires": -1},
        ])

        assert f.save_session() == 0
        assert not f.session_path.exists()

    def test_restore_adds_them_back(self, tmp_path):
        f = self._fetcher(tmp_path, [
            {"name": "master_access_token", "domain": ".upwork.com", "expires": -1},
        ])
        f.save_session()

        f2 = self._fetcher(tmp_path)
        assert f2.restore_session() is True
        assert f2._context.add_cookies.call_count == 1

    def test_restore_without_a_saved_file_is_harmless(self, tmp_path):
        assert self._fetcher(tmp_path).restore_session() is False

    def test_corrupt_file_does_not_raise(self, tmp_path):
        f = self._fetcher(tmp_path)
        f.profile_dir.mkdir(parents=True, exist_ok=True)
        f.session_path.write_text("not json", encoding="utf-8")

        assert f.restore_session() is False

    def test_forget_session_removes_it(self, tmp_path):
        f = self._fetcher(tmp_path, [
            {"name": "user_uid", "domain": ".upwork.com", "expires": -1},
        ])
        f.save_session()
        f.forget_session()

        assert not f.session_path.exists()
        f.forget_session()      # idempotent


class TestSessionAudit:
    """Defects found auditing the login flow."""

    # A session check only reaches the page when the profile holds session
    # cookies, so anything testing the page logic has to carry one.
    SIGNED_IN_COOKIES = [{"name": "master_access_token", "domain": ".upwork.com"}]

    def _fetcher(self, tmp_path, cookies=None):
        from upwork_scraper.enrich import BrowserFetcher

        f = BrowserFetcher(profile_dir=tmp_path)
        f._context = MagicMock()
        f._context.cookies.return_value = cookies or []
        return f

    def test_restore_skipped_when_every_cookie_is_already_present(self, tmp_path):
        """Nothing to add means nothing to do — and nothing gets clobbered."""
        import json as _json, time as _time

        live = [{"name": "master_access_token", "domain": ".upwork.com",
                 "expires": _time.time() + 86400}]
        f = self._fetcher(tmp_path, live)
        f.profile_dir.mkdir(parents=True, exist_ok=True)
        f.session_path.write_text(_json.dumps(live), encoding="utf-8")

        assert f.restore_session() is False
        assert f._context.add_cookies.call_count == 0

    def test_partial_survival_restores_only_the_missing_cookies(self, tmp_path):
        """Chrome kept one auth cookie and dropped two; skipping left it broken."""
        import json as _json, time as _time

        expiry = _time.time() + 86400
        saved = [
            {"name": "oauth2_global_js_token", "domain": ".upwork.com", "expires": expiry},
            {"name": "master_access_token", "domain": ".upwork.com", "expires": expiry},
            {"name": "user_uid", "domain": ".upwork.com", "expires": expiry},
        ]
        survived = [saved[0]]                      # only this one persisted
        f = self._fetcher(tmp_path, survived)
        f.profile_dir.mkdir(parents=True, exist_ok=True)
        f.session_path.write_text(_json.dumps(saved), encoding="utf-8")

        assert f.restore_session() is True

        added = {c["name"] for c in f._context.add_cookies.call_args.args[0]}
        assert added == {"master_access_token", "user_uid"}

    def test_a_surviving_cookie_is_never_overwritten(self, tmp_path):
        """The value in the live jar is the fresher one; keep it."""
        import json as _json, time as _time

        expiry = _time.time() + 86400
        f = self._fetcher(tmp_path, [
            {"name": "master_access_token", "domain": ".upwork.com",
             "value": "fresh", "expires": expiry},
        ])
        f.profile_dir.mkdir(parents=True, exist_ok=True)
        f.session_path.write_text(_json.dumps([
            {"name": "master_access_token", "domain": ".upwork.com",
             "value": "stale", "expires": expiry},
            {"name": "user_uid", "domain": ".upwork.com", "expires": expiry},
        ]), encoding="utf-8")

        f.restore_session()

        added = f._context.add_cookies.call_args.args[0]
        assert [c["name"] for c in added] == ["user_uid"]

    def test_expired_save_is_not_restored(self, tmp_path):
        import json as _json

        f = self._fetcher(tmp_path)
        f.profile_dir.mkdir(parents=True, exist_ok=True)
        f.session_path.write_text(
            _json.dumps([{"name": "user_uid", "domain": ".upwork.com", "expires": 100}]),
            encoding="utf-8",
        )

        assert f.restore_session() is False

    def test_malformed_save_is_not_restored(self, tmp_path):
        import json as _json

        f = self._fetcher(tmp_path)
        f.profile_dir.mkdir(parents=True, exist_ok=True)
        f.session_path.write_text(_json.dumps({"not": "a list"}), encoding="utf-8")

        assert f.restore_session() is False

    def test_session_state_tells_the_modes_apart(self, tmp_path):
        f = self._fetcher(tmp_path, self.SIGNED_IN_COOKIES)
        page = MagicMock()
        f._context.new_page.return_value = page

        page.url = "https://www.upwork.com/nx/search/jobs/"
        page.content.return_value = "<html>jobs</html>"
        assert f.session_state() == "live"

        page.url = "https://www.upwork.com/ab/account-security/login?redir=x"
        assert f.session_state() == "signed_out"

        page.url = "https://www.upwork.com/nx/search/jobs/"
        page.content.return_value = "<html>We\u2019ll be right back</html>"
        assert f.session_state() == "rate_limited"

    def test_a_timeout_is_unknown_not_signed_out(self, tmp_path):
        f = self._fetcher(tmp_path, self.SIGNED_IN_COOKIES)
        f._context.new_page.side_effect = RuntimeError("Timeout")

        assert f.session_state() == "unknown"
        assert f.session_is_live() is False

    def test_a_profile_without_session_cookies_is_signed_out(self, tmp_path):
        """The freshly-reset case: decided without loading anything.

        `--reset-login` immediately followed by `--logged-in` reported a live
        session and produced a file with an empty hire-rate column. Cookies are
        a necessary condition, so their absence settles it — no page load, and
        nothing for a slow first paint to get wrong.
        """
        f = self._fetcher(tmp_path, [])

        assert f.session_state() == "signed_out"
        assert f._context.new_page.call_count == 0

    def test_visitor_only_cookies_do_not_count(self, tmp_path):
        """Anonymous visitors get a visitor_id too — it proves nothing."""
        f = self._fetcher(tmp_path, [{"name": "visitor_id", "domain": ".upwork.com"}])

        assert f.session_state() == "signed_out"

    def test_a_page_that_never_rendered_is_unknown_not_live(self, tmp_path):
        """An empty shell carries no visitor nav either.

        Reading that absence as proof of a session is what let a signed-out run
        through, so an unrendered page is now an unknown.
        """
        f = self._fetcher(tmp_path, self.SIGNED_IN_COOKIES)
        page = MagicMock()
        page.url = "https://www.upwork.com/nx/search/jobs/"
        page.content.return_value = "<html><body></body></html>"
        page.wait_for_function.side_effect = RuntimeError("Timeout 10000ms exceeded")
        f._context.new_page.return_value = page

        assert f.session_state() == "unknown"

    def test_a_cloudflare_challenge_is_unknown(self, tmp_path):
        f = self._fetcher(tmp_path, self.SIGNED_IN_COOKIES)
        page = MagicMock()
        page.url = "https://www.upwork.com/nx/search/jobs/"
        page.content.return_value = "<html><title>Just a moment...</title></html>"
        f._context.new_page.return_value = page

        assert f.session_state() == "unknown"

class TestSignedInDetection:
    """The job search loads for anonymous visitors, so a clean load proves nothing."""

    def _fetcher(self, url, html):
        from upwork_scraper.enrich import BrowserFetcher

        f = BrowserFetcher()
        f._context = MagicMock()
        # Session cookies present throughout: what is under test here is what
        # the *page* says, and the check never gets that far without them.
        f._context.cookies.return_value = [
            {"name": "master_access_token", "domain": ".upwork.com"}
        ]
        page = MagicMock()
        page.url = url
        page.content.return_value = html
        f._context.new_page.return_value = page
        return f

    def test_visitor_nav_means_signed_out_even_on_a_clean_load(self):
        """This exact case reported 'live' and silently produced anonymous data."""
        f = self._fetcher(
            "https://www.upwork.com/nx/search/jobs/",
            '<html><nav><a data-qa="global-signup-desktop-login">Log in</a>'
            "</nav><div>jobs</div></html>",
        )

        assert f.session_state() == "signed_out"

    def test_mobile_variant_also_counts(self):
        f = self._fetcher(
            "https://www.upwork.com/nx/search/jobs/",
            '<html><a data-qa="global-signup-mobile-login">Log in</a></html>',
        )

        assert f.session_state() == "signed_out"

    def test_signed_in_page_has_no_visitor_nav(self):
        f = self._fetcher(
            "https://www.upwork.com/nx/search/jobs/",
            '<html><nav data-qa="user-menu">My Jobs</nav></html>',
        )

        assert f.session_state() == "live"

    def test_login_redirect_still_wins(self):
        f = self._fetcher(
            "https://www.upwork.com/ab/account-security/login?redir=x", "<html></html>"
        )

        assert f.session_state() == "signed_out"

    def test_rate_limit_outranks_everything(self):
        f = self._fetcher(
            "https://www.upwork.com/nx/search/jobs/",
            '<html><a data-qa="global-signup-desktop-login"></a>'
            "We\u2019ll be right back</html>",
        )

        assert f.session_state() == "rate_limited"


class TestVisitorNavMarker:
    """The search page tags its nav as the visitor one — that is the signal."""

    def _state(self, html):
        from upwork_scraper.enrich import BrowserFetcher

        f = BrowserFetcher()
        f._context = MagicMock()
        f._context.cookies.return_value = [
            {"name": "master_access_token", "domain": ".upwork.com"}
        ]
        page = MagicMock()
        page.url = "https://www.upwork.com/nx/search/jobs/"
        page.content.return_value = html
        f._context.new_page.return_value = page
        return f.session_state()

    def test_visitor_nav_is_signed_out(self):
        """Reported 'live' before this, and produced hire_rate: null silently."""
        assert self._state('<html><nav data-qa="top-nav-visitor-ia"></nav></html>') == "signed_out"

    def test_member_nav_is_live(self):
        assert self._state('<html><nav data-qa="top-nav-user-ia"></nav></html>') == "live"
