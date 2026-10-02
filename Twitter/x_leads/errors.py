"""Failure modes worth telling apart."""


class XLeadsError(Exception):
    """Base for everything this package raises."""


class NotSignedIn(XLeadsError):
    """No usable X session, and none could be obtained."""


class LoginTimeout(NotSignedIn):
    """The sign-in window was opened but nobody finished signing in."""


class SearchBlocked(XLeadsError):
    """X served a rate limit or challenge instead of results."""


class BrowserMissing(XLeadsError):
    """Playwright or its Chromium build is not installed."""
