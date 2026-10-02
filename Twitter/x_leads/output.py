"""Rendering leads: JSON, JSONL, CSV, and a human-readable digest.

The digest is the default because the point of this tool is a person reading
twenty posts over coffee and deciding who to message. Every entry leads with
the tweet URL, since the next action is always "open it and reply".
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from x_leads.models import Lead

CSV_FIELDS = [
    # `budget` sits up front with the verdict: after "is this a lead?" the next
    # question is always "is it worth answering first?".
    "verdict", "score", "budget", "tweet_url", "handle", "display_name",
    "followers", "posted_at", "age_hours", "text", "bio", "profile_url",
    "professional_category", "location", "location_country", "location_source",
    "likes", "replies", "views", "niche", "signals", "reject_reason",
]

_BADGE = {"hot": "HOT ", "warm": "WARM", "cold": "COLD", "rejected": "----"}


def to_json(leads: list[Lead], indent: int = 2) -> str:
    return json.dumps(
        [lead.to_dict() for lead in leads], indent=indent, ensure_ascii=False
    )


def to_jsonl(leads: list[Lead]) -> str:
    return "\n".join(
        json.dumps(lead.to_dict(), ensure_ascii=False) for lead in leads
    )


def to_csv(leads: list[Lead]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = lead.to_dict()
        row["signals"] = "; ".join(row["signals"])
        # Excel renders embedded newlines as extra rows in some importers.
        row["text"] = " ".join(str(row["text"]).split())
        row["bio"] = " ".join(str(row["bio"] or "").split())
        row["location"] = " ".join(str(row["location"] or "").split())
        writer.writerow(row)
    return buf.getvalue()


def _age(lead: Lead) -> str:
    hours = lead.tweet.age_hours
    if hours is None:
        return "?"
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def to_digest(leads: list[Lead], width: int = 100, show_signals: bool = True) -> str:
    """The read-it-and-act-on-it view."""
    if not leads:
        return (
            "No new leads.\n\n"
            "That is a normal result for a narrow window. Things to try:\n"
            "  --include-seen      show leads an earlier run already reported\n"
            "  --hours 72          look further back\n"
            "  --min-verdict cold  include the weaker matches\n"
            "  --include-rejected  see what was filtered out, and why\n"
        )

    out: list[str] = []
    counts: dict[str, int] = {}
    for lead in leads:
        counts[lead.verdict] = counts.get(lead.verdict, 0) + 1

    header = "  ".join(
        f"{v}: {counts[v]}" for v in ("hot", "warm", "cold", "rejected") if v in counts
    )
    out.append(f"{len(leads)} leads   ({header})")
    out.append("=" * width)

    for lead in leads:
        t = lead.tweet
        author = f"@{t.author.handle}" if t.author.handle else "@?"
        followers = f"{t.author.followers:,}" if t.author.followers else "?"
        # The resolved country, not the raw field: the raw text is in the CSV
        # for auditing, but a digest is read at a glance and wants one word.
        where = f", {lead.location_country}" if lead.location_country else ""
        out.append("")
        out.append(
            f"[{_BADGE.get(lead.verdict, '----')} {lead.score:>3}]  "
            f"{author}  ({followers} followers, {_age(lead)} ago{where})"
            + (f"   {lead.budget}" if lead.budget else "")
        )
        out.append(f"  {t.url}")

        body = " ".join(t.text.split())
        for line in _wrap(body, width - 4):
            out.append(f"    {line}")

        if t.author.bio:
            bio = " ".join(t.author.bio.split())
            out.append(f"    bio: {bio[:width - 10]}")
        if show_signals and lead.signals:
            out.append(f"    why: {', '.join(lead.signals[:8])}")
        if lead.reject_reason:
            out.append(f"    rejected: {lead.reject_reason}")

    out.append("")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width) or [""]


def render(leads: list[Lead], fmt: str) -> str:
    match fmt:
        case "json":
            return to_json(leads)
        case "jsonl":
            return to_jsonl(leads)
        case "csv":
            return to_csv(leads)
        case "digest":
            return to_digest(leads)
        case _:
            raise ValueError(f"Unknown format: {fmt}")


def default_filename(fmt: str, niche: str = "leads") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    ext = {"digest": "txt"}.get(fmt, fmt)
    return f"{niche}_{stamp}.{ext}"
