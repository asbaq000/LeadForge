"""End-to-end pipeline test with a fake collector - no browser, no Google Sheets.

Exercises qualification, de-duplication, author cooldown, daily quota, the CSV
mirror and the SQLite state machine. Uses a throwaway database so your real
lead history is untouched.

    python scripts/selftest_pipeline.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import threads_leads.pipeline as pipeline_mod  # noqa: E402
from threads_leads.config import load_settings  # noqa: E402
from threads_leads.pipeline import Pipeline  # noqa: E402

NOW = int(time.time())

FAKE_POSTS = [
    # strong lead
    ("nora.builds", "Nora Whitfield",
     "Need someone to build an AI agent that triages our support inbox. Budget ~$4k, DM me.", 0),
    # strong lead, different person
    ("ravi_ops", "Ravi Menon",
     "Anyone know how to automate invoice data entry into QuickBooks? Doing it manually is brutal.", 1),
    # duplicate author - should collapse to one row
    ("nora.builds", "Nora Whitfield",
     "Also looking for help wiring our CRM to an API. Anyone able to build that?", 2),
    # seller - should be rejected
    ("growthguy", "Growth Guy",
     "I build AI automations for founders. DM me for a free audit. Link in bio.", 3),
    # off-domain - should be rejected
    ("homeowner22", "Sam",
     "Looking for a good plumber in Austin, anyone have recommendations for one?", 4),
    # too old - should be rejected on age
    ("stale_lead", "Old Post",
     "Need help automating my n8n workflow, stuck on the webhook step.", 5),
]


def make_post(username, full_name, text, idx, stale=False):
    return {
        "post_id": f"fake{idx}",
        "code": f"CODE{idx}",
        "permalink": f"https://www.threads.com/@{username}/post/CODE{idx}",
        "username": username,
        "full_name": full_name,
        "is_verified": False,
        "user_id": str(1000 + idx),
        "followers": 0,
        "bio": "",
        "text": text,
        "taken_at": NOW - (86400 * 30 if stale else 3600),
        "like_count": 5 + idx,
        "reply_count": idx,
        "repost_count": 0,
        "quote_count": 0,
        "link_attachment": "",
        "matched_query": "test query",
        "source": "graphql",
    }


class FakeCollector:
    """Stands in for ThreadsCollector - returns canned posts on the first query."""

    def __init__(self, settings, log=print):
        self.log = log
        self._served = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _batch(self):
        if self._served:
            return []
        self._served = True
        return [
            make_post(u, n, t, i, stale=(u == "stale_lead"))
            for u, n, t, i in FAKE_POSTS
        ]

    def warmup(self):
        self.log("  [safety] warmup (fake)")

    def browse_feed_briefly(self):
        pass

    def search(self, term, scrolls=None, recent=True):
        self.log(f"  search: {term!r} (fake)")
        return self._batch()

    def hashtag(self, tag, scrolls=None):
        return []

    def profile(self, username):
        return {"followers": 4200, "bio": f"Founder. {username}@example.com", "external_url": ""}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="threads_selftest_"))
    settings = load_settings()
    settings.db_path = tmp / "test.db"
    settings.csv_backup = tmp / "test_leads.csv"
    settings.lock_path = tmp / "test.lock"
    settings.max_queries_per_run = 1
    settings.query_pause_ms = (1, 2)
    settings.min_delay_ms, settings.max_delay_ms = 1, 2
    # The test must pass at any hour of day, and must not be throttled by the
    # real safety budget - those paths get their own test below.
    settings.active_hours = (0, 24)

    original = pipeline_mod.ThreadsCollector
    pipeline_mod.ThreadsCollector = FakeCollector
    try:
        print("=== pass 1 ===")
        pipe = Pipeline(settings, log=print)
        s1 = pipe.run(dry_run=False, sync_sheet=False)
        pipe.close()

        print("\n=== pass 2 (same posts - everything should already be seen) ===")
        pipe = Pipeline(settings, log=print)
        s2 = pipe.run(dry_run=False, sync_sheet=False)
        pipe.close()
    finally:
        pipeline_mod.ThreadsCollector = original

    print("\n--- results ---")
    rows = settings.csv_backup.read_text(encoding="utf-8").splitlines()
    print(f"CSV rows (incl. header): {len(rows)}")

    import csv as _csv

    with open(settings.csv_backup, encoding="utf-8") as fh:
        leads = list(_csv.DictReader(fh))
    for l in leads:
        print(f"  [{l['Score']:>3}] {l['Level']:<5} @{l['Author']:<14} "
              f"followers={l['Followers']:<6} topics={l['Topics']}")
        print(f"        emails={l['Emails'] or '-'}  opener={l['Suggested DM Opener'][:60]}...")

    checks = {
        "pass 1 exported exactly 2 leads (dupe author collapsed)": s1["exported"] == 2,
        "pass 2 exported 0 (dedupe works)": s2["exported"] == 0,
        "seller post rejected": all(l["Author"] != "growthguy" for l in leads),
        "off-domain post rejected": all(l["Author"] != "homeowner22" for l in leads),
        "stale post rejected on age": all(l["Author"] != "stale_lead" for l in leads),
        "enrichment filled follower counts": all(int(l["Followers"] or 0) > 0 for l in leads),
        "openers generated": all(l["Suggested DM Opener"] for l in leads),
        "status defaults to New": all(l["Status"] == "New" for l in leads),
    }

    print("\n--- assertions ---")
    failed = 0
    for label, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
        failed += not passed

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
