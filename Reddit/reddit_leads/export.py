"""CSV output."""

import csv

COLUMNS = [
    "date_posted_utc",
    "days_ago",
    "subreddit",
    "title",
    "lead_type",
    "intent_evidence",
    "work_location",
    "remote_evidence",
    "pay_or_budget",
    "author",
    "post_url",
    "contact",
    "comments",
    "lead_score",
    "why_it_matches",
    "snippet",
]


def write_csv(rows, path):
    """Write leads to a UTF-8 CSV that opens cleanly in Sheets and Excel."""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
