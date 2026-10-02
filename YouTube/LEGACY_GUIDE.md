# YouTube Lead Scraper

Finds YouTube channels worth pitching, pulls their public contact info, sorts
them by content type, and writes them into Google Sheets — one master tab plus
a separate table per content type.

A channel becomes a lead only if it clears **all** of these:

| Gate | Default |
|---|---|
| Subscribers | 1,000 – 1,000,000 |
| Uploaded recently | within the last 21 days |
| Uploads *consistently* | median gap between uploads ≤ 21 days |
| Has a video library | ≥ 5 videos, ≥ 4 uploads to measure |
| Reachable | at least one email or social profile |
| Not animation | animated / cartoon / anime channels are dropped |
| Not motion graphics | After Effects, C4D, VFX, mograph channels are dropped |
| Not a duplicate contact | one channel per email address |

Every channel it has ever evaluated — including rejects — is recorded in
`leads.db` and **never looked at again**. That is what keeps repeat runs cheap
and the sheet free of duplicates.

---

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. YouTube API key

Create one at [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials),
enable **YouTube Data API v3**, then put it in `.env`:

```
YOUTUBE_API_KEY=your_key_here
```

> Your old key was hardcoded in `ab.py`. Rotate it — anything that reached that
> file (backups, OneDrive sync, a future `git init`) has a copy.

### 3. Google Sheets access

1. In the same Cloud project, enable the **Google Sheets API**.
2. Create a **Service Account**, then **Keys → Add key → JSON**.
3. Save the JSON as `service_account.json` next to `main.py`.
4. Open the JSON, copy the `client_email` value.
5. Open your spreadsheet → **Share** → paste that address → give it **Editor**.

Step 5 is the one people miss. Without it every write returns 403.

6. Put the spreadsheet id in `.env` — it's the long string in the URL between
   `/d/` and `/edit`:

```
SPREADSHEET_ID=1AbC...xyz
```

### 4. Free LLM chain for real content-type classification

Content type (documentary, vlog, tech review, etc.) is decided by an LLM that
reads the channel, not keyword matching — and it's entirely API-dependent,
with no silent keyword fallback (see [below](#notes-on-classification) for
what happens if every key fails). Add as many free keys as you want; each
becomes its own attempt, tried in order, so one rate-limited or dead key just
falls through to the next:

```
GROQ_API_KEY=...           # https://console.groq.com/keys -- free, no card, ~30 seconds
GEMINI_API_KEY=...         # https://aistudio.google.com/apikey -- free
OPENROUTER_API_KEYS=...    # https://openrouter.ai/settings/keys -- free, comma-separate as many as you have
```

At least one is required for `classify.method: llm` (the default) to do
anything. `OPENROUTER_API_KEYS` accepts multiple free keys on one line —
`key1,key2,key3` — each tried as a separate attempt in the order listed. The
full attempt order across all three providers is `classify.llm_provider_order`
in `config.yaml`.

### 5. Check it works

```bash
python selftest.py
```

Runs 80+ offline checks on the parsers, filters and classifier, including the
provider-chain failover logic (mocked, no real network). No API key, no
network, no quota.

---

## Usage

```bash
python main.py run
```

Full pipeline: discover → filter → classify → extract contacts → write to Sheets.

Other commands:

```bash
python main.py run --max-channels 200      # cap the batch size
python main.py run --seed "fitness coach"  # one keyword instead of seeds.txt
python main.py run --no-sheets             # keep results in the DB only
python main.py run --no-about              # skip About-page scraping (faster)
python main.py export                      # push anything the DB hasn't synced
python main.py reclassify                  # re-run LLM classification on existing leads
python main.py csv                         # dump to exports/leads.csv
python main.py stats                       # what's in the DB, quota used today
python main.py init-sheet --all-tabs       # pre-create every category tab
```

Interrupting with Ctrl-C is safe — progress is committed as it goes.

### Tuning what it looks for

- **`seeds.txt`** — the discovery keywords, one per line. This is the main lever
  on *who* you find. ~90 seeds ship by default across most niches.
- **`config.yaml`** — thresholds, sub range, cadence rules, exclusions, quota.

---

## Quota

The daily allowance is 10,000 units and it is the binding constraint.

| Call | Cost | Returns |
|---|---|---|
| `search.list` | **100** | ≤ 50 candidates |
| `channels.list` | 1 | up to 50 channels |
| `playlistItems.list` | 1 | one channel's recent uploads |
| `channelSections.list` | 1 | featured channels |

The pipeline is ordered cheapest-gate-first so expensive calls are never spent
on channels that were going to fail anyway:

```
search (100u)  →  drop everything already in the DB (0u)
               →  batch channels.list, 50 per unit
               →  subscriber gate (0u)
               →  uploads + cadence gate (1u each)
               →  exclusions + classification (0u)
               →  contacts, incl. About page (0u — plain HTTP)
```

Two extra things keep the cost down:

- **Discovery searches videos, not channels.** A video published in the last 21
  days proves the channel is active before you spend anything verifying it.
- **Snowballing.** Every qualified lead's *featured channels* cost 1 unit and
  tend to be same-niche, same-size creators — by far the cheapest good leads.

Realistic yield on a full daily quota: **roughly 400–900 qualified leads**,
depending on niche. Usage is metered into the DB per call, so a crashed run
never loses track of what it already spent. Quota resets at midnight Pacific.

---

## What it can and cannot get

**Can:** emails written in the channel description (including obfuscated forms
like `name (at) domain (dot) com`), and every external link on the About page —
Instagram, Facebook, X, TikTok, LinkedIn, Discord, Telegram, website, Linktree.

**Cannot:** the address behind the About page's **"View email address"** button.
That is deliberately CAPTCHA-gated and there is no honest way around it. In
practice most creators who want business mail also paste it in their
description, which is what the parser targets.

Expect **40–55% of otherwise-qualified channels to have no reachable contact**.
That is normal. They're recorded as `rejected_no_contact` so they don't get
re-checked. Running with About-page enrichment on (the default) recovers a
large share of them via socials — `--no-about` is faster but finds noticeably
fewer.

---

## How deduplication works

Three independent layers, because losing one shouldn't cause double outreach:

1. **`leads.db`** — every channel id ever seen, with its verdict. Checked before
   any quota is spent.
2. **Email-level** — if two channels publish the same address, only the first
   becomes a lead (`filters.dedupe_by_email`). Creators often run several
   channels off one business inbox.
3. **Sheet-level** — every tab's Channel ID column is read before writing, so a
   row cannot land twice even if the DB is deleted or the sheet is hand-edited.

To deliberately re-check everything, delete `leads.db`.

---

## Project layout

```
main.py              CLI
selftest.py          offline test suite
config.yaml          thresholds and toggles
seeds.txt            discovery keywords
.env                 secrets (gitignored)
leads.db             SQLite: every channel seen + quota ledger
ytleads/
  youtube_api.py     quota-metered API wrapper
  discovery.py       seed search + featured-channel snowballing
  filters.py         subscriber range, upload cadence
  classify.py        keyword classifier: exclusions + optional keyword-only mode
  llm_classify.py    real category via a free-LLM chain (Groq/Gemini/OpenRouter)
  contacts.py        email + social extraction, About-page parsing
  store.py           SQLite persistence
  sheets.py          Google Sheets writer
  pipeline.py        orchestration
```

---

## Notes on classification

Two separate decisions, two separate methods:

- **Exclusion** (animation / motion graphics) is still keyword-based —
  `ytleads/classify.py`, ~32 categories scored across channel title, keywords,
  description, recent video titles, and YouTube's own `topicCategories`. It's
  fast, free, and already validated against live data, so there was no reason
  to touch it.
- **Content type** (documentary, vlog, tech review, etc.) is decided by an LLM
  chain when `classify.method: llm` is set (the default) — `ytleads/llm_classify.py`.
  It reads the same title/description/video-titles the keyword version used,
  but actually understands them instead of counting hits, so a channel that
  mentions "tutorial" in its bio but is really a vlog gets classified correctly.

The LLM is constrained to the same fixed category list the keyword classifier
uses (`classify.all_categories()`), so the sheet's tab-per-category layout
keeps working unchanged — it's a better decision-maker, not a different
taxonomy. Anything genuinely ambiguous still lands in **Other**.

**The chain, and what "entirely API-dependent" means.** `classify.method: llm`
does not fall back to a keyword guess, ever. Instead every configured key is
tried in order — `classify.llm_provider_order` (default `[groq, gemini,
openrouter]`), with every key in `OPENROUTER_API_KEYS` expanding into its own
attempt at the end of that list. On any failure — network error, retries
exhausted, rate limit, unparseable reply — it moves to the next attempt
immediately. Only once literally everything configured has failed does a
channel get **category = `pending_classification`** instead of a real one.

That lead is still saved in full — subscribers, contact info, everything — it
just doesn't have a content type yet, and it's still a usable lead for
outreach today. Nothing is lost and no YouTube quota is wasted. Run:

```bash
python main.py reclassify
```

any time afterward (limits reset every minute/day, or once you've added
another key) and it retries every pending lead. The run summary and
`python main.py stats` both surface how many leads are currently pending, so
this is never silent. If `classify.method: llm` is set but literally no key
is configured anywhere, every channel becomes pending immediately — the
startup log says so plainly rather than letting a whole run pass unclassified
without explanation.

Check the DB's `reason` column (not shown in the sheet/CSV, diagnostic only)
to see exactly which attempt classified a given lead — `groq`, `gemini`,
`openrouter` (or `openrouter#2`, `#3`... if you have multiple keys and an
earlier one failed for that particular channel), or `pending`.

**Rate limiting** is per-provider — `classify.llm_rpm_groq` /
`_gemini` / `_openrouter` in `config.yaml` — since each has a different
free-tier ceiling, and every individual OpenRouter key gets its own pacing so
one busy key doesn't throttle the others. These are guesses at reasonable
defaults; check your actual limits at console.groq.com/settings/limits,
aistudio.google.com, and openrouter.ai/settings/limits and adjust. On a run
classifying ~350 channels with just one working key in the chain, expect
roughly 12–15 extra minutes from this pacing — more keys in the chain mostly
only add latency on the channels where earlier attempts fail, not to every
channel.

**Sheets caveat:** export only appends rows. If `reclassify` changes a
lead's category after it was already pushed to Sheets, `export` afterward adds
a corrected row under the new tab but doesn't remove the stale one from the
old tab — clean that up by hand if it comes up.

To go back to pure keyword classification (no API key needed, no network,
slightly less accurate, never produces a pending lead), set
`classify.method: keyword` in `config.yaml` — this is a deliberate, explicit
mode, not the failure fallback anymore. To change what a category catches,
edit `CATEGORY_RULES` in `ytleads/classify.py` — terms in `strong` carry ~3×
the weight of `weak` ones, and this list is what both the keyword classifier
*and* the LLM's allowed answers are built from.

---

## Before you send anything

The scraper only reads information creators chose to publish for business
contact. Sending to it is still governed by where your recipients live —
CAN-SPAM (US) requires a real postal address and a working opt-out; GDPR/PECR
(UK/EU) are stricter about unsolicited B2B mail. Worth ten minutes of reading
before the first campaign, not after.
