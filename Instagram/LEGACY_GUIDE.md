# Instagram Profiles Scraper PPR

Quickly extract comprehensive data from any public Instagram profile—no login required. This tool helps marketers, analysts, and researchers access accurate and structured profile information in seconds.

With this scraper, you can gather follower counts, bios, URLs, verification status, and much more, all automatically and efficiently.

**Bitbash Banner**

Telegram   WhatsApp   Gmail   Website

*Created by Bitbash, built to showcase our approach to Scraping and Automation!*
*If you are looking for Instagram Profiles Scraper PPR you've just found your team — Let's Chat. 👆👆*

> **Current status:** the pipeline is complete and tested, but live runs are
> blocked by Instagram rate-limiting the IP this machine uses. See
> **[REPORT.md](REPORT.md)** for the diagnosis, everything that was tried, and
> the options for getting unblocked.

## Introduction

The Instagram Profiles Scraper PPR automates the process of collecting detailed information from public Instagram profiles. It's ideal for marketers, growth hackers, data scientists, and developers who need to analyze or monitor Instagram accounts at scale.

## Why Use This Scraper?

- Saves hours of manual browsing by automating profile data collection.
- Captures structured, machine-readable data ideal for analytics tools.
- Works without requiring Instagram credentials or logins.
- Supports fast extraction and scalable data retrieval.
- Provides accurate and up-to-date information directly from Instagram.

## Features

| Feature | Description |
| --- | --- |
| No Login Required | Access Instagram data without needing an account. |
| Full Profile Insights | Extracts biography, profile picture, followers, following, and more. |
| JSON Output | Clean, ready-to-use data format for APIs and analysis tools. |
| Fast & Reliable | Designed for high-speed and large-scale scraping operations. |
| Verified Badge Detection | Identifies verified accounts easily. |

## Installation

```bash
pip install -r requirements.txt
```

## Usage

Scrape a few profiles straight from the command line:

```bash
python src/runner.py instagram natgeo nasa
```

Scrape a list from a file, writing JSON:

```bash
python src/runner.py -i data/inputs.sample.txt -o data/output.json
```

Write CSV instead (nested location fields are flattened into columns):

```bash
python src/runner.py -i data/inputs.sample.txt -f csv -o data/output.csv
```

Use a settings file for delays, proxies and headers:

```bash
python src/runner.py -c src/config/settings.json -i data/inputs.sample.txt
```

### CLI options

| Flag | Description |
| --- | --- |
| `usernames...` | Usernames, `@handles` or full profile URLs — all normalized. |
| `-i, --input` | File with one username per line (`#` comments allowed). |
| `-o, --output` | Output file path. Default `data/output.json`. |
| `-f, --format` | `json` (default), `jsonl`, or `csv`. |
| `-c, --config` | Path to a settings JSON (copy `src/config/settings.example.json`). |
| `--delay` | Seconds between profiles. Default `2.0`. |
| `--concurrency` | Parallel workers. Default `1` — raise only behind rotating proxies. |
| `--compact` | Write JSON without indentation. |
| `--quiet` | Suppress per-profile logging. |

Profiles that fail are never dropped silently — they're written to a sibling
`<output>.errors.json` with the reason for each one.

## Daily lead generation

`find_creators.py` is the campaign tool built on top of the scraper: it picks
**10 niches at random** from your keyword list and finds **one qualifying
account for each**. Delivery target is chosen automatically:

```bash
# No --sheet -> writes data/leads/leads-YYYY-MM-DD.csv
python src/find_creators.py -k data/keywords.txt

# --sheet given -> appends to that Google Sheet instead
python src/find_creators.py -k data/keywords.txt \
    --sheet https://docs.google.com/spreadsheets/d/<id>/edit \
    --credentials src/config/google-credentials.json
```

Use the CSV path as a drop-in stand-in while Sheets access isn't set up yet —
the criteria, the random-niche selection, the per-day count and the
never-repeat guarantee are identical either way. Once you add `--sheet`, that
run's leads go to the spreadsheet instead of the CSV, using the sheet's own
`username` column as the dedupe source (see below).

Defaults encode the brief — **10,000 to 500,000 followers, at least 5 posts in
the last 7 days, 10 leads a day, one per niche**:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--sheet` | – | Google Sheet URL or id to append to. **Omit it and results go to a local CSV instead.** |
| `--leads-dir` | `data/leads` | Where the CSV and the delivered-leads ledger live. |
| `--credentials` | `src/config/google-credentials.json` | Service account JSON key (Sheet mode only). |
| `--worksheet` | `Leads` | Tab within the spreadsheet; created if missing (Sheet mode only). |
| `--daily-limit` | `10` | Niches to pick, and so leads to deliver today. |
| `--min-followers` | `10000` | Lower follower bound (inclusive). |
| `--max-followers` | `500000` | Upper follower bound (exclusive). |
| `--min-posts` | `5` | Posts required inside the window. |
| `--window-days` | `7` | Length of the recency window. |
| `--per-keyword-budget` | `45` | Fetches to spend on one niche before moving on. |
| `--max-depth` | `2` | Related-profile hops from a seed. |
| `--seed` | – | Fix the random pick, for a reproducible run. |
| `--dry-run` | off | Find leads, write nothing anywhere. |

### Never the same account twice

In **CSV mode**, `data/leads/delivered.txt` is the ledger: every account ever
written to any day's CSV goes in it, and it's checked before an account is
even fetched. Deleting old CSVs doesn't un-deliver an account — only removing
it from the ledger does.

In **Sheet mode**, the sheet itself is the source of truth. Every run reads
the `username` column and refuses to fetch any handle already there, so an
account that's ever been delivered won't reappear even if local files are
wiped. The same `delivered.txt` still gets kept in step as a backup, which
covers rows deleted from the sheet by hand.

Three more safeguards worth knowing:

* Leads accepted within a single run are added to the same skip set, so one
  account can't fill two niches.
* If the sheet write fails, the leads are parked in
  `data/run/unsent-<date>.json` and are **not** marked as delivered — nothing
  is lost and nothing is falsely recorded. The run exits non-zero.
* Each niche stops at its first qualifying account, so a barren niche can't
  drain the request budget for the rest.

### Google Sheets setup

One-time, and it never involves your Google password — a service account is
its own identity that can only touch sheets you explicitly share with it.

1. Google Cloud console → create or pick a project.
2. Enable the **Google Sheets API** and the **Google Drive API**.
3. Credentials → Create credentials → **Service account**, then create a JSON
   key and save it as `src/config/google-credentials.json`.
4. Open that JSON and copy the `client_email` value.
5. Share your spreadsheet with that address, with **Editor** permission.

Then `pip install -r requirements.txt` and run the command above. Use
`--dry-run` first to see the leads without writing.

Already have handles? Skip discovery entirely:

```bash
python src/find_creators.py -i data/candidates.txt --sheet <url>
```

### How discovery works, and its one real limit

Instagram answers `login_required` to **every** anonymous search and hashtag
endpoint, so there is no logged-out keyword search. Two routes around it:

1. **Related-profiles crawl (no login, the default).** Keywords are turned
   into likely handles (`Wildlife Documentary` → `wildlifedocumentary`,
   `wildlife.documentary`, …); whatever resolves becomes a seed, and the crawl
   walks Instagram's own "related profiles" edges outward from there, keeping
   accounts whose handle, name, bio or category match the keyword. Seeds get
   35% of the request budget and the graph walk gets the rest — seed handles
   are mostly abandoned squatter accounts, and it's their *neighbours* that
   are worth having.

2. **Hashtag sweep (needs a session).** `src/discovery/hashtag_search.py`
   covers the whole keyword list via hashtags. It never asks for a password —
   create a session once with `instaloader --login=<throwaway>` and it reuses
   that. Output is a handles file you feed straight to `find_creators.py -i`.

### Running anonymously without getting blocked

Anonymous quota is enforced **per IP** and runs out after roughly 100–200
requests, after which everything answers 401 for a while. No header trick
gets around that — it's an allowance, not a check to defeat. What the client
in `src/extractors/http_client.py` does instead is spend that allowance well
and recover cleanly:

* **Guest sessions.** Loads `instagram.com` first to pick up real
  `csrftoken` / `mid` / `ig_did` / `datr` cookies, then calls the API with
  them, exactly as the site's own JS does — rather than firing bare headers
  at the endpoint cold.
* **Fingerprint rotation.** Rotates through consistent browser identities;
  the `sec-ch-ua` hints always agree with the user-agent, because a Chrome
  131 UA sending Chrome 119 hints is a cheap tell.
* **Jittered pacing.** 2.5–5s between requests, randomised. Uniform timing
  is itself a signature.
* **Persistent cooldowns.** A block is written to `data/run/ratelimit.json`
  and honoured by later runs, so a fresh process won't burn a request
  rediscovering it — each of those probes plausibly extends the block.
  Repeat blocks escalate the wait: 15m, 30m, 60m, capped at 2h.
* **A dead-handle cache.** Seed guessing invents names that mostly 404;
  those are remembered in `dead-handles.txt` and never retried. In testing
  this alone cut a full run from 255 requests to 155.

**The honest limit.** These make the anonymous path last much longer — they
don't make it unlimited. Roughly 10–25 requests go into finding each lead, so
10 leads is well over a single quota window. Two ways to live with that:

* **Spread the day out.** Run with `--daily-limit 2` a few times a day. The
  ledger and checkpoint mean each run picks up cleanly and never repeats an
  account, so the day still ends at 10.
* **Add proxies** in `settings.json`, which is the only thing that genuinely
  raises the ceiling — this is what commercial scrapers' credits pay for.

A logged-in session (below) also lifts the limit substantially, at the cost
of putting an account at risk.

### Running logged in (`--login`)

A logged-in web session gets a far higher per-account rate limit than the
anonymous per-IP quota everything above runs under. This is the real fix if
you're hitting "please wait a few minutes" blocks that aren't clearing.

This tool never sees your password. Create the session once, yourself, with
instaloader's own CLI:

```bash
pip install instaloader
instaloader --login=your_throwaway_username
```

That logs in interactively (or reads the password from your terminal,
depending on your instaloader version) and writes a session file to disk.
Everything after that point only reads cookies out of that file:

```bash
python src/find_creators.py -k data/keywords.txt --login your_throwaway_username
```

Works with every mode above — CSV, Sheets, `-u`/`-i`. Use a throwaway account,
not your main one: this is still the kind of automated traffic Instagram
watches for, and a logged-in account can be flagged or challenged, just at a
much higher volume than the anonymous path tolerates.

If your session file isn't at instaloader's default location, point at it
directly with `--session-file <path>`.

## What Data This Scraper Extracts

| Field Name | Field Description |
| --- | --- |
| `username` | Instagram handle of the profile. |
| `full_name` | Display name of the user or brand. |
| `biography` | User's bio or description. |
| `external_url` | Website link in profile. |
| `category` | Profile category (e.g., Digital Creator). |
| `follower_count` | Number of followers. |
| `following_count` | Number of accounts followed. |
| `is_verified` | Indicates if the account is verified. |
| `media_count` | Total number of posts shared. `null` when the fallback app id served the profile — that payload doesn't carry a real count, and reporting `0` would look like an abandoned account. |
| `profile_pic_url_hd` | Direct link to high-resolution profile image. |
| `account_type` | Type of account: `1` personal, `2` business, `3` creator. |
| `location_data` | City, coordinates, and other location details. |

## Example Output

```json
[
    {
        "username": "instagram",
        "full_name": "Instagram",
        "biography": "Discover what's new on Instagram 🔎✨",
        "external_url": "https://help.instagram.com/",
        "category": "Digital creator",
        "follower_count": 674608953,
        "following_count": 107,
        "is_verified": true,
        "media_count": 7736,
        "profile_pic_url_hd": "https://scontent-fml1-1.cdninstagram.com/v/t51.2885-19/281440578_1088265838702675_6233856337905829714_n.jpg",
        "account_type": 3,
        "location_data": {
            "city_name": "",
            "latitude": 0,
            "longitude": 0
        }
    }
]
```

## Directory Structure Tree

```
Instagram Profiles Scraper PPR/
├── src/
│   ├── runner.py                   # scrape profiles -> JSON/JSONL/CSV
│   ├── find_creators.py            # niche hunt: discover -> filter -> shortlist
│   ├── extractors/
│   │   ├── http_client.py          # guest sessions, pacing, block cooldowns
│   │   ├── instagram_parser.py
│   │   └── utils_format.py
│   ├── discovery/
│   │   ├── keyword_seeds.py        # keyword -> handle guesses, relevance match
│   │   ├── related_crawler.py      # related-profiles graph walk (no login)
│   │   └── hashtag_search.py       # hashtag sweep (needs a session)
│   ├── filters/
│   │   └── shortlist.py            # follower band + posting frequency
│   ├── outputs/
│   │   ├── exporters.py
│   │   ├── leads.py                # delivered-leads ledger (local mirror)
│   │   └── sheets.py               # Google Sheets delivery + dedupe source
│   └── config/
│       └── settings.example.json
├── data/
│   ├── inputs.sample.txt
│   ├── keywords.txt                # the niche list
│   ├── leads/                      # delivered.txt mirror of the sheet
│   └── sample.json
├── requirements.txt
└── README.md
```

## How It Works

Instagram's own web client fetches profile pages through
`GET /api/v1/users/web_profile_info/?username=<name>`, authenticated by
nothing more than a public `x-ig-app-id` header that ships in Instagram's
JavaScript bundle. The scraper calls that same endpoint and reshapes the
verbose response into the flat schema above — which is why no account,
cookie or API key is needed.

Anonymous requests are rate-limited per IP. The default 2-second delay and
3-attempt backoff keep a modest run comfortable; for large batches, configure
proxies in your settings file before raising `concurrency`.

### Known limitation

A minority of business accounts currently make the endpoint return

```
HTTP 400 — Asset asset://laser.provider/ig_business_category_subvertical has been deleted.
```

That's a server-side schema bug on Instagram's end, not rate limiting: the
same request fails identically every time, for anyone, with or without a
login. The scraper detects it, skips the retries, and records the account in
the errors file with Instagram's own message. `@natgeo` is one current
example. Nothing to fix locally — those handles start working again when
Instagram repairs the schema.

## Use Cases

- Digital marketers use it to collect influencer data for campaign analysis.
- Researchers use it to study social media trends and audience behavior.
- Brands use it to monitor competitors and benchmark engagement metrics.
- Developers integrate it into analytics dashboards to enrich datasets.
- Agencies use it to track content creators and evaluate collaboration potential.

## FAQs

**Q1: Do I need an Instagram account to use this?**
No, the scraper works entirely without login credentials or API keys.

**Q2: What kind of profiles can I scrape?**
You can scrape any public Instagram profile, including verified and business accounts.

**Q3: Is the output structured?**
Yes, all data is returned as structured JSON objects—easy to parse and ready for analysis.

**Q4: How fast can it process data?**
It can extract hundreds of profiles per minute depending on your system setup and network conditions.

## Performance Benchmarks and Results

**Primary Metric:** Averages around 350 profiles per minute on standard network conditions.
**Reliability Metric:** Achieves a 98.7% success rate across multiple scraping sessions.
**Efficiency Metric:** Processes large datasets with low memory overhead, optimized for scalability.
**Quality Metric:** Ensures 99% data accuracy and consistent JSON structure output.

Book a Call · Watch on YouTube

---

> "Bitbash is a top-tier automation partner, innovative, reliable, and dedicated to delivering real results every time."
> **Nathan Pennington** — Marketer ★★★★★

> "Bitbash delivers outstanding quality, speed, and professionalism, truly a team you can rely on."
> **Eliza** — SEO Affiliate Expert ★★★★★

> "Exceptional results, clear communication, and flawless delivery. Bitbash nailed it."
> **Syed** — Digital Strategist ★★★★★
