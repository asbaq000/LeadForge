"""X (Twitter) lead scraper for a video-editing business.

Finds people who are *asking* for video/content help — not editors advertising
themselves, and not YouTube-growth guru threads.
"""

__version__ = "1.0.0"


def __getattr__(name: str):
    """Lazy re-exports.

    `x_leads.scraper` pulls in playwright, and `cli.py` imports this package to
    read `__version__` — importing eagerly would make `--help` fail on a box
    where the browser was never installed.
    """
    if name in ("XLeadScraper", "ScrapeConfig", "ScrapeResult"):
        from x_leads import scraper
        return getattr(scraper, name)
    # No playwright behind this one, but it is re-exported the same way to keep
    # the import surface in one place.
    if name == "LocationFilter":
        from x_leads.leads.location import LocationFilter
        return LocationFilter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__", "XLeadScraper", "ScrapeConfig", "ScrapeResult",
    "LocationFilter",
]
