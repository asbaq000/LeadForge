# Facebook Group Post URL Collector

Collects post URLs plus light content parsing (topic, niche/hashtags, budget)
from a Facebook Group you already belong to, and saves them to a Google Sheet
as: `Topic | Niche | URL | Budget | Number | Post Date | Has Contact Info`.

`Topic`, `Niche`, and `Budget` are parsed from a post's OWN visible text
(public content the poster wrote) -- never from a separate profile, comment,
or contact field. `Number` is just a sequential row count, not a phone number.
`Has Contact Info` is a Yes/No flag: it tells you whether the poster included
a phone/WhatsApp number in their own post, WITHOUT ever capturing or storing
the number itself -- see `has_contact_info()` in the script.

## What this tool does NOT do

- It does **not** log into Facebook for you.
- It does **not** ask for, accept, or store your Facebook email or password.
- It does **not** create, read, or store Facebook authentication cookies.
- It does **not** bypass login, CAPTCHA, 2FA, or any other Facebook security check.
- It does **not** collect names, profile URLs, emails, phone numbers, or comments.
  As a safeguard, any digit run of 7+ digits (phone-number shaped) found
  inside a post's own text is redacted before `Topic`/`Budget` are saved, in
  case a poster included a phone number in their post text.
- It only reads posts that are already loaded in a Facebook Group page **you**
  opened and logged into yourself, in your own Chrome browser.

If a step below sounds like "let the script log in for you" -- it isn't. You
always do the logging in, by hand, in a normal Chrome window.

## 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

You also need Google Chrome installed, and a matching `chromedriver` on your
PATH (or installed via `webdriver-manager` / Chrome's own driver if you
prefer -- Selenium 4.15+ can also auto-resolve the driver in most setups).

## 2. Create a Google service account

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create (or select) a project.
3. Enable the **Google Sheets API** and **Google Drive API**.
4. Go to **IAM & Admin > Service Accounts** and create a new service account.
5. Create a **JSON key** for that service account and download it.
6. Rename the downloaded file to `service_account.json` and place it in this
   project folder (next to `fb_group_url_collector.py`).

`service_account.json` contains credentials for a Google API identity, not
for Facebook -- it never touches your Facebook account.

## 3. Share the Google Sheet with the service account

1. Open the JSON key file and copy the `client_email` value
   (looks like `something@your-project.iam.gserviceaccount.com`).
2. Create a Google Sheet named exactly `Lead Scraper` (or whatever you set
   `SHEET_NAME` to -- see below).
3. Click **Share** on that sheet and share it with the service account's
   email address, with **Editor** access.

## 4. Configure `SHEET_NAME` and `TAB_NAME`

Both are constants near the top of `fb_group_url_collector.py`:

```python
SHEET_NAME = "Lead Scraper"
TAB_NAME = "Facebook URLs"
```

If `TAB_NAME` doesn't exist yet inside the sheet, the script creates it
automatically and adds the header row (`Topic`, `Niche`, `URL`, `Budget`, `Number`).

## 5. Log into Facebook manually

Start Chrome yourself with remote debugging enabled, using a real profile
directory (so your normal Facebook session is available):

**Windows (PowerShell):**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-fb-debug-profile"
```

Then, in that Chrome window, log into Facebook the normal way -- type your
email/password yourself, complete any 2FA/CAPTCHA Facebook asks for. The
script never sees any of this.

> The first time, using a fresh `--user-data-dir` means you'll need to log
> in again in that profile. After that, Chrome remembers the session for
> that profile, same as any normal browser profile.

## 6. Open a Facebook Group

In that same Chrome window, navigate to a Facebook Group you are already a
member of (URL contains `/groups/...`). Let the page finish loading its
first batch of posts.

## 7. Start the collector

In a terminal, from this project folder:

```bash
python fb_group_url_collector.py
```

The script will:
1. Attach to your already-open, already-logged-in Chrome tab.
2. Confirm it's looking at a Facebook Group page.
3. Scroll gradually to let more posts load.
4. For each post, click its timestamp to reveal the real permalink URL, then
   return to the feed. This is required because Facebook's current markup
   does not expose the real permalink as a static link -- only as something
   revealed by an actual click (confirmed via live testing). It keeps
   scrolling and clicking through a group's FULL available post history
   within `DAYS_BACK`, not just the first screenful -- it only stops once
   `CONSECUTIVE_OLD_POSTS_TO_STOP` posts in a row are older than that
   window, or `MAX_POSTS_PER_RUN` (a high safety ceiling, not a normal
   stopping point) is hit.
5. Filter, dedupe, and write new rows to your Google Sheet.

Progress and any issues are printed to the console and written to
`fb_urls_scraper.log`.

**Known limitation:** Facebook also renders the visible "2h" / "Just now"
timestamp text as scrambled Unicode that can't be read from the DOM as
plain text (confirmed via live testing) -- this is separate from the
permalink-click fix above. When a post's timestamp can't be read this way,
it is skipped and logged rather than guessed at, per this tool's "never
guess" design. In practice this means fewer posts pass the `DAYS_BACK`
filter than are actually recent; there is currently no reliable passive
way to read that text.

## 8. How the "last N days" filter works

`DAYS_BACK = 2` means: only posts whose displayed timestamp is within the
last 2 days are kept. The script understands common Facebook timestamp
formats such as "5 minutes ago", "2 hrs", "yesterday", "yesterday at 3:45 PM",
and several absolute date formats. If a post's timestamp can't be confidently
parsed, that post is **skipped** rather than guessed at -- it will not appear
in your sheet, and the skip is logged.

## 8b. How the buyer-vs-seller filter works

This tool is meant to surface leads -- people looking to **hire** someone --
not other freelancers advertising their own services. `EXCLUDE_SELLER_POSTS
= True` (near the top of the script) turns on a best-effort keyword
classifier (`classify_post_intent`) over each post's own text:

- Posts that read like "I am a video editor", "available for hire", "DM me
  for editing services" etc. are classified as **seller** and skipped
  (logged as "looked like a service-provider pitch").
- Posts that read like "need a video editor", "looking for a designer",
  "hiring", "who can edit this" etc. are classified as **buyer** and kept.
- Anything ambiguous (matches both, or neither) is classified **unknown**
  and kept -- silently dropping a real lead is worse than including an
  occasional unrelated post. Set `EXCLUDE_SELLER_POSTS = False` to disable
  this filter entirely and keep everything.

This is a keyword heuristic, not true intent understanding -- it can
misclassify unusual phrasing, sarcasm, or posts that mix both roles.

## 8c. How the remote/freelance filter works

On top of the buyer filter, `EXCLUDE_ONSITE_POSTS = True` (near the top of
the script) skips buyer posts that look like an on-site/in-person/full-time
role rather than remote/freelance work (`classify_work_arrangement`):

- Posts mentioning "on-site", "in-office", "full-time position", "walk-in
  interview", "near me", "in-person only" etc. are classified **onsite**
  and skipped (logged as "not remote/freelance").
- Posts mentioning "remote", "freelance", "work from home", "WFH",
  "part-time", "gig", "per-project" etc. are classified **remote_freelance**
  and kept.
- A post that doesn't mention work arrangement at all is classified
  **unknown** and kept -- most posts in a freelance group are implicitly
  remote/freelance already, so this avoids over-filtering. Set
  `EXCLUDE_ONSITE_POSTS = False` to disable this filter and keep everything
  the buyer filter already allows through.

## 9. How deduplication works

- `seen_fb_urls.json` stores every post URL the script has ever written.
- On each run, this file is loaded first; any URL already in it is skipped.
- The script also reads the existing `URL` column in your Google Sheet
  and skips anything already there, as a second safety check.
- `Number` (the row count column) is computed from the sheet's current row
  count at write time, so it keeps counting up across runs rather than
  restarting at 1 each time.
- After a successful run, `seen_fb_urls.json` is updated with the newly
  found URLs.

## 9b. How the daily lead limit works

`MAX_LEADS_PER_RUN = 20` (near the top of the script) caps how many new leads
get saved to Google Sheets per calendar day, tracked in
`daily_lead_quota.json` as `{"date": ..., "count": ...}`:

- Each run checks today's count first. If the limit is already reached, the
  script exits immediately without even opening the browser.
- Otherwise it collects at most the remaining amount (`20 - already used
  today`), stopping extraction immediately mid-page the moment that many
  qualifying leads are found -- it doesn't keep scanning past the cap.
- The count only increases after leads are actually **written** to the
  sheet (not just collected), matching the "20 unique new post URLs...
  saved to Google Sheets" requirement.
- The reset is calendar-date based, not a literal clock trigger inside the
  script: the first run on a new day automatically starts back at 0/20. If
  you want it to actually run automatically every day at 9 AM, schedule
  `python fb_group_url_collector.py` with Windows Task Scheduler (or cron)
  for that time -- the quota logic makes it safe if you also run it again
  later the same day (it'll just top up toward 20, not restart from 0).

## 10. Troubleshooting Google Sheets authentication errors

- **`FileNotFoundError: service_account.json not found`** -- make sure the
  key file is in this folder and named exactly `service_account.json`.
- **`gspread.exceptions.SpreadsheetNotFound`** -- the `SHEET_NAME` must match
  the Google Sheet's title exactly, and the sheet must be shared with the
  service account's `client_email` (Editor access).
- **`PermissionError` / 403 from Google** -- the Sheets API or Drive API is
  probably not enabled on your Google Cloud project, or the sheet wasn't
  shared with the service account.
- **Rows aren't appearing** -- check `fb_urls_scraper.log` for a Google
  Sheets error; if the write failed, your collected URLs are safely saved
  to a local `fb_urls_backup_<timestamp>.json` file instead of being lost.

## Troubleshooting the browser / page detection

- **"Could not attach to Chrome"** -- make sure Chrome was started with
  `--remote-debugging-port=9222` and is still running.
- **"active tab is not on facebook.com" / "not a Facebook Group page"** --
  click into the Chrome window and make sure the *active* tab is a Facebook
  Group URL (`facebook.com/groups/...`) before running the script.
- **"No post containers found" / "selectors may need updating"** -- Facebook
  periodically changes its page markup. This is logged, not guessed around;
  if you see this consistently, the CSS selectors in
  `find_post_containers()` need to be revisited.

## Project files

```
fb_group_url_collector/
├── fb_group_url_collector.py   # main script
├── service_account.json        # you provide this (Google API credentials)
├── seen_fb_urls.json           # dedup store, auto-updated
├── fb_urls_scraper.log         # run log
├── requirements.txt
└── README.md
```
