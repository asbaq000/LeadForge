# Upwork scraper - commands

## The two important ones (CSV)

Add `--format csv` and name the file `.csv`. Everything the JSON holds is in
the CSV too - 14 job columns, plus 32 `client_*` columns once `--enrich-clients`
has run.

**1. Ten jobs, up to 2 days old, full client data (no hire rate no login required)**

```
python -m upwork_scraper --niche video --pages 1 --max-age-days 2 --limit 10 --enrich-clients --format csv --out jobs.csv
```

**2. Same, with hire rate (signed in)**

```
python -m upwork_scraper --reset-login
python -m upwork_scraper --login
python -m upwork_scraper --niche video --pages 1 --max-age-days 2 --limit 5 --enrich-clients --logged-in --format csv --out jobs.csv
```

Keep `--limit 5` on the signed-in run. A signed-in session gets soft-blocked
after roughly a dozen job-page loads, and the block is account-scoped, so a
bigger limit costs you the account's access rather than getting you more rows.

`--reset-login` is only needed when the profile is already rate limited or you
are switching accounts; a working session can go straight to run 2.

Want JSON instead? Drop `--format csv` and use `--out jobs.json`.

## Countries

India, Pakistan, Bangladesh, Egypt and the Philippines are excluded from every
run. Nothing to add - it is the default.

It only works with `--enrich-clients`, because that is the only stage that
learns where the client is; without it you get a warning and no exclusions.
Excluded jobs are still looked up before being dropped, so `--limit 10` with two
Indian clients writes 8 rows - ask for a couple extra when you need a full ten.

```
--exclude-country "Nepal,Kenya"   add more countries
--allow-country India             keep one of the five after all
--no-country-filter               everywhere, no exclusions
--drop-unknown-country            also drop rows where the lookup failed
```

Permanent change: put `EXCLUDED_COUNTRIES=India,Pakistan,Nepal` in `.env`
(`none` turns it off).

The Standard Run

bash
python -m upwork_scraper --niche video --pages 1 --out jobs.json
Quickly scrapes the first page of jobs using your video.json keywords and saves to jobs.json.

The "Get Everything" Run

bash
python -m upwork_scraper --niche video --pages 1 --enrich-clients --out jobs.json
Scrapes jobs AND opens the background browser to fetch all the rich client details (country, hire rate, etc).


fetch 10 latest to 2 days old jobs with full data but without hire rate
python -m upwork_scraper --niche video --pages 1 --max-age-days 2 --limit 10 --enrich-clients --out jobs.json


The "Testing" Run

bash
python -m upwork_scraper --niche video --pages 1 --enrich-clients --enrich-limit 3 --out test.json
Scrapes jobs but only enriches the first 3 clients (perfect for making sure things work without waiting a long time).

The "Only New Jobs" Run

bash
python -m upwork_scraper --niche video --pages 1 --max-age 60 --enrich-clients --out new_jobs.json
Scrapes jobs but throws away anything older than 60 minutes, so you only get fresh postings.

The "Live Watcher" Run

bash
python -m upwork_scraper --niche video --watch --interval 120 --skip-backlog --out live_jobs.jsonl
Runs continuously, checking Upwork every 120 seconds and saving any brand new jobs it finds to a file.

The Custom Search Run

bash
python -m upwork_scraper --query "video editor" --query "youtube editor" --pages 1 --out custom.json
Ignores your niche file and just searches for these specific keywords right now.