"""Rendering accounts: CSV, JSON, and a readable digest."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from ig_leads.models import CSV_FIELDS, Account


def to_csv(accounts: list[Account]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for a in accounts:
        writer.writerow(a.to_dict())
    return buf.getvalue()


def to_json(accounts: list[Account]) -> str:
    return json.dumps([a.to_dict() for a in accounts], indent=2, ensure_ascii=False)


def to_digest(accounts: list[Account], width: int = 100) -> str:
    if not accounts:
        return (
            "No accounts matched.\n\n"
            "Things to try:\n"
            "  --niches wildlife,restoration   search different niches\n"
            "  --min-posts 3                   loosen the posting-frequency rule\n"
            "  --max-accounts 40               check more candidates (costs budget)\n"
            "  --include-rejected              see what was filtered out, and why\n"
        )

    out = [f"{len(accounts)} accounts", "=" * width]
    for a in accounts:
        out.append("")
        flags = []
        if a.is_verified:
            flags.append("verified")
        if a.is_business:
            flags.append("business")
        tag = f"  [{', '.join(flags)}]" if flags else ""
        out.append(
            f"@{a.username}  ({a.followers:,} followers, "
            f"{a.posts_in_window} posts/14d){tag}"
        )
        out.append(f"  {a.profile_url}")
        if a.full_name:
            out.append(f"    {a.full_name}")
        if a.biography:
            bio = " ".join(a.biography.split())
            out.append(f"    {bio[:width - 6]}")
        if a.category:
            out.append(f"    category: {a.category}")
        if a.external_url:
            out.append(f"    link: {a.external_url}")
        out.append(f"    niche: {a.niche}  (#{a.hashtag})")
        if a.reasons:
            out.append(f"    why: {', '.join(a.reasons[:6])}")
        if a.reject_reason:
            out.append(f"    rejected: {a.reject_reason}")
    out.append("")
    return "\n".join(out)


def render(accounts: list[Account], fmt: str) -> str:
    match fmt:
        case "csv":
            return to_csv(accounts)
        case "json":
            return to_json(accounts)
        case "digest":
            return to_digest(accounts)
        case _:
            raise ValueError(f"Unknown format: {fmt}")


def default_filename(fmt: str, prefix: str = "ig_leads") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    ext = {"digest": "txt"}.get(fmt, fmt)
    return f"{prefix}_{stamp}.{ext}"
