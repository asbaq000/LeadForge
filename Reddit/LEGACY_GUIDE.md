# Reddit AI Leads

Finds **recent, remote, paid AI work** on Reddit — job posts and one-off AI
projects — and writes them to a CSV (or straight into a Google Sheet).

No Reddit account, no app registration, no OAuth token, no API key. Every
Reddit listing answers on `.json`, and that is all this reads.

## Quick start

```bash
git clone https://github.com/noorzahid-midnights/Reddit-Scraper.git
cd Reddit-Scraper
python3 -m reddit_leads
```

That writes `leads.csv` with every lead that passes the filters. Nothing to
install — the tool is Python 3.8+ standard library only.

```bash
python3 -m reddit_leads --limit 40 --days 7 --out this_week.csv
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `-n, --limit` | `0` | how many leads to return; `0` = every match |
| `-d, --days` | `14` | maximum post age |
| `-o, --out` | `leads.csv` | output path |
| `--delay` | `2.0` | seconds between requests (raise if rate-limited) |
| `--save-raw PATH` | – | also dump every fetched post as JSON |
| `--from-json PATH` | – | re-score a saved dump without re-fetching |

A full run makes 92 requests and takes roughly three minutes. There is no cap
on how many leads come back: the filters decide, not a quota.

## What counts as a lead

A post has to clear every one of these:

1. **Posted within the last 14 days.** Anything older is dropped, and so is
   anything with a future timestamp.
2. **About AI work.** Matching is word-boundary based, so `AI` matches "AI" but
   not "s**ai**d" or "m**ai**ntain". Strong terms (`langchain`, `llm`, `rag`,
   `computer vision`, …) qualify a post on their own; weak ones (`python`,
   `automation`) only count when two or more appear together.

   Crucially, a **negated** mention does not count. Task subreddits are full of
   "do not use ChatGPT" and "no AI generated answers, I want a real person" —
   posts that name AI precisely because they are *not* AI work. A phrase ruling
   AI out rejects the post outright, unless some genuine, un-negated AI term is
   also present: "[Hiring] AI engineer — no AI-written cover letters" is still
   a real lead.

   In the micro-task subreddits (`r/slavelabour`, `r/DoneDirtCheap`) a couple of
   weak words means nothing, so a lead there must name real AI work.
3. **Someone hiring, not someone talking.** See below — this is the gate that
   keeps out course ads, news and venting.
4. **Remote, strictly.** See below.
5. **Not a duplicate.** See below.

### The demand rule

Generic job words are useless as a filter: "paid", "contract", "rate" and
"apply" all appear in Udemy ads, product announcements and rants. So a post has
to show that the poster **themselves** is hiring or commissioning the work.
Five gates, in order:

1. **No course or referral promotion anywhere** — Udemy, coupon codes, "enroll
   now", affiliate links. This list is deliberately narrow: "newsletter",
   "youtube" and "bootcamp" are *not* on it, because "automate my newsletter"
   is a real brief.
2. **The title must not announce a different kind of post** — news
   ("announces", "releases"), venting ("rant", "unpopular opinion"), showcase
   ("I built", "check out my"), advice-seeking ("how do I", "any advice") or
   job-seeking ("open to work"). Only the *title* vetoes: the same words in a
   body are often incidental, and "we just launched, now we need an AI dev" is
   a genuine lead.
3. **First-person hiring intent**, matched two ways — fixed phrases ("we're
   hiring", "my budget is", "willing to pay") and, because real posts write
   "looking for a remote n8n automation freelancer", a **seek verb followed
   within 60 characters by a role noun**. Fixed phrases alone miss those.
4. **An actionable hook** — a stated budget, a contact route, or an explicit
   `[Hiring]` tag. Intent with no way to act on it is not a lead.
5. **Outside the gig subreddits, the bar rises** to money or a `[Hiring]` tag,
   because that is where the courses, news and rants come from.

The `intent_evidence` column shows the exact phrases that passed a lead, so
every row can be audited.

### The remote rule

Two independent gates, because "didn't mention an office" is not the same as
"is remote":

- **Any live onsite marker disqualifies the post** — `onsite`, `on-site`,
  `hybrid`, `in person`, `must be located`, `must reside`, `relocate`,
  `commute`, `days in office`, `our office`, and similar.
- The marker is ignored when it is **negated**: "remote, **no** onsite",
  "**not** hybrid", "**without** relocation" all stay in the results. This is
  what stops the filter from throwing away good leads that mention the word
  only to rule it out.
- After that, the post still needs **positive remote evidence** — an explicit
  `remote` / `work from home` / `worldwide` / `any timezone` statement, or
  membership of a subreddit whose own rules make every post remote
  (`r/RemoteJobs`, `r/remotejs`, `r/WorkOnline`, `r/jobbit`). Silence is not
  treated as remote.

### Deduplication

The same gig arrives several times: one post is returned by several queries,
and posters re-post the same ad every few days under a new ID. Four identities
are checked:

| Key | Catches |
| --- | --- |
| post ID | the same post returned by two different queries |
| author + title fingerprint | one poster re-posting their ad |
| title fingerprint alone | copy-paste ads from different accounts |
| external link | the same job board URL posted twice |

The fingerprint is the set of meaningful words in the title, sorted, so word
order and filler words do not defeat it. The title-only key needs at least four
distinctive words before it fires — otherwise two unrelated clients both
posting "Looking for an AI developer" would collapse into one lead.

When duplicates collide, the highest-scoring copy survives.

### Ranking

Leads are sorted by a score built from: AI relevance, hiring intent (stronger
when it is in the title), whether a budget is stated, freshness inside the
14-day window, whether remote is stated in the title, post length, and comment
count. Every lead carries a `why_it_matches` column showing exactly which
signals fired, so you can audit or re-tune the ranking.

## Output columns

`date_posted_utc`, `days_ago`, `subreddit`, `title`, `lead_type` (job vs
project/gig), `work_location`, `remote_evidence`, `pay_or_budget`, `author`,
`post_url`, `contact`, `comments`, `lead_score`, `why_it_matches`, `snippet`.

The CSV is UTF-8 with a BOM, so it opens correctly in Excel and imports into
Google Sheets without mangling non-ASCII characters.

## Google Sheets version

`google_apps_script/RedditLeads.gs` runs the same rules inside Google Sheets,
with no local Python at all — Google's servers do the fetching.

It reads Reddit's **Atom feeds** rather than the JSON API, because Reddit
answers `.json` with HTTP 403 and a block page when the request comes from a
datacenter IP range, which is where Apps Script runs. The `.rss` endpoints are
served normally from those same addresses. The trade-off is that feeds carry no
comment count (the column shows `n/a`) and search feeds sometimes omit the post
body. Run `testFetch` first if anything looks wrong — it reports the HTTP
status and parsed entry count for each feed type.

1. Open <https://sheets.new>
2. **Extensions → Apps Script**
3. Delete the placeholder, paste the contents of `RedditLeads.gs`, **Save**
4. Run `generateLeads` once and approve the permission prompt
5. Reload the sheet — a **Reddit Leads** menu appears; use it any time

Results land in a sheet named **AI Remote Leads**, everything filtered out lands
in **Rejected (audit)** with the reason, and each run appends a line to
**Run log**.

### Twice-daily auto-update

**Reddit Leads → Turn on twice-daily auto-update** installs two time-driven
triggers (08:00 and 20:00 by default, in the script's timezone). Google will ask
for one extra authorization the first time, because scheduling and email need
permissions the manual run does not.

Runs are **append-only**. Each run compares what it found against the post IDs
already in the sheet and adds only genuinely new leads, at the top, stamped with
`first_seen`. Running it twice on the same posts adds nothing the second time,
so the sheet accumulates instead of being rewritten — nothing you have already
worked is lost.

Already-seen IDs are read back out of the sheet rather than kept in script
properties, so deleting a row by hand really does forget it, and the two can
never drift apart.

**Reddit Leads → Auto-update status** reports whether the schedule is live.
To stop the schedule, either use **Turn off auto-update**, or delete the
triggers by hand from the Apps Script editor's **clock icon** (Triggers) — both
do the same thing, and no code change is needed either way.
The **Run log** sheet records every run — when, how many posts were scanned, how
many matched, how many were new — so a silent failure is visible rather than
looking like a quiet week.

| `CONFIG` key | Default | Meaning |
| --- | --- | --- |
| `RUN_HOURS` | `[8, 20]` | hours of the day to run |
| `MAX_NEW_PER_RUN` | `0` | cap on leads added per run; `0` = no cap |
| `KEEP_DAYS` | `30` | drop rows added longer ago than this; `0` keeps everything |
| `EMAIL_ON_NEW_LEADS` | `false` | email you when new leads land |
| `EMAIL_TO` | `''` | blank = whoever owns the script |
| `MAX_AGE_DAYS` | `14` | ignore posts older than this |
| `WRITE_AUDIT_SHEET` | `true` | write the rejection audit sheet |
| `SPREADSHEET_ID` | `''` | only needed if the script is not bound to a Sheet |

## Tuning

Everything you would want to change lives in `reddit_leads/config.py`:
subreddits, search queries, keyword vocabularies, the onsite/negation lists,
and the request delay. Adding a subreddit is one line.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The Apps Script port has its own test, which shims the few Google APIs it uses
so the Atom parsing and filtering can run outside Google:

```bash
npm install @xmldom/xmldom
node tests/test_apps_script.js   # Atom parsing, filtering, noise rejection
node tests/test_scheduling.js    # append-only sheet, dedupe across runs, triggers
```

40 Python tests cover the demand gate and its noise classes, the
AI-negation logic, the onsite/negation logic, the AI-term false-positive guards,
the recency window, self-promo exclusion, all four dedupe keys, and the
end-to-end pipeline.

## Rate limits

Reddit throttles unauthenticated clients. The Python client sends a descriptive
User-Agent, waits 2 seconds between requests, and backs off exponentially on
`429` and `5xx`. If you still get throttled, raise `--delay`. Individual source
failures are reported and skipped rather than aborting the run.

Reddit also blocks the JSON API outright for datacenter IP ranges — cloud VMs,
CI runners and Apps Script all get HTTP 403. Running the Python tool from a
normal home or office connection is unaffected; running it on a VPS is not, and
that is what the Atom-feed path in the Apps Script version works around.
