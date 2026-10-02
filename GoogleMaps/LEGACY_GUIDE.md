# Google Maps Lead Scraper

Finds every business of a given type in a city that **has no website**, recovers
their phone number and (sometimes) email, and writes them to a Google Sheet.
Built for selling website-building services: a business on Maps with no website
is a business that needs one.

```bash
python ui.py
```

That opens a page in your browser where you pick the client types, the country,
the cities and how many leads you want, then watch them arrive. Everything is
also available on the command line:

```bash
python run.py --city "Karachi, Pakistan" --niche dentist --target 1000
```

---

## Read this before you start

Three things about this tool that you should know up front.

**1. Google Maps has no email field.** Not in the listing, not in the API,
nowhere. Phone numbers come straight off the listing and are reliable. Emails
have to be hunted on Facebook pages and directories, and these businesses were
selected *precisely because they have no web presence*.

Measured on a real Karachi dentist run: **50 of 54 leads had a phone (93%);
0 of 4 email lookups returned a usable address**, at roughly 75 seconds each.
The one email that did turn up was `care@oladoc.com` — a *directory's* address,
not the dentist's. The tool now rejects those on purpose rather than padding
the numbers with contacts that reach a listings site instead of your lead.

**Phone is your channel.** Email hunting is on by default because you asked for
it, capped at 50 lookups per run. If you want speed, turn it off:

```bash
python run.py --city "Karachi, Pakistan" --niche dentist --target 1000 --no-email
```

**2. This scrapes Google Maps, which is against Google's Terms of Service.**
You asked for a free tool, and free means no Places API, which means scraping.
Expect CAPTCHAs at volume. The tool detects them, stops, and tells you — it
never tries to solve one. Google also reshuffles their page markup every few
weeks; when that happens the selectors in `gmaps_leads/scraper.py` need a look.

**3. 1,000 leads takes hours, not minutes.** Deliberately. Requests are spaced
out with random delays because hammering Google is what gets you blocked. Start
it and leave it running. It is fully resumable.

---

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

```bash
python -m playwright install chromium
```

### 2. Try it without Google Sheets

Confirm the scrape works before wiring up credentials:

```bash
python run.py --city "Austin, TX, USA" --niche dentist --target 30 --csv-only --no-email
```

You get a CSV in `out/`. If that works, set up Sheets.

### 3. Open the UI

```bash
python ui.py
```

(or double-click `start-ui.bat` on Windows). Your browser opens on the control
panel:

- **Who do you want to find?** — type any client type and press Enter. Add as
  many as you like: dentist, gym, driving school, whatever you sell to. There
  are one-click presets for the common ones. **Each client type gets its own tab
  in your spreadsheet.**
- **Where?** — pick a country, then add cities. Major cities for that country
  are one click away, and you can type any other.
- **How many leads?** — per client type, per city. Three client types across two
  cities at 100 each is six lists and up to 600 leads.
- **Options** — save to Sheets, recover phone numbers, hunt emails, show the
  browser window. Advanced holds the sheet link, grid size and delays.

Press **Find leads** and the right-hand side shows live progress, every lead as
it is found, and a running tally per tab. **Stop** halts after the current step;
nothing collected is lost. Closing or reloading the page does not stop the run.

### 4. Connecting Google Sheets

You need a *service account* — a robot Google account that owns the write
access. About five minutes, once.

1. Go to <https://console.cloud.google.com/> and create a project (or pick one).
2. Enable both APIs:
   - <https://console.cloud.google.com/apis/library/sheets.googleapis.com>
   - <https://console.cloud.google.com/apis/library/drive.googleapis.com>
3. Go to **APIs & Services → Credentials → Create Credentials → Service account**.
   Give it any name, click through to Done.
4. Click the new service account → **Keys** → **Add key** → **Create new key** →
   **JSON**. A file downloads.
5. Save that file into this folder as `service_account.json`.
6. Open it and copy the `client_email` value — it looks like
   `something@your-project.iam.gserviceaccount.com`.
7. **Either** create a Google Sheet yourself and share it with that email as an
   **Editor** (easiest), **or** let the tool create one and pass your own Gmail
   with `--share-with you@gmail.com` so it gets shared back to you.

In the UI, paste your sheet's link under **Advanced → Google Sheet link** and
press **Test the connection**. It tells you straight away whether the service
account can reach it, and how many leads are already in there.

For the command line, copy `.env.example` to `.env` and set the sheet key (the
long id in your sheet's URL):

```
GOOGLE_SERVICE_ACCOUNT_JSON=./service_account.json
GOOGLE_SHEET_KEY=1f-rZNqd4Gg-T-3ZZyn3XZVCjlM21wOlipVszxEkp2-o
```

> A sheet created by a service account is owned by the robot and **invisible in
> your Drive** until it is shared with you. That is what `--share-with` is for.

---

## Usage

```bash
python run.py --city "Karachi, Pakistan" --niche dentist --target 1000
```

```bash
python run.py --city "Dubai, UAE" --niche "beauty salon" --target 500 --no-email
```

```bash
python run.py --city "Manchester, UK" --niche plumber --csv-only
```

Several client types and several cities in one go — this is six lists, landing
on three tabs:

```bash
python run.py --city Karachi,Lahore,Islamabad --country Pakistan               --niche dentist,gym,salon --target 200
```

### What it does, in order

1. **Geocode** the city via OpenStreetMap (free, no key) to get its bounding box.
2. **Tile** the box into a grid, sorted centre-outward so the dense commercial
   core is scraped first.
3. **Search** each tile with several phrasings of your niche.
4. **Filter** to listings with no website link.
5. **Open listings** for leads with no phone yet, and for any whose
   no-website claim the search could not prove.
6. **Hunt emails** via Bing in a real browser.
7. **Write** a CSV and append new rows to your Google Sheet — one tab per
   client type, skipping anything already in there.

### Key options

| Flag | Default | What it does |
|---|---|---|
| `--city` | required | `"Karachi, Pakistan"`, or `Karachi,Lahore` with `--country` |
| `--country` | | Added to any city that does not name one |
| `--niche` | required | `dentist`, or `dentist,gym,salon` — one tab each |
| `--target` | `1000` | Stop once this many leads are found, per list |
| `--tile-km` | `3.0` | Grid tile size. **Lower = more leads, slower** |
| `--queries` | | Extra phrasings, comma-separated |
| `--no-email` | off | Skip email hunting (much faster) |
| `--email-limit` | `150` | Max email lookups per run |
| `--no-headless` | off | Show the browser — **needed to solve a CAPTCHA** |
| `--csv-only` | off | Skip Sheets entirely |
| `--share-with` | | Your Gmail, if letting the tool create the sheet |
| `--sheet-key` | from `.env` | Sheet to write into, by key or full URL |
| `--fresh` | off | Ignore saved progress and re-scrape |

### Why the grid matters

A single Google Maps search stops feeding results at about **120 listings**, no
matter how many businesses exist. That is the ceiling every naive scraper hits.

Two things get past it: searching many **map tiles** (a search centred on
downtown returns different businesses than one centred three km north), and
searching many **phrasings** ("dentist", "dental clinic", "orthodontist" all
surface different subsets). Karachi at `--tile-km 3` is 400 tiles × 8 phrasings
= 3,200 searches available. You will hit 1,000 leads long before exhausting it.

If you come up short, in order of effect:

```bash
--tile-km 2          # finer grid, many more searches
--queries "implant clinic,braces,dental hospital"
--target 1500        # the city may genuinely have fewer than you think
```

---

## What lands in your spreadsheet

One tab per client type, named after it — `Dentist`, `Gym`, `Hair Salon`. The
tab is created the first time that client type produces a lead, with a frozen,
bold header row. Runs only ever append, and never append a business that is
already somewhere in the file.

The `Status` column is left empty on every row. It is yours — mark leads called,
booked, dead, whatever — and nothing ever writes over it.

If you point it at a spreadsheet you already use, nothing else in there is
touched.

### Only confirmed leads get written

Google serves two different result-card layouts. The rich one shows a Website
button, so a card without one is genuinely website-free. The compact one has no
action row at all, and then *every* card looks website-free whether it is or
not — on a real Karachi gym search, 5 of 12 listings that looked clean turned
out to have websites.

So a lead found in a compact-layout search is marked unconfirmed, and its own
listing page is opened to settle it. Anything the listing exposes a website for
is dropped. **Only confirmed leads are written to the sheet**, because a row
once written cannot be taken back — the rest stay in the database and the CSV,
and go up on the next run that checks them.

If a run reports leads held back, either raise **Listings to open / run** under
Advanced (`--phone-lookup-cap`) or leave phone lookup on and run the same list
again.

---

## It is resumable

Every lead is written to `data/leads.db` (SQLite) the moment it is found, along
with which tile+query pairs are already done.

- **Ctrl+C any time.** Nothing is lost.
- **Re-run the same command** and it skips completed tiles and continues.
- **Run it next week** against the same sheet and only genuinely new businesses
  get appended.
- Notes you add by hand in the Google Sheet are never overwritten. Rows are only
  appended.

### How "no repeats" actually works

Three checks, and a lead has to pass all of them:

1. **Place ID**, Google's own id for a listing. Catches the same shop surfacing
   under several search phrasings or in two overlapping tiles.
2. **Phone number**, compared on the last nine digits. Catches a business listed
   twice under different place ids, which happens after it moves or
   re-registers — local and international spellings of one number still match.
3. **The whole spreadsheet**, not just the tab being written. Every tab is read
   before anything is appended, so a business already on your Dentist tab is not
   added again when a later "doctor" run turns it up.

Where a business has no phone on record yet, the fallback key is its name plus
the start of its address.

---

## When Google blocks you

You will see:

```
Google is showing a CAPTCHA / rate-limit page.
```

Progress is already saved. Options, in order of preference:

1. **Wait a few hours** and run it again. Usually enough.
2. **Tick "Show the browser window"** (or `--no-headless`) and solve the CAPTCHA
   in the window that appears. The run carries on by itself once you do; it
   waits up to ten minutes for you.
3. **Slow it down**: raise the min and max delays under Advanced, or
   `--min-delay 4 --max-delay 9`.
4. Use a different network.

If it happens constantly, you are scraping too fast. Raise the delays.

---

## Output columns

`Name`, `Category`, `Phone`, `Status`, `Email`, `Address`, `City`, `Rating`,
`Reviews`, `Maps URL`, `Email Source`, `Search Query`, `Scraped At`, `Place ID`

`Email Source` tells you where an email came from, so you can judge it before
sending. `Place ID` is the dedupe key — don't delete that column.

---

## Layout

```
ui.py                   Starts the web UI (start-ui.bat does the same)
run.py                  Command line front end
gmaps_leads/
  pipeline.py           The run engine both front ends drive
  geo.py                Geocoding and grid tiling
  keywords.py           Niche -> search phrasings
  scraper.py            Playwright Google Maps scraper
  models.py             Lead record and result-card parsing
  enrich.py             Browser-based email hunting
  store.py              SQLite dedupe, resume, CSV export
  sheets.py             Google Sheets writer, one tab per client type
webapp/
  app.py                Flask server, job control, live event stream
  places.py             Countries and their major cities
  templates/, static/   The page itself
data/leads.db           Your accumulated leads (resume state)
out/                    CSV exports
```

### If Google changes their markup

The scrape breaks in an obvious way — zero leads found, or names arriving empty.
Everything fragile is in two constants in `gmaps_leads/scraper.py`:
`_EXTRACT_JS` (the results feed) and `_DETAIL_JS` (a single listing panel). They
deliberately anchor on `a[href*="/maps/place/"]`, `data-item-id` attributes, and
text shape rather than on Google's obfuscated class names, which is why they
survive most redesigns. `parse_card_text()` in `models.py` handles the text
layout of a card.

---

## Adding your own niche

`gmaps_leads/keywords.py` has a `NICHE_VARIANTS` dict. Add an entry:

```python
"car detailing": [
    "car detailing", "auto detailing", "car wash",
    "ceramic coating", "paint protection",
],
```

Anything not in that dict still works — it falls back to generic modifiers — but
a curated list finds noticeably more.
