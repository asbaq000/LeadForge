"""Failure modes worth telling apart.

`AccountAtRisk` is the important one. Everything that could plausibly mean
Instagram has noticed the automation raises it, and it always stops the run
immediately rather than retrying. Retrying into a block is how spare accounts
become banned accounts.
"""


class IGLeadsError(Exception):
    """Base for everything this package raises."""


class NotSignedIn(IGLeadsError):
    """No usable Instagram session, and none could be obtained."""


class LoginTimeout(NotSignedIn):
    """The sign-in window opened but nobody finished signing in."""


class AccountAtRisk(IGLeadsError):
    """Instagram pushed back. Stop now; do not retry.

    Raised for 401, 429, checkpoint/challenge redirects and feedback_required.
    Every one of these means the account has been noticed, and continuing
    escalates a temporary throttle into a ban.
    """


class BudgetExhausted(IGLeadsError):
    """The self-imposed request budget for this window is used up.

    Not an Instagram error — this is the scraper refusing to keep going.
    """


class BrowserMissing(IGLeadsError):
    """Playwright or its Chromium build is not installed."""
