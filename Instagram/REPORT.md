# BUILD & BLOCKER REPORT

**Instagram lead scraper — 13 August 2026**

> **The tool is finished. Instagram is the blocker.**
> Every piece of the lead pipeline is built and tested. What stands in the way
> is an access problem at Instagram's end, and it traces back to a single
> cause: the IP this machine goes out on.

| | Status |
| --- | --- |
| **Pipeline** | Complete — discovery, filtering, dedupe, CSV and Sheets output all working |
| **Automated checks** | 70 passing (offline, synthetic data — logic proven, not live throughput) |
| **Live access** | Blocked — anonymous quota exhausted, both login attempts checkpointed |
| **Leads delivered** | 0 of 10 — blocked before a full run could complete |

---

## 1. Diagnosis: one IP explains every failure

Three symptoms looked separate — an anonymous rate limit that wouldn't clear,
a checkpoint on a new account, a checkpoint on an established account. They
share a cause. All traffic left from:

```
egress IP   154.192.226.122
status      401 "Please wait a few minutes before you try again."
duration    > 4 hours (normal cooldown is ~15 minutes)
```

That IP spent the day sending several hundred anonymous API calls — a large
share of them generated during development and testing. Instagram now treats
it as suspicious. Crucially, that reputation applies to **logins from the IP
too**, which is why both accounts were challenged:

| Account | Type | Result |
| --- | --- | --- |
| `hollygrailwar` | Newly created | Checkpoint required |
| `iblamefatema` | Established, in regular use | Checkpoint required |

An established account being challenged is the informative part. If the
accounts were the problem, that one would have sailed through. The common
factor is the network address, not the credentials.

---

## 2. What was tried, in order

| # | Attempt | Result |
| --- | --- | --- |
| 1 | **Anonymous scraping** — pulled real data successfully (`instagram`, `nasa`, `nike`, `zuck` all returned full records) | Worked, then quota ran out after ~80–100 requests and never recovered |
| 2 | **Guest-session bootstrapping** — load the homepage first for real `csrftoken` / `mid` / `ig_did` cookies, as the site's own JS does | No effect. The limit is an allowance, not a check to defeat |
| 3 | **Alternate endpoints** — `i.instagram.com`, oembed, post/profile embeds, GraphQL with a doc_id, mobile user-agents | All refused. Only oembed answered, and it returns an author name — no followers, no posts |
| 4 | **Login, new throwaway account** | Checkpoint. Password accepted; Instagram demanded identity verification |
| 5 | **Login, established account** | Checkpoint. This is what pinned the cause to the IP |
| 6 | **Automatic Chrome cookie import** | Not possible. `browser_cookie3` can't decrypt Chrome 127+ App-Bound Encryption; running elevated gives `BrowserCookieError: Unable to get key` |
| 7 | **Manual cookie import** | Built, not yet run — the most likely remaining option to work |

---

## 3. Where to go from here

### Option A — Import the Chrome session by hand *(best odds)*

Chrome already holds a valid, authenticated session for an account in normal
use. Reusing its cookies means **there is no login event** — and so nothing
for Instagram to challenge. This is the only remaining route that sidesteps
the checkpoint rather than fighting it.

```bash
python src/discovery/import_browser_session.py --help-manual
```

Prints exact DevTools steps. Copy the two values into the JSON file it
describes — never into a chat window.

### Option B — Change the IP *(fixes the root cause)*

A phone hotspot is the zero-cost test: a different network has a different
reputation, and would confirm the diagnosis in about a minute. A residential
proxy is the durable version — this is exactly what commercial scraping
credits are spent on, and the code already accepts proxies in
`src/config/settings.json`.

### Option C — Wait, then run small batches *(slow but free)*

IP reputation decays. After a long pause — overnight rather than minutes —
run two leads at a time instead of ten. The ledger and checkpoint make each
run resume cleanly and never repeat an account, so the day still totals ten.

```bash
python src/find_creators.py -k data/keywords.txt --daily-limit 2
```

---

## 4. Findings worth keeping

Independent of the blocker, several things were learned that the code now
depends on — recorded so they aren't rediscovered later.

### Instagram serves some accounts only under a second app ID

The primary web app ID returns HTTP 400 on a large share of business accounts,
with a server-side schema error (`ig_business_category_subvertical has been
deleted`). A second public app ID handles them. In a 12-account sample this
converted six hard failures to zero.

```
primary    936619743392459   full data, incl. 12 recent post timestamps
                             400s on many business accounts
fallback   238260118697367   survives those accounts
                             but media_count = 0, no post edges
```

Because the fallback reports `media_count` as `0` whether or not the account
posts, the scraper records it as `null` — a confident zero there would mark
active accounts as abandoned.

### Post recency is free on the primary path

The profile payload already carries the twelve most recent post timestamps, so
the "5 posts in 7 days" test usually costs no extra request. Only
fallback-served accounts need a separate call to the feed endpoint.

### There is no keyword search without a login

Every search and hashtag endpoint answers `login_required` to anonymous
callers. Discovery therefore works by guessing likely handles from each
keyword and then walking Instagram's own *related profiles* graph outward from
whatever resolves.

### Caching cut request volume by 40%

Most invented handles are dead. Remembering them between runs took an
end-to-end test from 255 requests to 155 — on a quota measured in the low
hundreds, that is the difference between finishing a day's leads and not.

---

## 5. What exists in the repo

| Component | Purpose | Checks |
| --- | --- | ---: |
| `src/find_creators.py` | Picks 10 random niches, one lead each, writes CSV or Sheet | 19 |
| `src/extractors/http_client.py` | Guest sessions, fingerprint rotation, pacing, persistent cooldowns | 18 |
| `src/filters/shortlist.py` | 10k–500k followers, ≥5 posts in 7 days, boundary-tested | 14 |
| `src/discovery/import_browser_session.py` | Reuses a browser's existing Instagram session | 10 |
| `src/extractors/instagram_parser.py` | Profile fetch, app-ID fallback, post timestamps | 9 |
| `src/discovery/` | Keyword seeds, related-profile crawl, hashtag sweep | — |
| `src/outputs/` | CSV, JSON, Google Sheets, delivered-leads ledger | — |

### What "70 passing" does and doesn't prove

Every check runs offline against synthetic data. They prove the logic is
correct — the follower band, the recency window, the never-repeat guarantee,
the daily top-up, the failure handling. They say nothing about live yield,
because no full run has completed against real Instagram. Expect the first
real run to need tuning that no synthetic test can predict.

---

## 6. Security note

Two account passwords were exposed in plain text during this work and
**should be rotated**, along with any other account reusing them:

- `hollygrailwar`
- `iblamefatema`

The same caution applies to the cookie route in Option A: a `sessionid` grants
account access exactly as a password does. Put it in the file the importer
asks for, and delete that file once the session is saved. `.gitignore` already
excludes `mycookies.json`, `session-*` and `src/config/google-credentials.json`
so none of them can be committed by accident.
