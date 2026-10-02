# Upwork Scraper (standalone)

The scraping core of [`Upwork-Job-Scraper`](../Upwork-Job-Scraper), extracted so it
runs on its own — **no PostgreSQL, no connection pool, no Docker Compose stack, no
migrations**. Point it at Upwork, get `Job` objects (or JSON / JSONL / CSV) back.

The original project is untouched and still works exactly as before. This is a
separate copy, not a refactor of it.

**Want to just run it?** → [**GUIDE.md**](GUIDE.md) has copy-paste commands for the
video / YouTube / creator niche.

## What it does

1. `curl_cffi` hits `https://www.upwork.com/` with `impersonate="chrome"` and pulls
   the `visitor_gql_token` cookie.
2. That cookie becomes the Bearer token for Upwork's GraphQL API (`/api/graphql/v1`).
3. Pages are fetched concurrently (50 jobs per page) and parsed into Pydantic models.
4. You get a `list[Job]` — print it, save it, queue it, insert it into your own database.

## Install

```bash
cd Upwork-Scraper-Standalone
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
```

No configuration required. A `.env` is optional — see [Configuration](#configuration).

## Command line

```bash
# 150 most recent jobs to stdout as JSON
python -m upwork_scraper --pages 3

# Keyword search into a CSV
python -m upwork_scraper --query "python scraper" --format csv --out jobs.csv

# Continuous feed, appending only jobs not seen before
python -m upwork_scraper --watch --interval 120 --out feed.jsonl

# Pipe into jq — logs go to stderr, data to stdout
python -m upwork_scraper --pages 1 | jq '.[].title'
```

After `pip install -e .` the `upwork-scraper` command works the same way.

| Flag | Default | Description |
|------|---------|-------------|
| `--pages N` | `3` | Pages to fetch, 50 jobs each. Upwork caps pagination near offset 5000 (~101 pages). |
| `--query TEXT` | — | Keyword filter. Repeat or comma-separate for several. |
| `--max-age MINUTES` | — | Keep only jobs published within the last N minutes. |
| `--max-age-days DAYS` | — | Same in days, e.g. `--max-age-days 2`. |
| `--enrich-clients` | off | Separate pass for job-poster details (browser). |
| `--exclude-country C` | India, Pakistan, Bangladesh, Egypt, Philippines | Drop jobs posted from this country. Repeat or comma-separate; adds to the default list. Needs `--enrich-clients`. |
| `--allow-country C` | — | Keep a country the default list excludes. |
| `--no-country-filter` | off | Keep every country, including the default exclusions. |
| `--drop-unknown-country` | off | Also drop jobs whose country could not be determined. |
| `--clients-out PATH` | `<out>.clients.jsonl` | Where client details go. |
| `--enrich-delay MIN MAX` | `4 11` | Randomised seconds between client requests. |
| `--enrich-limit N` | all | Only enrich the newest N jobs. |
| `--skip-backlog` | off | Watch mode: ignore jobs that already existed at startup. |
| `--sort ORDER` | `recency` | Upwork sort order. |
| `--format {json,jsonl,csv}` | `json` | Output format. |
| `--out PATH` | stdout | Write to a file instead. |
| `--watch` | off | Keep scraping on an interval until Ctrl+C. |
| `--interval N` | `120` | Seconds between cycles in watch mode. |
| `--no-proxy` | off | Ignore `WEBSHARE_URL` and connect directly. |
| `--webshare-url URL` | env | Webshare proxy list URL. |
| `--pin-proxy` | off | Send every page through the proxy that fetched the token. |
| `--workers N` | 10 (2 direct) | Concurrent page fetchers. |
| `--log-level LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |

## Niches

A niche bundles the keywords to search with the terms that decide what's on-topic,
so you can point the scraper at a whole market segment instead of one keyword.
The built-in `video` preset covers video editing, YouTube channels, short-form
social video, and creator/influencer content — 20 keywords.

```bash
python -m upwork_scraper --list-niches

# already-posted jobs
python -m upwork_scraper --niche video --backfill --backfill-pages 20 --format csv --out jobs.csv

# new jobs as they appear
python -m upwork_scraper --niche video --watch --interval 60 --pages 1 --skip-backlog --out live.jsonl

# both: history first, then keep streaming into the same file
python -m upwork_scraper --niche video --backfill --watch --interval 60 --pages 1 --out jobs.jsonl
```

Add keywords without losing the preset — `--query "drone footage"`, or
`--keywords-file my-keywords.txt` (one per line), or edit
`upwork_scraper/niches/video.json` directly. Point `--niche` at your own JSON file
to use a completely different set.

Because broad keywords like `influencer` return marketing roles with no video
work, each niche also carries `match_terms` / `exclude_terms` that drop off-topic
results (~5-10% of what the search returns). `--no-filter` turns that off.

Measured on 2026-08-12: 20 keywords x 4 pages backfilled **2,009 jobs spanning 61
days** in 48 seconds, from one IP with no proxies.

## Backfill: already-posted jobs

`--backfill` pages deep into history instead of skimming the newest jobs.
`--backfill-pages` sets the depth per keyword (50 jobs a page); Upwork caps
pagination near 101 pages.

The first page of every search is fetched on its own to read the result count,
so keywords with fewer matches stop early instead of hammering empty offsets —
asking for 100 pages of "davinci resolve" costs 16 requests, not 100.

## Real-time keyword feed

This is the main use case: a live stream of jobs as they get posted, filtered by
your keywords.

```bash
python -m upwork_scraper \
  --watch --interval 60 --pages 1 \
  --query "python,web scraping" --query react \
  --skip-backlog \
  --format jsonl --out feed.jsonl
```

- `--watch --interval 60` polls once a minute.
- `--pages 1` is enough — see the timing numbers below.
- `--skip-backlog` ignores the ~50 jobs that already existed when you started, so
  you only get jobs posted from now on. Without it, the first cycle dumps the
  current page as a baseline.
- Each keyword is searched separately and the results merged. A job matching two
  keywords is emitted once, with `matched_query` listing both.
- Jobs already emitted never repeat, so `feed.jsonl` only ever grows with genuinely
  new postings.

### How real-time is it, really?

Measured against the live API on 2026-08-12:

| | Measured |
|---|---|
| Age of the newest job on page 1 | **10–90 seconds** |
| New jobs site-wide | **~3 per minute** |
| Backlog covered by one 50-job page | **~15 minutes** |
| End-to-end catch latency at `--interval 40` | **42–147 seconds** after posting |

So: yes, it works. A job shows up in the API within about a minute of being
posted, and you see it on your next poll.

The 15-minute backlog window is the safety margin that matters. One page holds
roughly 15 minutes of postings, so at a 60s interval you are ~15× oversampled —
a slow cycle, a restart, or a brief network failure loses nothing. You'd have to
be down for 15+ minutes straight to actually miss a job, and even then the loop
warns you:

```
WARNING: Every one of the 50 jobs fetched was new — some may have been missed.
         Lower --interval or raise --pages.
```

Polling much faster than 60s buys little (~3 jobs/min site-wide means most cycles
return nothing) and costs rate-limit headroom. Below ~30s you are mostly making
requests to be told nothing changed.

**Narrow keywords are quiet, and that's normal.** At ~3 jobs/min site-wide, a
specific keyword may go many minutes with no match. An empty cycle means nothing
matched, not that the feed is broken — run without `--query` to confirm the
pipeline is live.

### Rate limits

Each keyword is a separate search, so a cycle costs `keywords × pages` requests.
Five keywords at one page each on a 60s interval is 5 requests/minute — fine
without proxies. If you want many keywords *and* a fast interval, set
`WEBSHARE_URL` so requests spread across rotating IPs.

## Client details (separate stage)

`--enrich-clients` runs a second pass *after* jobs are scraped, opening each job
page in a real browser to collect the poster's details. The fetching is its own
stage — an enrichment failure can never affect a job scrape — but the output is
merged: each job carries its poster under `client`, so a job can be qualified on
client history without joining files. `--separate-clients` also writes the
standalone file. In CSV the fields flatten to `client_*` columns.

From the **public, logged-out** page: country, city, client local time, member
since, total spent, total hires and active contracts, industry, company size,
how many proposals the job already has, when the client last viewed it,
interviewing/invite counts, and the job's location requirement. No account
needed. Star rating, review count and jobs-with-hires come from the page's embedded
state payload — present logged out, though the card never displays them. Hire
rate stays `null`: its denominator (`postedCount`) ships as null to visitors.

Needs the browser extra (`pip install -e ".[browser]"` then
`patchright install chrome`). Cloudflare blocks every plain HTTP client and
every stock-Playwright configuration; patchright driving real Chrome headful is
what works. The window is parked off-screen by default.

Paced hard: randomised 4-11s gaps, a long pause every ~12 pages, a persistent
profile that keeps Cloudflare clearance, one pinned IP for the run (rotating
proxies *causes* challenges here), backoff, and a circuit breaker that stops
after 5 consecutive failures. Failures skip the job rather than aborting.

Measured live: 8/8 jobs enriched at ~8s each.

See [GUIDE.md](GUIDE.md) § 4c for setup and tuning.

## Country filter

Jobs posted from India, Pakistan, Bangladesh, Egypt and the Philippines are
dropped by default.

```bash
# The default five, dropped
python -m upwork_scraper --niche video --limit 20 --enrich-clients --out jobs.json

# Add to the list, or take one off it
python -m upwork_scraper ... --enrich-clients --exclude-country "Nepal,Sri Lanka"
python -m upwork_scraper ... --enrich-clients --allow-country India

# Turn it off entirely
python -m upwork_scraper ... --no-country-filter
```

**It needs `--enrich-clients`.** The country is not in the search API — every
country-bearing field there (`client`, `location`, `clientCountry`, `buyer`,
`upworkHistoryData`) refuses a visitor token with an oauth2 scope error. It
comes from the job page, which is what enrichment loads. A run without
enrichment has no country on any job, so nothing can be excluded, and the CLI
warns rather than quietly keeping everything.

Because the country only exists after enrichment, an excluded job is still
enriched before being dropped — it costs its ~8s either way. `--limit 20` with
three excluded clients emits 17 jobs, not 20.

Matching is alias-aware: `India`, `IND` and `IN` are the same country, as are
`Philippines`, `PHL` and `Republic of the Philippines`. Jobs whose country could
not be determined — never enriched, or the lookup failed — are **kept**, since a
browser timeout is not evidence about where the client is. `--drop-unknown-country`
reverses that.

## Library

```python
from upwork_scraper import UpworkScraper

scraper = UpworkScraper()

jobs = scraper.scrape(max_pages=2)                          # list[Job], newest first
react = scraper.scrape(max_pages=1, query="react")          # one keyword
both = scraper.scrape(query=["react", "django"])            # several
fresh = scraper.scrape(query="python", max_age_minutes=30)  # recent only

for job in jobs:
    print(job.title, job.link, job.budget or job.hourly_low)

# Real-time feed — yields only jobs posted since the last cycle
for batch in scraper.scrape_loop(
    interval=60, max_pages=1, query=["python", "react"], skip_backlog=True
):
    for job in batch:
        print(f"[{job.matched_query}] {job.title} ({job.age_seconds():.0f}s old)")
        print(f"  {job.link}")
```

`Job` is a Pydantic model, so `job.model_dump(mode="json")` gives you a
JSON-ready dict. Fields: `cipher`, `title`, `description`, `link`, `skills`,
`published_date`, `job_type`, `is_hourly`, `hourly_low`, `hourly_high`,
`budget`, `duration_weeks`, `contractor_tier`, `matched_query`, plus an
`age_seconds()` helper.

`scrape()` refreshes the token and retries once if Upwork returns 401.
`scrape_loop()` catches errors, backs off 30s and keeps going, so a network blip
won't kill a long-running feed. Its seen-cipher set is capped at 20,000 entries
(days of history) so memory stays flat.

The country filter is a plain object with no dependencies on the rest of the
package, so any integration downstream of enrichment can reuse the same rules:

```python
from upwork_scraper import CountryFilter

blocked = CountryFilter.default()                       # the five, unknown kept
strict = CountryFilter.default(drop_unknown=True)       # unknown dropped too
custom = CountryFilter.build(add=["Nepal"], remove=["India"])
mine = CountryFilter.from_names(["Nepal", "Latvia"])    # exactly these

blocked.status("IND")           # "blocked"
blocked.status("Canada")        # "allowed"
blocked.status(None)            # "unknown"

kept = blocked.apply(jobs)               # filter, logging what went and why
kept, dropped = blocked.partition(jobs)  # both halves, order preserved
blocked.allows(job)                      # one job — reads job.client.country
```

`allows` has the same signature as `Niche.is_relevant`, so it can be passed as
`scrape(job_filter=...)` — though at scrape time no job has a country yet, so it
only does real work once enrichment has run.

Lower-level pieces are exported too, if you'd rather assemble them yourself:

```python
from upwork_scraper import TokenManager, build_proxy_manager, fetch_all_jobs

proxies = build_proxy_manager()
token = TokenManager().get_token()
jobs = fetch_all_jobs(token, proxies, max_pages=2)
```

## Configuration

Every setting is optional; the scraper runs with no `.env` at all.

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBSHARE_API_KEY` | unset | Webshare API key. Proxies are optional — unset means direct. |
| `WEBSHARE_URL` | unset | Older Webshare download-link form. The API key wins. |
| `MAX_PAGES` | `3` | Default pages per cycle. |
| `PAGE_SIZE` | `50` | Jobs per page. |
| `SCRAPE_INTERVAL` | `120` | Default watch-mode interval. |
| `EXCLUDED_COUNTRIES` | India, Pakistan, Bangladesh, Egypt, Philippines | Comma-separated country list, replacing the default. `none` turns the filter off. |

## Proxies

Proxies are optional here — that's the main behavioural difference from the
original project, which required `WEBSHARE_URL`.

- **Webshare credentials set** → `WebshareProxyManager` loads the list (by API
  key or download URL), rotates randomly per request, and refreshes hourly.
  10 concurrent workers. Verify they actually work with `--check-proxies`:
  datacenter proxies reach the open internet but are 403'd by Upwork.
- **unset (or `--no-proxy`)** → `NoProxyManager`, direct connection from your own
  IP, throttled to 2 concurrent workers to stay polite. Fine for light use; Upwork
  will rate-limit a single IP if you hammer it.

`--pin-proxy` routes every page through the same proxy that fetched the token.
Upwork issues the visitor token against the requesting egress IP, so pinning is
the safer choice when rotating proxies causes 401s. It's off by default, matching
the original project's rotate-per-page behaviour.

## Docker

```bash
docker build -t upwork-scraper .
docker run --rm upwork-scraper --pages 2 --format jsonl > jobs.jsonl
docker run --rm -e WEBSHARE_URL=https://... upwork-scraper --watch
```

## Tests

```bash
pip install pytest
python -m pytest tests -q
```

189 tests, no network access required — everything is mocked.

## What was left behind

Deliberately not carried over from the original project, since they are the
"extra" layers around the scraper:

| Dropped | Where it lived |
|---------|----------------|
| PostgreSQL connection pool | `src/postgres/core.py` |
| `insert_jobs()` / `get_job_count()` / `has_jobs()` | `src/postgres/jobs.py` |
| `jobs` table DDL and migrations | `resources/db/migrations/` |
| DB-driven scrape loop, bulk first run | `src/controllers/scraper_controller.py` |
| `psycopg[binary,pool]` dependency | `pyproject.toml` |
| Postgres service, volumes, compose stack | `docker-compose.yml` |
| Required `DATABASE_URL` | `src/settings/config.py` |

Dedup used to come from `ON CONFLICT (cipher) DO NOTHING`. Here it's in memory:
`scrape()` drops repeated ciphers within a batch, and `scrape_loop(only_new=True)`
tracks ciphers already yielded for the life of the loop. If you need dedup that
survives a restart, persist the ciphers yourself.

Added on top of the original scraping logic: optional proxies, multi-keyword
search, niche presets with relevance filtering, backfill with total-aware
pagination, the real-time feed (`--skip-backlog`, `--max-age`, gap warnings),
JSON/JSONL/CSV output, the `UpworkScraper` facade, and the CLI.
