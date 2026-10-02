# How to run it — video / YouTube / creator niche

Everything below is copy-paste. Commands are shown for Windows PowerShell; on
macOS/Linux swap `.venv\Scripts\python.exe` for `.venv/bin/python`.

---

## 0. The fastest way — double-click

Two launchers sit in this folder, already set up for the video niche:

| File | What it does |
|---|---|
| `run-backfill.cmd` | Grabs already-posted jobs into `jobs.csv`, then stops. ~4 min. |
| `run-live.cmd` | Watches for new jobs, appending to `live.jsonl`. Runs until you close it. |

Double-click either one. That's it — no setup, no typing.

With `run-live.cmd`, `live.jsonl` only appears once the first *new* job is
posted, which can take a few minutes. The window showing "No new jobs this
cycle" means it's working and waiting.

Everything below is the same thing with the flags spelled out, so you can change
what it fetches.

---

## 1. One-time setup

The virtualenv already exists in this folder. If you ever need to rebuild it:

```powershell
cd C:\Users\PC-05\Desktop\YT_Scrapper\Upwork-Scraper-Standalone
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Check it works:

```powershell
.\.venv\Scripts\python.exe -m upwork_scraper --list-niches
```

You should see the `video` niche with 20 keywords. No API key, no login, no
database, no proxy needed.

---

## 2. Get the jobs already posted (backfill)

This pulls existing history — the deeper you go, the further back you reach.

```powershell
.\.venv\Scripts\python.exe -m upwork_scraper `
  --niche video --backfill --backfill-pages 20 `
  --format csv --out jobs.csv
```

Open `jobs.csv` in Excel when it finishes.

**How deep to go** — `--backfill-pages` is per keyword, 50 jobs a page:

| `--backfill-pages` | Reaches back | Roughly |
|---|---|---|
| `4` | ~2 months | 2,000 jobs, 1 minute |
| `20` | further | ~8,000 jobs, 4 minutes |
| `100` | as far as Upwork allows | ~20,000 jobs, 20 minutes |

Upwork caps pagination at ~101 pages per keyword, so `100` is the practical
maximum. Keywords with fewer results stop early on their own — asking for 100
pages of "davinci resolve" only makes 16 requests, because that search has 754
results total.

---

## 3. Watch for new jobs in real time

```powershell
.\.venv\Scripts\python.exe -m upwork_scraper `
  --niche video --watch --interval 60 --pages 1 `
  --skip-backlog `
  --format jsonl --out live.jsonl
```

Leave it running. Every new matching job gets appended to `live.jsonl` as one
JSON object per line. Press `Ctrl+C` to stop.

- `--interval 60` — check once a minute. Jobs appear in Upwork's API within
  about a minute of being posted, so you'll see them 1–2 minutes after they go up.
- `--pages 1` — one page per keyword is plenty; a page holds ~15 minutes of
  postings, so you have a wide safety margin.
- `--skip-backlog` — don't dump the jobs that already existed when you started.
  Drop this flag if you *do* want that first snapshot.

Quiet stretches are normal. Upwork gets ~3 new jobs a minute across the whole
site, so only a fraction match the niche. Some cycles will report nothing.

---

## 4. Both at once — history, then live

Backfill first, then keep streaming into the same file. Jobs from the backfill
are remembered, so they won't repeat.

```powershell
.\.venv\Scripts\python.exe -m upwork_scraper `
  --niche video --backfill --backfill-pages 20 `
  --watch --interval 60 --pages 1 `
  --format jsonl --out jobs.jsonl
```

This is the one to use if you want the full picture in a single file.

---

## 4b. Only jobs from the last 2 days

Add `--max-age-days 2` to any command:

```powershell
.\.venv\Scripts\python.exe -m upwork_scraper `
  --niche video --backfill --backfill-pages 20 `
  --max-age-days 2 `
  --format csv --out jobs.csv
```

Verified live: 857 jobs in, 710 kept, oldest 1.98 days, none over the line.

---

## 4c. Client info (who posted the job)

Works, verified live: **8/8 jobs enriched at ~8 seconds each.**

```powershell
.\.venv\Scripts\python.exe -m upwork_scraper `
  --niche video --pages 1 --max-age-days 2 `
  --enrich-clients --enrich-limit 30 `
  --format jsonl --out jobs.jsonl
```

Client details are merged **into each job**, in one file, so you can qualify a
job on its poster in a single pass:

```json
{
  "title": "Meta and TikTok Ads Creative Strategist",
  "budget": 500,
  "contractor_tier": "ExpertLevel",
  "client": {
    "country": "AUS",
    "city": "Sydney",
    "member_since": "Jul 31, 2025",
    "total_spent": 7500.0,
    "total_hires": 4,
    "active_hires": 1,
    "industry": "Real Estate",
    "company_size": "Small company (2-9 people)",
    "proposals": "Less than 5",
    "last_viewed": "54 seconds ago",
    "interviewing": 0,
    "invites_sent": 0,
    "job_location": "Worldwide",
    "fetch_status": "ok"
  }
}
```

`client` is `null` for jobs that were never enriched (anything past
`--enrich-limit`). A job that *was* attempted but failed still gets a `client`
object with `fetch_status` set to `blocked` or `failed` — so "no data" and
"never tried" stay distinguishable.

In CSV the same fields flatten to `client_country`, `client_total_spent`,
`client_proposals` and so on, ready for a spreadsheet filter.

Add `--separate-clients` if you also want the standalone client file.

### What you get

Everything below comes from the **public, logged-out** job page. No Upwork
account needed.

| Field | Example | Coverage |
|---|---|---|
| Country | `United States` | always |
| City | `Ewa Beach` | most |
| Client local time | `9:01 PM` | always |
| Member since | `Oct 27, 2020` | always |
| **Total spent** | `$14,000` | clients with history |
| **Total hires / active** | `21 hires, 4 active` | clients with history |
| **Total hours** | `70,699 hours` | hourly clients |
| **Rating** | `4.97` | clients with feedback |
| **Reviews** | `218` | clients with feedback |
| **Jobs with hires** | `149` | clients with history |
| **Avg spend per hire** | `$862` (computed) | clients with history |
| **Active hire share** | `2%` (computed) | clients with history |
| **Industry** | `Real Estate` | clients with history |
| **Company size** | `Small company (2-9 people)` | clients with history |
| **Proposals so far** | `Less than 5`, `5 to 10` | always |
| Last viewed by client | `54 seconds ago` | when recent |
| Interviewing / invites / unanswered | `1` / `3` / `0` | always |
| Job location requirement | `Only freelancers located in the U.S. may apply.` | always |

A brand-new client genuinely has no spend or hire history — those fields come
back `null` rather than zero, so you can tell "no history" from "never fetched".

Still login-only, and left `null`: **jobs posted**, hire rate percentage, star
rating, review count, payment-verified badge.

### Hire rate needs a sign-in, and costs you

Hire rate is jobs-with-hires ÷ **jobs posted**. Logged out, `postedCount` ships
as `null`, so the denominator does not exist and the number cannot be derived.

Signed in it works. Measured 2026-08-12 immediately after `--login`:

```
hire rate          89
jobs posted        17
open jobs           2
jobs with hires    15
total hires        32
```

15/17 = 88.2%, which matches the 89 Upwork reports — the formula is confirmed.

**But the session was soft-blocked after roughly a dozen page loads.** Upwork
started serving its "We'll be right back" error page to that session: HTTP 200,
app shell renders, never hydrates, signed-out nav. Not a Cloudflare challenge —
Upwork's own throttle.

The anonymous profile kept working from the same IP at the same moment, 3/3
successful. **The throttle is tied to the account, not the address**, which is
why proxies do not help here and why the two profiles are kept separate.

Practical shape: keep the daily run anonymous, and use `--logged-in` in small
deliberate batches on jobs you already shortlisted:

```powershell
--limit 3 --enrich-clients --logged-in --enrich-delay 180 300
```

If you see `rate_limited` in the output, stop for a few hours. Retrying deepens
it, so the run halts itself on the first occurrence rather than grinding on.

### `--logged-in` never downgrades behind your back

The whole point of the flag is hire rate, so a run that cannot deliver it asks
rather than quietly producing a file with an empty column:

```
--logged-in was asked for, but this profile is not signed in.
Hire rate and jobs-posted come only from a signed-in session; every
other client field works either way.

  [1] Sign in now — a browser window opens  <- default
  [2] Reset this profile, then sign in (use if signing in keeps failing)
  [3] Carry on without hire rate or jobs-posted
  [4] Stop, change nothing
```

`[2]` is the default when the profile is rate limited, because signing in on
top of a flagged profile just reproduces the block. `[4]` exits 3 before any
scraping happens.

Unattended runs are never asked — `--watch`, `--no-auto-login`, and anything
whose stdin is not a terminal (a scheduled task, a pipe) keep the old
behaviour: sign in automatically where allowed, otherwise log the reason and
continue anonymously.

Two things back that up, because a session check can only ever predict:

- A profile holding no Upwork session cookies is signed out, decided without
  loading a page. `--reset-login` followed immediately by `--logged-in` used to
  report a live session and enrich anonymously — the reset profile has no
  visitor nav to detect *because it has nothing yet*, and that absence was
  being read as proof of a session.
- After enrichment, a signed-in run where not one client returned a hire rate
  says so, and points at `--login-status`.

What the payload *does* give, none of which the card displays:

| Field | Example |
|---|---|
| `rating` | `4.97` |
| `total_reviews` | `218` |
| `total_jobs_with_hires` | `149` |
| exact `total_spent` | `325900.1` (the card rounds to "$326K") |

`avg_spend_per_hire` is the usable stand-in, and arguably tells you more:

```
Prague, Czech Republic    $326,000 spent   378 hires   $862/hire   70,699 hours
New York, United States     $5,300 spent   262 hires    $20/hire      103 hours
Ketchikan, United States      $535 spent    22 hires    $24/hire
```

Same "lots of hires" signal, very different clients. `active_hire_share`
(active ÷ total hires) shows how busy they are right now.

The proposal count is arguably the most useful field here — it tells you how
much competition a job already has before you spend a connect on it.

### Setup

One extra install, already done in this folder:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[browser]"
.\.venv\Scripts\python.exe -m patchright install chrome
```

### Why a browser, and why this browser

Job pages are public, but Cloudflare refuses automated clients. Measured, not
guessed — every row below was run against live job pages:

| Approach | Result |
|---|---|
| curl_cffi, 7 TLS impersonations (chrome99-146, safari, firefox, edge) | 403 |
| Playwright, bundled Chromium, headless | 403 challenge |
| Playwright, bundled Chromium, headful | 403 challenge |
| Playwright, real Chrome, headless | 403 challenge |
| Playwright, real Chrome, headful | 403 challenge |
| Playwright, real Chrome, persistent profile | 403 challenge |
| patchright, real Chrome, headless | 403 challenge |
| **patchright, real Chrome, headful** | **works** |

Cloudflare fingerprints the CDP traces stock Playwright leaks; patchright patches
them out. Headless is detected separately, so the browser has to run headful —
by default the window is parked 2400px off-screen, so you won't see it. Add
`--headful` to watch it work.

### Not getting blocked

| Protection | Default |
|---|---|
| Randomised gap between pages | 4-11s, never a fixed interval |
| Longer pause now and then | 25-60s every ~12 pages |
| Persistent browser profile | keeps Cloudflare clearance between runs |
| One IP for the whole run | see below |
| Retry once on a challenge | with a 6-12s settle wait |
| Backoff on failure | 20s, doubling, capped at 5 min |
| **Circuit breaker** | stops after 5 consecutive failures |

**Counter-intuitive but important: do not rotate proxies here.** A Cloudflare
clearance cookie is bound to the IP that earned it, so a new IP per request
throws clearance away and triggers a fresh challenge every time. The enrichment
browser pins one proxy for the whole run. This is the opposite of the right
policy for the job search, which does rotate.

If you do get blocked, slow down rather than switching IPs:

```powershell
--enrich-delay 15 40
```

At the default pace expect ~8s per job, so 30 jobs takes about 5 minutes. Use
`--enrich-limit` to keep batches sane — enriching thousands of backfilled jobs
would take days and is the surest way to get noticed.

**Terms of service, plainly:** automated scraping of job pages is against
Upwork's terms regardless of transport. The pacing above protects your IP; it
does not make this sanctioned. Your call.

---

## 5. Adding your own keywords

Three ways, all of which **add to** the built-in list rather than replacing it.

**A. On the command line** — quickest for a one-off:

```powershell
--query "drone footage" --query "wedding videographer,twitch editor"
```

**B. In a file** — best when you have a lot. Make `my-keywords.txt`:

```
# one per line, # starts a comment
drone footage
wedding videographer
anime editing
faceless youtube channel
```

Then:

```powershell
--niche video --keywords-file my-keywords.txt
```

**C. Edit the niche itself** — permanent, and lets you tune the filter too:

```
upwork_scraper\niches\video.json
```

It has three lists:

| List | What it does |
|---|---|
| `keywords` | What gets searched on Upwork. One request per keyword per page. |
| `match_terms` | A job is kept if any of these is in its title or skills. |
| `exclude_terms` | A job is dropped if any of these is in its title. |

Copy the file, edit your copy, and point at it with `--niche my-niche.json` if
you'd rather not modify the original.

**Keywords cost requests.** Each keyword is a separate search, so a cycle costs
`keywords × pages` requests. 20 keywords at 1 page on a 60s interval is 20
requests/minute, which is fine from one IP. If you want a lot more keywords or a
much faster interval, set `WEBSHARE_URL` in a `.env` file to spread the load
across rotating proxies.

---

## 6. Output formats

| Flag | Best for |
|---|---|
| `--format csv` | Excel / Google Sheets |
| `--format jsonl` | Live feeds and appending — one JSON object per line |
| `--format json` | One-off snapshots you'll load in code |

Leave off `--out` and it prints to the screen instead (logs go to stderr, data to
stdout, so `| jq` works).

Each job includes: title, description, link, skills, budget or hourly range,
published date, contractor tier, and `matched_query` — which of your keywords
found it.

---

## 7. Tuning the results

**Too much noise?** The relevance filter is on by default with `--niche`. Broad
keywords like `influencer` and `social media video` pull in marketing roles with
no video work, and the filter removes them (typically ~5–10% of results). To see
everything the search returned, add `--no-filter`. To cut more, add terms to
`exclude_terms` in the niche file.

**Missing jobs you'd expect?** Add the keyword you'd search with yourself. Check
what a keyword actually returns:

```powershell
.\.venv\Scripts\python.exe -m upwork_scraper --query "your keyword" --pages 1 --no-filter
```

**Only want fresh postings?** `--max-age 30` keeps only jobs published in the
last 30 minutes.

---

## 7b. Excluding countries

Jobs posted from **India, Pakistan, Bangladesh, Egypt and the Philippines** are
dropped automatically. You do not have to ask for it — it is the default.

**One thing to know: it only works with `--enrich-clients`.** Upwork's search
does not tell us where the client is; that only appears on the job page, which
is what the client lookup opens. Without `--enrich-clients` there is no country
to check, so nothing gets excluded and you'll see this warning:

```
Country filter is on but --enrich-clients is not: the search API does not
return the client's country, so nothing can be excluded.
```

A normal run therefore looks like this:

```powershell
.\.venv\Scripts\python.exe -m upwork_scraper `
  --niche video --pages 1 --max-age-days 2 `
  --limit 20 --enrich-clients `
  --format csv --out jobs.csv
```

and reports what it dropped:

```
Country filter: excluding Bangladesh, Egypt, India, Pakistan, Philippines (unknown country: kept)
Client details attached for 20/20 jobs
Country filter dropped 3 of 20 jobs (India 2, Philippines 1)
```

Because the country is only known *after* the lookup, an excluded job still
costs its ~8 seconds. `--limit 20` with 3 excluded clients writes 17 rows, not
20 — ask for a few more than you need.

**Changing the list:**

| What you want | Add this |
|---|---|
| Also exclude somewhere else | `--exclude-country Nepal` (repeat, or `"Nepal,Kenya"`) |
| Keep one of the five after all | `--allow-country India` |
| Everywhere, no exclusions | `--no-country-filter` |
| Also drop jobs where the lookup failed | `--drop-unknown-country` |

To change the default permanently, put it in `.env` instead of typing it every
run:

```
EXCLUDED_COUNTRIES=India,Pakistan,Bangladesh,Egypt,Philippines,Nepal
```

`EXCLUDED_COUNTRIES=none` turns it off.

Spelling doesn't matter: `India`, `IND` and `IN` are all the same country, as
are `Philippines`, `PHL` and `Republic of the Philippines`.

**Jobs where the country couldn't be read are kept.** If a page fails to load,
that tells us nothing about where the client is, and throwing the job away over
a browser timeout loses work you'd have wanted. Those rows have an empty
`client_country` and a `client_fetch_status` that isn't `ok`. Use
`--drop-unknown-country` if you'd rather only ever see confirmed-good countries.

---

## 8. If something goes wrong

| Symptom | What it means |
|---|---|
| `Every one of the N jobs fetched was new — some may have been missed` | The gap between polls got too long. Lower `--interval` or raise `--pages`. |
| `No new jobs this cycle`, repeatedly | Normal. Nothing matching was posted. Run without `--niche` to confirm the feed is live. |
| `Token expired, refreshing` | Normal, handled automatically. |
| Everything fails after running a long backfill | Probably rate-limited from one IP. Wait a few minutes, then use a smaller `--backfill-pages` or set up proxies. |

Add `--log-level DEBUG` for more detail.

---

## Quick reference

```powershell
# what niches exist
--list-niches

# history only
--niche video --backfill --backfill-pages 20 --format csv --out jobs.csv

# live only
--niche video --watch --interval 60 --pages 1 --skip-backlog --out live.jsonl

# both
--niche video --backfill --backfill-pages 20 --watch --interval 60 --pages 1 --out jobs.jsonl

# with your own keywords
--niche video --keywords-file my-keywords.txt --query "extra keyword"

# everything, unfiltered
--niche video --no-filter

# country exclusions (needs --enrich-clients to have any effect)
--niche video --limit 20 --enrich-clients            # drops IN/PK/BD/EG/PH
--enrich-clients --exclude-country "Nepal,Kenya"     # add more
--enrich-clients --allow-country India               # keep one of the five
--no-country-filter                                  # everywhere
```
