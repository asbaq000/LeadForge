"""Browser transport for the enrichment stage.

Job pages are public but Cloudflare refuses plain HTTP clients with a 403 at
every TLS impersonation setting, so this drives a real browser.

Every choice here was measured against live job pages on 2026-08-12, not
assumed. What was tried and what happened:

    plain HTTP (curl_cffi, 7 impersonations)   403, always
    Playwright bundled chromium, headless      403 challenge
    Playwright bundled chromium, headful       403 challenge
    Playwright system chrome, headless         403 challenge
    Playwright system chrome, headful          403 challenge
    Playwright + persistent profile            403 challenge
    patchright system chrome, headless         403 challenge
    patchright system chrome, HEADFUL          works

Cloudflare fingerprints the CDP automation traces that stock Playwright leaks;
patchright patches them out. Headless is detected on its own, independently of
CDP, so the browser must run headful — `offscreen` parks the window far off the
desktop instead, which keeps it out of the way without pretending to be
headless.

Three things that are counter-intuitive and easy to "fix" back into breakage:

1. No custom user agent, no `--disable-blink-features` style args, no stealth
   init scripts. Those *help* stock Playwright and *hurt* patchright, whose
   whole approach is to look like an ordinary Chrome with nothing overridden.
2. One IP for the whole run. A Cloudflare clearance cookie is bound to the IP
   that earned it, so rotating proxies per request throws away clearance and
   triggers a fresh challenge every time. This is the opposite of the right
   policy for the search API.
3. Success is judged by the client card being in the DOM, not by HTTP status —
   a page was observed returning 403 while still rendering the real content.
"""

import json
import logging
import random
import time
from pathlib import Path

from upwork_scraper.proxies.proxy_manager import ProxyManager

LOGGER = logging.getLogger(__name__)

INSTALL_HELP = """
The browser transport needs patchright (a patched Playwright that Cloudflare
does not flag) plus Chrome. From this folder:

    .venv\\Scripts\\python.exe -m pip install -e ".[browser]"
    .venv\\Scripts\\python.exe -m patchright install chrome

Stock playwright is detected on Upwork job pages and will be blocked.
""".strip()

# Two separate Chrome profiles, deliberately isolated:
#   anonymous  - signed out, what every run uses by default
#   logged-in  - holds an Upwork session, used only when explicitly asked for
# Keeping them apart means signing in cannot silently change what an existing
# run does, and the scheduled job stays anonymous unless told otherwise.
DEFAULT_PROFILE_DIR = Path.home() / ".upwork_scraper" / "browser_profile"
LOGIN_PROFILE_DIR = Path.home() / ".upwork_scraper" / "browser_profile_login"


def profile_for(logged_in: bool) -> Path:
    return LOGIN_PROFILE_DIR if logged_in else DEFAULT_PROFILE_DIR

# Present on a job page that actually rendered — the real success signal.
READY_SELECTOR = '[data-qa="client-location"], [data-qa="client-contract-date"]'

CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-challenge",
    "cf_chl_opt",
    "enable javascript and cookies to continue",
)

# Upwork's own soft block, served as a friendly error page with HTTP 200. Seen
# on a signed-in session after roughly a dozen job-page loads: the app shell
# renders but never hydrates, and the body reads "We'll be right back".
# Distinct from a Cloudflare challenge, and it means back off — hard.
RATE_LIMIT_MARKERS = (
    "we'll be right back",
    "we will be right back",
)

# Served only to visitors. Their presence is the reliable "signed out" signal —
# the job search itself loads for anyone, so a successful load proves nothing.
SIGNED_OUT_MARKERS = (
    # The search page labels its whole nav as the visitor one. This is the
    # marker that matters, because the search page is what gets checked.
    'data-qa="top-nav-visitor-ia"',
    # Job pages use these instead.
    'data-qa="global-signup-desktop-login"',
    'data-qa="global-signup-mobile-login"',
    "Log in to Upwork",
)

# Markup that proves a job page actually rendered, whichever variant was
# served. The signed-out page uses data-qa hooks; the signed-in one renders
# differently, so content markers are checked too rather than trusting one
# selector to cover both.
RENDERED_MARKERS = (
    'data-qa="client-location"',
    'data-qa="client-contract-date"',
    "About the client",
    "totalAssignments",
)


def _normalise(text: str) -> str:
    """Lowercase with curly quotes folded to straight ones.

    Upwork's error page uses a typographic apostrophe, so matching on a plain
    "we'll" silently missed it and the block looked like a render failure.
    """
    return (
        text.lower()
        .replace("’", "'")
        .replace("‘", "'")
        .replace("&#39;", "'")
        .replace("&rsquo;", "'")
    )

# Parks the window ~2400px off-screen. Headful for detection purposes, invisible
# in practice. The only launch arg used, because args are a detection signal.
OFFSCREEN_ARGS = ["--window-position=-2400,-2400"]

# Chrome stores window bounds in the profile, so a profile that has been parked
# off-screen reopens off-screen even without the arg — which strands the login
# window where it cannot be seen or dragged back. Visible runs therefore state
# the position explicitly rather than letting the profile decide.
ONSCREEN_ARGS = ["--window-position=80,60", "--window-size=1280,900"]


def _load_playwright():
    """patchright if present, stock playwright as a (blocked) fallback."""
    try:
        from patchright.sync_api import sync_playwright
        return sync_playwright, True
    except ImportError:
        pass
    try:
        from playwright.sync_api import sync_playwright
        LOGGER.warning(
            "Using stock playwright — Cloudflare detects it on job pages. "
            "Install patchright for this to work."
        )
        return sync_playwright, False
    except ImportError as e:
        raise RuntimeError(INSTALL_HELP) from e


class BrowserFetcher:
    """Fetches job pages with a real browser. Use as a context manager.

        with BrowserFetcher() as fetch:
            enricher = ClientEnricher(html_fetcher=fetch)

    The instance is callable, so it plugs straight into `ClientEnricher`.
    """

    def __init__(
        self,
        proxy_manager: ProxyManager | None = None,
        headless: bool = False,
        offscreen: bool = True,
        profile_dir: str | Path | None = None,
        timeout_ms: int = 45_000,
        ready_timeout_ms: int = 15_000,
        retry_on_challenge: bool = True,
        rng: random.Random | None = None,
    ):
        if headless:
            LOGGER.warning(
                "headless=True is detected by Cloudflare on job pages — "
                "expect every fetch to be blocked."
            )

        self.proxy_manager = proxy_manager
        self.headless = headless
        self.offscreen = offscreen
        self.profile_dir = Path(profile_dir or DEFAULT_PROFILE_DIR)
        self.timeout_ms = timeout_ms
        self.ready_timeout_ms = ready_timeout_ms
        self.retry_on_challenge = retry_on_challenge
        self._rng = rng or random.Random()

        self._playwright = None
        self._context = None
        self.is_patched = False
        self.pages_fetched = 0

    # ------------------------------------------------------------------ setup
    def _proxy_settings(self) -> dict | None:
        """One proxy for the whole run — clearance cookies are IP-bound."""
        if not self.proxy_manager:
            return None
        proxy = self.proxy_manager.get_proxy()
        if not proxy:
            return None
        LOGGER.info("Browser pinned to proxy %s", proxy.host)
        return {
            "server": f"http://{proxy.host}:{proxy.port}",
            "username": proxy.username,
            "password": proxy.password,
        }

    def start(self):
        sync_playwright, self.is_patched = _load_playwright()
        self._playwright = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        # A persistent profile keeps the Cloudflare clearance cookie between
        # runs, which is the single biggest reduction in challenges.
        self._context = self._launch_context(
            lambda: self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="chrome",
                headless=self.headless,
                no_viewport=True,
                args=(
                    OFFSCREEN_ARGS
                    if (self.offscreen and not self.headless)
                    else ([] if self.headless else ONSCREEN_ARGS)
                ),
                proxy=self._proxy_settings(),
            )
        )
        # Only the signed-in profile restores a session. Doing it for every
        # profile would let a stray session.json turn anonymous runs into
        # signed-in ones, which is exactly the isolation the two profiles buy.
        if self.profile_dir == LOGIN_PROFILE_DIR:
            self.restore_session()

        LOGGER.info(
            "Browser ready (patchright=%s, headless=%s, profile=%s)",
            self.is_patched, self.headless, self.profile_dir,
        )
        return self

    def _launch_context(self, launch):
        """Launch, turning a profile clash into an explanation."""
        try:
            return launch()
        except Exception as e:
            if "already in use" in str(e) or "existing browser session" in str(e):
                raise RuntimeError(
                    f"The browser profile is already open:\n  {self.profile_dir}\n"
                    "Another run (or a --login window) still has it. Chrome allows "
                    "one process per profile — wait for that run to finish, or "
                    "close its window, then try again."
                ) from e
            raise

    def close(self):
        for closer, method in ((self._context, "close"), (self._playwright, "stop")):
            try:
                if closer is not None:
                    getattr(closer, method)()
            except Exception:
                pass
        self._context = self._playwright = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ fetch
    # Cookies Upwork sets for a signed-in member. `visitor_id` is deliberately
    # absent — anonymous visitors get that one too.
    SESSION_COOKIES = frozenset({
        "master_access_token", "oauth2_global_js_token", "user_uid", "company_uid",
    })

    LOGIN_REDIRECT = "/ab/account-security/login"

    # Proof that the page painted, without needing to know what signed-in
    # markup looks like: Upwork hooks its components with data-qa/data-test
    # attributes everywhere. A healthy search page carries ~950 of them; a
    # blank shell carries none.
    HYDRATED = "() => document.querySelectorAll('[data-qa],[data-test]').length >= 5"

    def session_state(self) -> str:
        """`live`, `signed_out`, `rate_limited` or `unknown`.

        Cookies outlive the session they belong to — a profile kept its auth
        cookies while Upwork redirected every request to the login page — so a
        cookie check alone cannot promote a profile to `live`; that part asks
        the server. Their *absence*, though, is decisive on its own, and free.

        Four outcomes rather than a boolean, because a network blip is not the
        same as being signed out, and silently downgrading a signed-in run on
        a timeout would hide the real problem.

        `live` is never the fallback. It is returned only on positive evidence:
        session cookies present, the page actually rendered, and no visitor
        markup on it. An earlier version returned `live` whenever it failed to
        recognise the page, so a slow first paint on a fresh profile — which
        carries no visitor nav *because it carries nothing yet* — read as a
        signed-in session, and the run went on to produce empty hire rates.
        """
        if self._context is None:
            return "unknown"

        # A profile holding none of Upwork's session cookies cannot be signed
        # in, whatever a page render suggests. This is the freshly-reset case,
        # answered in microseconds and immune to page timing.
        if not self.has_session_cookies():
            return "signed_out"

        page = None
        try:
            # Inside the try: opening the page can fail too, and that is an
            # unknown, not a verdict of signed-out.
            page = self._context.new_page()
            page.goto(
                "https://www.upwork.com/nx/search/jobs/",
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )

            # Wait for the render rather than sleeping a fixed 1.5s and hoping.
            # Warm, the markers are there at domcontentloaded; cold, they are
            # not, and that difference used to decide the verdict.
            hydrated = True
            try:
                page.wait_for_function(
                    self.HYDRATED, timeout=min(self.ready_timeout_ms, 10_000)
                )
            except Exception:
                hydrated = False

            html = page.content()
            low = _normalise(html)

            if any(m in low for m in RATE_LIMIT_MARKERS):
                return "rate_limited"
            if self.LOGIN_REDIRECT in page.url:
                return "signed_out"

            # The job search renders for anonymous visitors too, so "it loaded
            # without redirecting" proves nothing. The sign-up nav is what
            # actually distinguishes the two: it is only served to visitors.
            if any(m in html for m in SIGNED_OUT_MARKERS):
                return "signed_out"
            if any(m in low[:6000] for m in CHALLENGE_MARKERS):
                return "unknown"

            return "live" if hydrated else "unknown"
        except Exception as e:
            LOGGER.warning("Session check failed: %s", type(e).__name__)
            return "unknown"
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def session_is_live(self) -> bool:
        """Convenience wrapper — see `session_state` for the distinctions."""
        return self.session_state() == "live"

    def has_session_cookies(self) -> bool:
        """True when the profile holds an Upwork session.

        Read from the cookie jar rather than by loading a page, so it can be
        polled while the user is part-way through signing in without
        disturbing them.
        """
        if self._context is None:
            return False
        try:
            names = {c.get("name") for c in self._context.cookies()}
        except Exception:
            return False
        return bool(names & self.SESSION_COOKIES)

    # Where a rescued session is kept. Inside the profile directory, so it
    # lives and dies with the profile it belongs to.
    SESSION_FILE = "session.json"
    SESSION_TTL_DAYS = 30

    @property
    def session_path(self) -> Path:
        return self.profile_dir / self.SESSION_FILE

    def save_session(self) -> int:
        """Persist Upwork cookies so they outlive the browser process.

        Signing in with Google (or without ticking "keep me logged in") yields
        session-scoped cookies that Chrome drops on exit, even though the token
        itself is still valid server-side. Saving them with an explicit expiry
        turns a one-shot login into one that lasts.

        The file holds live session tokens — treat it like a password. It sits
        in the profile directory and is git-ignored.
        """
        if self._context is None:
            return 0

        cookies = [
            c for c in self._context.cookies()
            if "upwork.com" in (c.get("domain") or "")
        ]
        if not cookies:
            return 0

        expiry = time.time() + self.SESSION_TTL_DAYS * 86400
        for cookie in cookies:
            if (cookie.get("expires") or -1) <= 0:
                cookie["expires"] = expiry

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(json.dumps(cookies), encoding="utf-8")
        try:  # best effort; POSIX only
            self.session_path.chmod(0o600)
        except Exception:
            pass

        LOGGER.info("Saved %d session cookies to %s", len(cookies), self.session_path)
        return len(cookies)

    def restore_session(self) -> bool:
        """Re-add saved cookies to a fresh context, if it needs them.

        Skipped when the profile already carries a session: Upwork rotates
        tokens as you use it, so replaying an older file over a live jar would
        downgrade a working session to a stale one.
        """
        if self._context is None or not self.session_path.exists():
            return False

        try:
            cookies = json.loads(self.session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            LOGGER.warning("Saved session is unreadable (%s)", type(e).__name__)
            return False

        if not isinstance(cookies, list) or not cookies:
            LOGGER.warning("Saved session is empty or malformed")
            return False

        # A file older than the expiry we wrote is certainly dead.
        fresh = [c for c in cookies if (c.get("expires") or 0) > time.time()]
        if not fresh:
            LOGGER.warning("Saved session has expired — sign in again with --login")
            return False

        # Fill in what the profile is missing rather than replacing wholesale.
        # Chrome persists some auth cookies and drops others, so an
        # all-or-nothing rule fails both ways: replacing everything can
        # clobber fresher values, and skipping when *any* cookie survives
        # leaves the session broken — which is exactly what happened when
        # only oauth2_global_js_token came back and the run reported
        # signed_out with a perfectly good session file on disk.
        try:
            present = {
                (c.get("name"), c.get("domain")) for c in self._context.cookies()
            }
        except Exception:
            present = set()

        missing = [c for c in fresh if (c.get("name"), c.get("domain")) not in present]
        if not missing:
            LOGGER.debug("Profile already holds every saved cookie")
            return False

        try:
            self._context.add_cookies(missing)
        except Exception as e:
            LOGGER.warning("Could not restore the saved session: %s", type(e).__name__)
            return False

        LOGGER.info(
            "Restored %d of %d saved cookies (%d already present)",
            len(missing), len(fresh), len(fresh) - len(missing),
        )
        return True

    def forget_session(self):
        """Delete the saved session — the way to sign this profile out."""
        try:
            self.session_path.unlink()
        except FileNotFoundError:
            pass

    def session_cookies_persist(self) -> bool:
        """Whether the session survives closing the browser.

        Upwork issues session-scoped cookies unless "Keep me logged in on this
        device" is ticked. Those vanish when Chrome exits, so the next run
        starts signed out — which looks exactly like an expired session and
        sent this down the wrong path once already.
        """
        if self._context is None:
            return False
        try:
            cookies = self._context.cookies()
        except Exception:
            return False

        session_cookies = [
            c for c in cookies if c.get("name") in self.SESSION_COOKIES
        ]
        if not session_cookies:
            return False
        # A cookie with no (or a past) expiry dies with the browser. Playwright
        # reports -1 for session cookies.
        return any((c.get("expires") or -1) > 0 for c in session_cookies)

    def wait_for_login(self, timeout_s: int = 300, poll_s: float = 3.0) -> bool:
        """Block until the sign-in completes, or the timeout expires.

        Cookies appearing is necessary but not sufficient — some arrive
        part-way through an SSO redirect, before the account is actually
        signed in — so the cookie poll only decides when it is worth asking
        Upwork for a verdict.
        """
        waited = 0.0
        while waited < timeout_s:
            if self.has_session_cookies() and self.session_state() == "live":
                return True
            time.sleep(poll_s)
            waited += poll_s
        return self.has_session_cookies() and self.session_state() == "live"

    # The signed-in app serves job details on this route, and hydrates them
    # into window.__NUXT__ — where the numbers are richer than the card.
    DETAILS_URL = "https://www.upwork.com/nx/search/jobs/details/{cipher}"

    def fetch_client_state(self, cipher: str, settle_ms: int = 4000) -> dict | None:
        """Client details from the signed-in app's own state object.

        Returns None when the state is absent — signed out, blocked, or the
        route changed — so the caller can fall back to scraping the HTML.
        """
        from upwork_scraper.enrich.nuxt_state import STATE_SCRIPT

        if self._context is None:
            raise RuntimeError("BrowserFetcher not started")

        page = self._context.new_page()
        try:
            page.goto(
                self.DETAILS_URL.format(cipher=cipher),
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
            # The state is populated during hydration, so give it a moment —
            # but read the object rather than waiting for the card to paint.
            try:
                page.wait_for_function(
                    "() => window.__NUXT__ && window.__NUXT__.state "
                    "&& window.__NUXT__.state.jobDetails",
                    timeout=self.ready_timeout_ms,
                )
            except Exception:
                page.wait_for_timeout(settle_ms)

            state = page.evaluate(STATE_SCRIPT)
            self.pages_fetched += 1
            return state
        except Exception as e:
            LOGGER.warning("State read failed for %s: %s", cipher, type(e).__name__)
            return None
        finally:
            try:
                page.close()
            except Exception:
                pass

    # Old name kept so existing callers keep working.
    is_signed_in = has_session_cookies

    def open_page(self, url: str):
        """A page left open for the caller to drive — used by the login flow."""
        if self._context is None:
            raise RuntimeError("BrowserFetcher not started")
        page = self._context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        return page

    def _load_once(self, url: str, ready_selector: str | None = None) -> tuple[int, str, bool]:
        page = self._context.new_page()
        try:
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            status = response.status if response else 0

            # Wait for the client card rather than a blanket sleep. Its presence
            # is what "success" means; the status code can lie. Pages that are
            # not job pages pass `ready_selector=""` to skip the check.
            selector = READY_SELECTOR if ready_selector is None else ready_selector
            rendered = True
            if selector:
                try:
                    page.wait_for_selector(selector, timeout=self.ready_timeout_ms)
                except Exception:
                    # The selector targets the signed-out markup. A signed-in
                    # page renders differently, so fall back to asking whether
                    # the content is there at all.
                    rendered = any(m in page.content() for m in RENDERED_MARKERS)

            # A brief, irregular read pause and a scroll — closer to real use
            # than loading and closing instantly.
            page.wait_for_timeout(self._rng.randint(400, 1400))
            try:
                page.mouse.wheel(0, self._rng.randint(300, 1100))
                page.wait_for_timeout(self._rng.randint(200, 600))
            except Exception:
                pass

            return status, page.content(), rendered
        finally:
            try:
                page.close()
            except Exception:
                pass

    def fetch(self, url: str, ready_selector: str | None = None) -> tuple[int, str]:
        """Load a job page and return (status, html).

        Reports 200 whenever the page actually rendered, even if the navigation
        status was not 200 — an observed page returned 403 with the real content
        present. Otherwise the three failure modes are told apart:

            403  Cloudflare challenge
            429  Upwork's own soft block ("We'll be right back")
            503  loaded but never rendered the client card
        """
        if self._context is None:
            raise RuntimeError("BrowserFetcher not started — use it as a context manager")

        attempts = 2 if self.retry_on_challenge else 1
        status, html, rendered = 0, "", False
        challenged = rate_limited = False

        for attempt in range(1, attempts + 1):
            status, html, rendered = self._load_once(url, ready_selector)
            head = _normalise(html[:6000])
            challenged = any(m in head for m in CHALLENGE_MARKERS)

            # The soft-block text sits deep in the document — observed at byte
            # 1,159,368 of a 1.2MB page — so the whole body has to be searched.
            # Scanning everything alone would false-positive on healthy pages
            # that merely ship the error string in a bundle, so it only counts
            # when the page also failed to render any real content.
            rate_limited = not rendered and any(
                m in _normalise(html) for m in RATE_LIMIT_MARKERS
            )

            if rendered and not challenged and not rate_limited:
                self.pages_fetched += 1
                return 200, html

            # Retrying a soft block just deepens it — only challenges are worth
            # a second attempt.
            if rate_limited:
                LOGGER.error(
                    "Upwork is serving its 'we'll be right back' page — the "
                    "session is rate limited. Stop and let it cool off."
                )
                break

            if attempt < attempts:
                reason = "Cloudflare challenge" if challenged else "page never rendered"
                LOGGER.warning(
                    "%s on %s — waiting then retrying once",
                    reason, url.rsplit("/", 1)[-1],
                )
                time.sleep(self._rng.uniform(6, 12))

        self.pages_fetched += 1
        if rate_limited:
            return 429, html
        if challenged:
            return 403, html
        return (503 if not rendered else status or 200), html

    __call__ = fetch


# Back-compat alias: this used to be Playwright-specific.
PlaywrightFetcher = BrowserFetcher


def build_browser_fetcher(
    proxy_manager: ProxyManager | None = None,
    headless: bool = False,
    offscreen: bool = True,
) -> BrowserFetcher:
    """Started fetcher, ready to hand to ClientEnricher. Caller must close()."""
    return BrowserFetcher(
        proxy_manager=proxy_manager, headless=headless, offscreen=offscreen
    ).start()
