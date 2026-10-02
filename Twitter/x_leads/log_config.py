"""Logging setup.

Everything goes to stderr so stdout stays clean for data — `x-leads --stdout
| jq` has to work.
"""

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def init_logger(verbose: bool = False, quiet: bool = False) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Playwright is chatty at DEBUG and none of it is ours.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def force_utf8_streams() -> None:
    """Stop Windows cp1252 from killing a run part-way through.

    Tweets are full of emoji and non-Latin script. On Windows a *redirected*
    stream defaults to cp1252 and raises UnicodeEncodeError mid-write, which
    silently truncates `> leads.json`.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
