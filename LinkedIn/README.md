# LinkedIn

Collect hiring posts using your keywords, time window, and qualification rules.

## Setup

Open a terminal in this folder. Use a separate Python virtual environment for this scraper so dependencies do not collide:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

Where Playwright is used, install its browser with `python -m playwright install chromium`. Configure API keys using environment variables or the local `.env` file and supply your own Google credentials where required. Saved credentials and browser profiles are ignored by Git.

## Run

```sh
python linkedin_bot.py
```

From the repository root, run `npm start`, open `http://127.0.0.1:3000`, and choose **LinkedIn**. The dashboard runs this command with this folder as its working directory. Additional arguments accept a JSON array, such as `["--help"]` for CLI-based scrapers. LinkedIn and Facebook use their existing script interfaces; use the configuration files below for their settings.

## Configuration

- `keywords.txt`
- `dashboard.config.json`

The dashboard can edit the listed files. Changes apply to the next run. File paths supplied as arguments are relative to this folder. Search scope, output options, and qualification depend on this engine's implemented capabilities; a task description alone does not create new scraping behavior. Some inherited classifiers remain specialized (for example, hiring intent or podcast detection).

## Output and authentication

Results are written by the original engine to its selected output path or Google Sheet. Live stdout/stderr appears in the dashboard. Browser login and challenges require your interaction in the scraper's browser; the dashboard does not provide interactive terminal input. Stop long runs from the activity list.

---

<div align="center"><sub>LeadForge · crafted by asbaq000</sub></div>
