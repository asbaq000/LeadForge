# Reddit

Search communities with editable subreddits, queries, and scoring vocabulary.

## Setup

Open a terminal in this folder. Use a separate Python virtual environment for this scraper so dependencies do not collide:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
# Uses Python standard library; no pip dependencies.
```

Where Playwright is used, install its browser with `python -m playwright install chromium`. Configure API keys using environment variables or the local `.env` file and supply your own Google credentials where required. Saved credentials and browser profiles are ignored by Git.

## Run

```sh
python -m reddit_leads
```

From the repository root, run `npm start`, open `http://127.0.0.1:3000`, and choose **Reddit**. The dashboard runs this command with this folder as its working directory. Additional arguments accept a JSON array, such as `["--help"]` for CLI-based scrapers. LinkedIn and Facebook use their existing script interfaces; use the configuration files below for their settings.

## Configuration

- `reddit_leads/dashboard.config.json`

The dashboard can edit the listed files. Changes apply to the next run. File paths supplied as arguments are relative to this folder. Search scope, output options, and qualification depend on this engine's implemented capabilities; a task description alone does not create new scraping behavior. Some inherited classifiers remain specialized (for example, hiring intent or podcast detection).

## Output and authentication

Results are written by the original engine to its selected output path or Google Sheet. Live stdout/stderr appears in the dashboard. Browser login and challenges require your interaction in the scraper's browser; the dashboard does not provide interactive terminal input. Stop long runs from the activity list.

See [LEGACY_GUIDE.md](LEGACY_GUIDE.md) for the original detailed setup and behavior.

---

<div align="center"><sub>LeadForge · crafted by asbaq000</sub></div>
