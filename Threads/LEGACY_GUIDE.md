# Threads Lead Scraper

Finds people on Threads who are **asking for** AI development, scripting and automation
work — and drops them into a Google Sheet, ready for outreach.

The hard part isn't scraping Threads. It's that the AI/automation niche on Threads is
overwhelmingly people *selling* these services. This tool is built around separating
buyers from sellers: seller-shaped language carries a heavy negative weight, and a post
must show both **domain relevance** and **intent** before it can qualify at all.

---

## What lands in your sheet

Each row is one qualified lead, 25 columns:

| Column | Notes |
|---|---|
| Captured At / Posted At | UTC timestamps |
| Score / Level | 0–100, bucketed HOT (75+) / WARM (60+) / COOL (45+) |
| Author, Name, Followers, Verified | Followers filled by profile enrichment |
| Post Text | Full text of the post |
| Post URL / Profile URL | Direct links |
| Topics | `n8n`, `chatbot`, `scraping`, `api_integr`, … |
| Signals / Matched Terms | *Why* it scored — for auditing the filter |
| Emails / Links | Pulled from post text, bio and link attachments |
| Likes / Replies / Reposts | Engagement |
| Matched Query | Which search surfaced them |
| Suggested DM Opener | Pre-written first message quoting their own ask |
| Status / Owner / Notes | Blank workspace for your team (Status defaults to `New`) |
| Post ID | Dedupe key — don't delete this column |

---

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

```bash
python -m playwright install chromium
```

### 2. Log in to Threads once

```bash
python scripts/login.py
```

A browser window opens. Log in, clear any 2FA, wait for your feed, press ENTER. The
session is cached as a persistent Chrome profile in `auth/profile/` and reused
headlessly from then on. Your password goes into the real Threads page — this script
never sees or stores it.

> **Use a secondary account.** Automated browsing violates Meta's Terms of Service and
> the account doing the scraping carries the risk. Don't point this at the account your
> business depends on. See [Account safety](#account-safety) before your first real run —
> it's the difference between this lasting a year and lasting a week.

### 3. Google Sheets access

1. Go to <https://console.cloud.google.com/> → create (or pick) a project.
2. **APIs & Services → Library** → enable **Google Sheets API**.
3. **APIs & Services → Credentials → Create Credentials → Service Account**. Any name.
4. Open the service account → **Keys → Add Key → Create new key → JSON**. Download it.
5. Save that file in this folder. Keep whatever filename Google gave it and point
   `GOOGLE_SERVICE_ACCOUNT_JSON` at it — the name doesn't matter, the path does.
6. Open the JSON, copy the `client_email` value (looks like
   `something@project-id.iam.gserviceaccount.com`).
7. Create your Google Sheet, click **Share**, paste that address, give it **Editor**.
8. Copy the sheet ID from the URL:
   `docs.google.com/spreadsheets/d/`**`THIS_LONG_PART`**`/edit`

### 4. Configure

Copy `.env.example` to `.env` and fill in:

```
GOOGLE_SERVICE_ACCOUNT_JSON=./service_account.json
GOOGLE_SHEET_ID=<the long ID from step 8>
GOOGLE_SHEET_TAB=Leads

THREADS_PROFILE_DIR=./auth/profile
HEADLESS=true

# Match the account's real location. An account reporting UTC is a mismatch.
BROWSER_TIMEZONE=Asia/Karachi
BROWSER_LOCALE=en-US

# Residential proxy in the account's home city. Blank = your own IP,
# which is the RIGHT choice when you are in the same city as the account.
PROXY_SERVER=
PROXY_USERNAME=
PROXY_PASSWORD=

DAILY_TARGET=70
```

`GOOGLE_SHEET_ID` is the only field that's strictly required to start. The proxy
fields are blank-safe — whether you want them filled depends on where you're running
from, see [Account safety](#account-safety).

### 5. Verify

```bash
python run.py check
```

Every line should say OK before you run a real scrape.

---

## Running it

```bash
python run.py scrape --dry-run --headful
```

Watch the browser work and see what *would* be written, without touching the sheet.
Do this first — it's how you tell whether your queries are hitting.

```bash
python run.py scrape
```

The real thing: collect, score, enrich, append to Google Sheets, mirror to
`data/leads_backup.csv`.

Other commands:

```bash
python run.py scrape --limit 15
```

```bash
python run.py stats
```

```bash
python run.py safety
```

```bash
python run.py score "anyone know how to automate my invoicing?"
```

---

## Hitting 70 leads a day

One giant run looks like a bot and burns your session. Four moderate runs don't.

```bash
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
```

Run that **as Administrator**. It registers a Windows Scheduled Task firing at
08:15, 12:15, 16:15 and 20:15, capped at 20 leads each — 80/day of headroom against a
70/day target. The daily counter lives in SQLite, so once 70 are banked the remaining
runs exit immediately without touching Threads.

Change the schedule:

```bash
powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Times 07:00,11:00,15:00,19:00,22:00 -PerRun 18
```

Remove it:

```bash
powershell -Command "Unregister-ScheduledTask -TaskName ThreadsLeadScraper -Confirm:$false"
```

### If you're coming up short

Volume depends on how much matching conversation actually exists on Threads on a given
day. Levers, in the order worth pulling:

1. **Add queries** — `config.yaml` → `queries`. This is by far the biggest lever. Think
   about how your buyers phrase their problem, not how you'd describe your service.
2. **Widen the age window** — `run.max_post_age_hours: 96` → `168`.
3. **Lower the bar** — `run.min_score: 45` → `38`. Expect more noise; check the
   `Signals` column to see what's slipping in.
4. **Scroll deeper** — `run.scrolls_per_query: 6` → `10`.
5. **More passes per day** — re-run the scheduler script with 5–6 times.
6. **Shorten author cooldown** — `run.author_cooldown_days: 21` → `7`.

Don't raise `pacing.max_queries_per_run` much past ~25. That's the setting most likely
to get the account rate-limited.

---

## Account safety

Read this before your first real run. It is the part that decides whether this
tool lasts a year or a week.

### What the tool does automatically

| Guard | Default | Why |
|---|---|---|
| **Circuit breaker** | 6h → 24h → 72h | On *any* challenge, login bounce or "try again later", it stops and rests. Escalates on repeat strikes. This is the single most important guard — nearly every account loss is a soft limit that got hammered into a hard one. |
| **Search ceiling** | 12/hour, 80/day | A person does not run 200 keyword searches a day. |
| **Profile ceiling** | 18/hour | Profile enrichment is the second-most-flagged behaviour after search. |
| **Session cap** | 18 minutes | Long unbroken sessions are a strong bot signal. Four short visits beat one long one. |
| **Active hours** | 07:00–23:00 | Real accounts sleep. Evaluated in the *account's* timezone (`BROWSER_TIMEZONE`), not the machine's — so this stays correct on a VPS in another region. Runs outside the window exit immediately. |
| **Feed warm-up** | 85% of sessions | Opens the home feed and scrolls before searching, so the session doesn't begin with a burst of queries. |
| **Feed interleave** | 25% between searches | Occasional drift back to the feed, like a person getting distracted. |
| **Single-instance lock** | on | Two processes driving one account concurrently is unmistakable. |
| **Persistent profile** | `auth/profile/` | Stable cookies *and* device fingerprint across runs. A brand-new browser identity every session is itself a tell. |
| **Read-only** | always | The scraper never likes, follows, posts or DMs. Write actions are what trigger action-blocks; it performs none. |
| **Randomised everything** | — | Scroll distances, pauses, overshoot-and-correct, plus a 25-minute random delay on each scheduled run. |

Check the current state any time:

```bash
python run.py safety
```

If the breaker has tripped, **let it sit**. Clearing it early (`python run.py safety --reset`)
and running again is the reliable way to lose the account.

### What only you can do

These four matter more than everything above combined:

1. **Use a burner account.** Not your business account, not one linked to anything
   you care about. This converts "ban risk" into "inconvenience". Nothing else you
   do reduces the *consequence* of a ban.
2. **Warm it up first.** A profile photo, a bio, ~10 posts, follow 30–50 relevant
   accounts, and use it manually on your phone for 7–14 days before pointing this at
   it. Brand-new accounts that immediately run keyword searches are flagged fastest.
3. **Make your egress match the account's home city.** Set `BROWSER_TIMEZONE` to
   that city's zone. Then check where your traffic actually leaves from:
   - **Running from the same city as the account?** You already have the ideal
     setup — a real residential connection in the right place. Leave `PROXY_SERVER`
     blank. A proxy would make this *worse*, not better.
   - **Running from elsewhere, or on a VPS/cloud box?** Add a residential proxy in
     the account's home city via `PROXY_SERVER`. Meta scores datacenter IPs harshly.
     `scripts/login.py` uses the proxy automatically, so log in through it too —
     the account should never see a sudden change of network.
4. **Don't raise the ceilings.** Every default here is set below where trouble starts.
   `max_searches_per_hour` and `max_queries_per_run` are the two most dangerous knobs
   in the file.

### The honest bit about "almost zero"

There is no configuration of browser automation against Meta that reaches near-zero
risk, and anyone who tells you otherwise is selling something. With the hardening
above plus a warmed burner account on a residential proxy, the realistic outcome is
a **long-lived account with an occasional cooldown** — not immunity.

If you genuinely need near-zero risk, the risk has to move off your account entirely:

| Approach | Ban risk to you | Trade-off |
|---|---|---|
| This tool, hardened | Low, non-zero | Free; burner account occasionally rests |
| **Third-party scraping API** (Apify etc.) | ~Zero | ~$30–50/mo; their infra takes the risk |
| **Official Threads API** | Zero | Requires Meta App Review for `threads_keyword_search`; days-to-weeks approval, rate-limited |

`collector.py` is pluggable — the pipeline, scoring, dedupe, enrichment and Sheets
output are all source-agnostic. Swapping in an Apify or official-API collector is a
contained change if you decide the monthly cost is worth removing the risk.

---

## Tuning the filter

Everything lives in `config.yaml` — no code changes needed.

A post's score is the sum of the weights of every group that matches:

| Group | Weight | Meaning |
|---|---|---|
| `intent_high` | +34 | "looking for", "hiring", "willing to pay" |
| `domain` | +26 | AI, automation, n8n, API, Python, scraping… |
| `intent_medium` | +18 | "how do I", "struggling with", "manually" |
| `icp` | +14 | founder, agency, ecommerce, clients |
| `contactable` | +8 | email, Calendly, "DMs open" |
| `seller_negative` | **−45** | "I build…for founders", "link in bio", "my course" |
| `noise_negative` | **−25** | gm/gn, crypto, giveaways, follow-back |

Two hard gates apply on top: a post is rejected outright unless it matches **`domain`**
*and* one of **`intent_high` / `intent_medium`**. That's what stops "looking for a good
plumber" scoring as a lead.

After editing, always re-run:

```bash
python scripts/selftest.py
```

It scores 20 labelled posts (10 real leads, 10 sellers/noise) and tells you exactly
which ones you broke and which config key to fix. Add your own real examples to
`SAMPLES` as you see mistakes in the sheet — that's how the filter gets sharper over time.

To check the plumbing rather than the rules:

```bash
python scripts/selftest_pipeline.py
```

---

## How it works

```
config.yaml ──► queries + hashtags
                     │
                     ▼
   collector.py  Playwright, logged-in Chromium
                 • intercepts /graphql JSON responses   ◄── primary
                 • parses inline <script> JSON blobs    ◄── first paint
                 • scrapes post cards from the DOM      ◄── fallback
                     │
                     ▼
   parser.py     normalise to a flat post record
                     │
                     ▼
   scorer.py     domain gate → intent gate → weighted score
                     │
                     ▼
   store.py      SQLite: seen posts, author cooldown, daily quota
                     │
                     ▼
   enrich.py     emails, links, profile followers/bio, DM opener
                     │
                     ▼
   sheets.py     append to Google Sheets  +  CSV mirror
```

Reading the JSON the page renders *from* — rather than the HTML it renders *to* — is
what keeps this from breaking every time Threads ships a CSS change. The DOM path is
only there for when interception misses.

**De-duplication is three-layered:** post IDs in SQLite, one lead per author per run,
and an author cooldown (default 21 days) so the same person doesn't reappear across
weeks. On startup the scraper also reads the `Post ID` column back out of your sheet, so
even a deleted database won't produce duplicate rows.

---

## Troubleshooting

**`No saved Threads session`** — run `python scripts/login.py`.

**`Threads redirected to login/challenge`** — session expired or the account got
flagged. Re-run the login script. If it keeps happening, the account is rate-limited:
reduce `max_queries_per_run` and the number of daily passes.

**`Spreadsheet not found or not shared`** — you skipped step 7. Share the sheet with the
`client_email` from `service_account.json` as an Editor.

**`no post cards rendered`** — either the search genuinely returned nothing, or Threads
changed its markup. Run with `--headful` to see the page yourself. The GraphQL
interception path is independent of markup, so this alone doesn't mean data loss.

**Zero leads but plenty of posts seen** — your filter is too tight for what those queries
return. Run `python run.py scrape --dry-run` and check the rejection reasons, then lower
`min_score` or broaden `domain`.

**Lots of sellers in the sheet** — copy the offending post text into
`scripts/selftest.py` as a `False` sample, add a matching pattern to `seller_negative`,
and re-run the self-test until it passes.

---

## Files

| Path | Purpose |
|---|---|
| `config.yaml` | Queries, hashtags, scoring rules, quotas — your main dial |
| `.env` | Credentials and paths |
| `run.py` | CLI: `check`, `scrape`, `stats`, `safety`, `score` |
| `threads_leads/collector.py` | Playwright driver + interception |
| `threads_leads/parser.py` | Payload → flat post record |
| `threads_leads/scorer.py` | Qualification rules |
| `threads_leads/enrich.py` | Contacts + DM opener |
| `threads_leads/store.py` | SQLite dedupe / quota / archive |
| `threads_leads/sheets.py` | Google Sheets + CSV output |
| `threads_leads/pipeline.py` | Orchestration |
| `scripts/login.py` | One-time session capture |
| `scripts/selftest.py` | Scoring accuracy check |
| `threads_leads/safety.py` | Rate ceilings, circuit breaker, instance lock |
| `scripts/selftest_pipeline.py` | Plumbing check, no network |
| `scripts/selftest_safety.py` | Account-protection check, no network |
| `scripts/schedule_windows.ps1` | Task Scheduler registration |
| `auth/profile/` | Persistent browser profile - deleting it forces a fresh login |
| `data/leads.db` | State + safety counters (safe to delete; sheet re-seeds leads) |
| `data/leads_backup.csv` | Local mirror of everything exported |

---

## One caveat worth repeating

Threads has no public search API for third parties, so this drives a real logged-in
browser. That's against Meta's ToS, and the risk sits with the account you log in as.
Use a dedicated account, keep the default pacing, and treat a login challenge as a
signal to slow down rather than to push harder.
