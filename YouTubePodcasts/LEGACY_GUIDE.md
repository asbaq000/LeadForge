# YouTube Podcaster Lead Scraper

Finds **podcast channels anywhere in the world**, proves they really are
podcasts, enriches each one into a complete outreach lead, and writes a CSV
you can drop straight into Google Sheets (or pushes to a spreadsheet
directly).

This is the podcaster-focused rebuild of the older niche scraper. The old
version sorted channels into ~30 content niches; niche is no longer the
question. The question is now:

1. **Is this a podcast?** — the gate. Everything that isn't one is rejected.
2. **What kind of podcast?** — genre, format, host, language.
3. **How do I reach them?** — email, socials, guest-booking form, website.
4. **Are they worth pitching?** — episode length, cadence, reach, whether
   they already cut clips.

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

copy .env.example .env        # then fill in YOUTUBE_API_KEY
.venv\Scripts\python.exe selftest.py     # offline sanity check, no quota
.venv\Scripts\python.exe main.py run     # the real thing
```

## One run = 30 fresh podcasters, never a repeat

`discovery.min_qualified_leads` (default **30**) is both a floor and a hard
cap. The moment a run has 30 new podcasters it stops evaluating mid-batch;
candidates still queued are never written to the DB, stay "unknown", and are
picked up by the *next* run rather than being burned.

Each run writes its own timestamped file containing **only that run's
leads**:

```
exports/leads_2026-08-20_1432.csv     <- run 1, 30 rows
exports/leads_2026-08-21_0905.csv     <- run 2, a different 30 rows
```

Nothing repeats, because a channel is evaluated exactly once ever and is
stamped with the run that found it. Verified in `selftest.py` (per-run lead
isolation) and by a three-run simulation: 30 / 30 / 30 leads, zero overlap.

Import a file straight into Google Sheets with **File → Import → Upload**.
It's UTF-8 with BOM, so Arabic / Hindi / CJK show names come through intact.

### Commands

Run these from the project folder. Substitute `python` if you're not using
the bundled venv.

| Command | What it does | Quota |
|---|---|---|
| `.venv\Scripts\python.exe main.py run` | **the main one** — find 30 new podcasters, write a timestamped CSV, push to Sheets | yes |
| `.venv\Scripts\python.exe main.py run --min-leads 50` | same, but ask for 50 this time | yes |
| `.venv\Scripts\python.exe main.py run --no-sheets` | CSV only, skip Google Sheets | yes |
| `.venv\Scripts\python.exe main.py run --seed "true crime podcast"` | one keyword only (repeatable) | yes |
| `.venv\Scripts\python.exe main.py stats` | what's in the DB, today's quota | none |
| `.venv\Scripts\python.exe main.py csv` | re-dump the **last run's** leads | none |
| `.venv\Scripts\python.exe main.py csv --all` | dump **every** lead ever found | none |
| `.venv\Scripts\python.exe main.py export` | push unsynced leads to Sheets | none |
| `.venv\Scripts\python.exe main.py reclassify` | retry LLM profiling on pending leads | none |
| `.venv\Scripts\python.exe main.py init-sheet` | create tabs + headers up front | none |
| `.venv\Scripts\python.exe selftest.py` | offline test suite | none |

Other `run` flags: `--max-channels N`, `--csv-out PATH`, `--seeds FILE`,
`--no-csv`, `--no-about`.

---

## What counts as a podcaster

All four of these qualify, by design:

- **Full-episode video podcasts** — interviews, solo shows, panels.
- **Clips channels** cut from a parent show — often the best lead of the
  four, since whoever runs it already pays to have episodes chopped up.
- **Audio-first shows** that mirror episodes to YouTube.
- **Interview and talk shows that never use the word "podcast"** — these are
  caught on evidence rather than vocabulary.

## How the gate decides (`ytleads/podcast.py`)

Two signals are **decisive on their own**:

- a link to a real podcast platform — Spotify *show*, Apple Podcasts, an RSS
  feed, Buzzsprout/Libsyn/Captivate/Podbean/Acast/... Nobody links these but
  podcasters. (A Spotify *artist* or *track* link is a musician and is
  explicitly not counted.)
- the word **podcast in the channel name**, in any of ~20 languages —
  `podcast`, `подкаст`, `بودكاست`, `पॉडकास्ट`, `ポッドキャスト`, `팟캐스트`,
  `播客`, `พอดแคสต์`, and more.

Everything else accumulates toward `podcast.min_score` (default 5.0):
episode numbering across recent titles (`Ep. 42`, `#128`, `S2E7`), guest
markers (`ft.`, `w/`, `with Dr. …`), long-form median runtime, talk-show
wording, podcast mentions in the bio or keywords, clips-of-a-podcast
branding.

**Accumulated evidence alone is not enough** — it must also include at
least one signal that is actually about the *format*. Episode numbering and
a 20-minute median are format-agnostic: a Let's Play channel posting
"Minecraft survival #11" clears the raw score by itself, and did come out
labelled a podcast during testing before this rule existed. The one
exception is a genuinely long-form numbered series (45+ minute median),
which is what keeps famously unbranded interview shows — whose titles are
just `#412 - Guest Name` — in the list.

Set `podcast.require_platform_or_name: true` for a stricter, smaller list
that only accepts the two decisive signals.

---

## The pipeline

Each stage is cheaper than the one it protects:

```
discover recent videos by keyword      100 units / page (<=50 candidates)
  -> drop everything already in the DB    0 units
  -> channels.list, batched               1 unit / 50 channels
  -> subscriber-range gate                0 units
  -> uploads playlist + cadence gate      1 unit each
  -> videos.list: runtimes + views        1 unit each
  -> PODCAST GATE, pass 1 (description)   0 units
  -> About page, if it could matter       0 units (plain HTTP)
  -> PODCAST GATE, pass 2 (all links)     0 units
  -> LLM profile: genre/format/host/lang  0 YouTube units
  -> contacts + email dedupe              0 units
  -> persist -> CSV + Sheets
```

Two deliberate ordering choices:

- **The gate runs after runtime and About-page enrichment.** The two
  strongest podcast signals — a platform link and a long median runtime —
  only exist once those have run. Obvious non-podcasts are still cut before
  the HTTP hit: anything scoring below `podcast.borderline_score` is
  rejected without fetching its About page.
- **Rejects are written to the DB too.** That is the whole "never revisit"
  mechanism — a channel that failed the gate today costs zero units
  tomorrow.

### Quota budget

A default project gets 10,000 units/day. Search costs 100 per page;
evaluating the ~50 channels it returns costs roughly another 100
(channels + uploads + videos). So budget **~200 units per seed page** —
about 45–50 seed pages a day. `seeds.txt` is ordered by expected yield so
that if quota runs out, what got cut is the long-tail genre seeds rather
than the high-yield generic and non-English ones.

---

## What you get per lead (41 columns)

Ordered so you can write the cold email from the first ten columns alone.

**Who they are** — Podcast/Channel, Host, Subscribers, Genre, Format,
Language, Country

**How to reach them** — Email, Guest/Booking Form, Website, Instagram,
Twitter/X, LinkedIn, TikTok, Facebook

**Where the show lives** — Spotify, Apple Podcasts, Other Platform, RSS
Feed, Membership (Patreon/Substack/…), Discord, Telegram, Other Links

**What the show looks like** — Median Episode (min), Longest Episode (min),
Avg Views/Episode, Shorts Share, Episodes/Month, Last Upload, Days Since
Upload, Median Gap (days), Total Videos, Total Views

**Why we believe it's a podcast** — Podcast Score, Confidence, Podcast
Signals (the actual evidence list, so a surprising lead can be audited)

**Bookkeeping** — Channel ID, Channel URL, Created, Found Via, First Seen

`Shorts Share` is worth a second look: it tells you whether a show already
cuts clips. Blank means unknown, which is a different answer from `0%`.

---

## Genre, format and host: the LLM layer

Keyword matching is bad at genre and hopeless at host names, so a free-tier
LLM chain does that part. It is handed the hard numbers it cannot observe
(median runtime, cadence, platform links) — without them a small model
happily calls a 90-minute weekly interview show a "vlog".

The chain is **Groq → Gemini → every OpenRouter key in turn**, moving on
after any failure. A provider with no key in `.env` is skipped, not an
error. If every attempt fails, the lead is still saved **in full** with
`genre = pending_classification`; run `main.py reclassify` later to finish
those off. It costs zero YouTube quota.

The model may also veto a lead (`is_podcast: false`) — but only against a
low or medium-confidence verdict. A channel linking its own Spotify feed
stays a lead no matter what an 8B model thinks, and the disagreement is
recorded in the reason column.

Set `classify.method: keyword` for a zero-API run. You keep every lead and
every contact detail; you lose host names and get weaker genres.

---

## Configuration

Everything is in `config.yaml`, commented inline. The knobs that matter most:

| Setting | Default | Effect |
|---|---|---|
| `podcast.min_score` | 5.0 | The gate. ~4 = more unbranded shows + more noise; ~7 = tighter, smaller |
| `podcast.require_platform_or_name` | false | Strict mode: only the two decisive signals qualify |
| `podcast.min_median_minutes` | 0 | Set ~15 to keep only full-episode shows (clips channels are exempt) |
| `filters.min/max_subscribers` | 500 – 500,000 | Small and mid shows reply to cold outreach far more than 1M-sub ones |
| `filters.max_days_since_last_upload` | 45 | Podcasts publish weekly/fortnightly; 21 days would cut most of the market |
| `discovery.min_qualified_leads` | 30 | Leads per run — hard cap, not just a floor |
| `discovery.region_code` | `""` | **Blank on purpose** — setting it narrows a deliberately worldwide sweep |
| `sheets.tab_per_genre` | false | One flat list beats 30 near-empty tabs; Genre is just a column |

---

## Going worldwide

`seeds.txt` covers ~20 languages (Spanish, Portuguese, French, German,
Italian, Dutch, Polish, Turkish, Russian, Arabic, Hindi, Urdu, Japanese,
Korean, Chinese, Indonesian, Malay, Vietnamese, Thai, Filipino) plus
region-led seeds. `discovery.region_code` and `relevance_language` are
blank deliberately — filling either one narrows the sweep to one market.

The gate recognises "podcast" in all of those scripts, so a Turkish or
Indonesian show qualifies on exactly the same evidence an English one does.

---

## Files

```
main.py              CLI
config.yaml          every tunable, commented
seeds.txt            discovery keywords, ordered by yield
selftest.py          offline test suite -- no network, no quota
ytleads/
  podcast.py         THE GATE + genre/format taxonomy + episode maths
  llm_classify.py    genre/format/host/language via free LLM chain
  discovery.py       keyword search + featured-channel snowballing
  filters.py         subscriber and cadence gates
  contacts.py        emails, socials, podcast platforms, booking forms
  youtube_api.py     quota-metered API wrapper
  store.py           SQLite: never-revisit memory + canonical lead rows
  sheets.py          column layout (shared with the CSV) + Sheets writer
  config.py          config + secrets loading
```

Run `python selftest.py` after any change to `podcast.py` — the gate has
regression tests for every false positive found so far.

---

## Upgrading an existing database

`store.py` migrates additively: any column the code expects and the file on
disk lacks is added on open. A `leads.db` from the older niche scraper keeps
its entire never-revisit history — old rows simply have blank podcast
columns, and old niche categories live in the retired `category` column
while new leads use `genre`.

---

## Known limits

- **The About-page email button is CAPTCHA-gated.** There is no honest way
  around it. Most creators who want business mail also paste it into their
  description, which is what the parser is tuned for.
- **Keyword genres are weak.** They are a fallback for when the LLM chain is
  off or exhausted, not the main path.
- **Host names come from the bio.** If a show never names its host in text,
  the column is blank — the model is instructed never to guess a name.
- **Sheets export is append-only.** A `reclassify` that changes a genre does
  not move an already-exported row out of its old tab.
