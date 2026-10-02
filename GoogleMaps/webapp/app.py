"""The web UI: pick client types, countries and cities, watch leads arrive.

Run it with `python app.py` from the project root, or `python webapp/app.py`.

One run happens at a time, on a worker thread, because it drives a real
browser. The browser thread publishes events; the page subscribes to them over
a server-sent-events stream and also gets a full snapshot on connect, so
reloading the page mid-run picks the display straight back up.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

# Running this file directly has to work, so make the project importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file

from gmaps_leads import keywords
from gmaps_leads.pipeline import RunConfig, Runner
from gmaps_leads.sheets import SheetsError, SheetWriter, sheet_key_from_url
from gmaps_leads.store import LeadStore
from webapp import places

load_dotenv(ROOT / ".env")

app = Flask(__name__)

SETTINGS_PATH = ROOT / "webapp" / "settings.json"
DB_PATH = ROOT / "data" / "leads.db"

# The spreadsheet this tool was set up against. Editable in the UI.
DEFAULT_SHEET = (
    "https://docs.google.com/spreadsheets/d/"
    "1f-rZNqd4Gg-T-3ZZyn3XZVCjlM21wOlipVszxEkp2-o/edit"
)

DEFAULT_SETTINGS = {
    "sheet_url": DEFAULT_SHEET,
    "country": "PK",
    "cities": ["Karachi"],
    "niches": ["dentist"],
    "target": 100,
    "find_email": False,
    "phone_lookup": True,
    "headless": True,
    "push_to_sheets": True,
    "write_csv": True,
    "fresh": False,
    "tile_km": 3.0,
    "max_tiles": 400,
    "email_limit": 25,
    "phone_cap": 250,
    "min_delay": 1.8,
    "max_delay": 4.0,
    "share_with": "",
}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return data


def save_settings(values: dict) -> None:
    data = load_settings()
    data.update({k: v for k, v in values.items() if k in DEFAULT_SETTINGS})
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --- the job -----------------------------------------------------------------

_MAX_LOG = 400
_MAX_LEADS = 250


class Job:
    """One run at a time, plus the live state the page renders."""

    def __init__(self):
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.subscribers: list[queue.Queue] = []
        self.reset()

    def reset(self) -> None:
        self.state = {
            "running": False,
            "cancelled": False,
            "started_at": None,
            "finished_at": None,
            "tasks": [],
            "current": None,
            "progress": {"phase": "", "done": 0, "total": 0, "label": ""},
            "counters": {"found": 0, "target": 0, "phones": 0, "emails": 0,
                         "pushed": 0},
            "sheet": {"url": "", "title": ""},
            "blocked": None,
            "results": [],
            "log": [],
            "leads": [],
        }

    # --- publishing ------------------------------------------------------

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, event: str, **data) -> None:
        payload = {"event": event, "data": data, "at": time.time()}
        with self.lock:
            self._fold(event, data)
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    # A page that stopped reading; drop it rather than block
                    # the scraper.
                    dead.append(q)
            for q in dead:
                self.subscribers.remove(q)

    def _fold(self, event: str, data: dict) -> None:
        """Keep a snapshot good enough to rebuild the page after a reload."""
        st = self.state
        if event == "log":
            st["log"].append(data)
            del st["log"][:-_MAX_LOG]
        elif event == "lead":
            st["leads"].append(data)
            del st["leads"][:-_MAX_LEADS]
        elif event == "run_started":
            st["tasks"] = data.get("tasks", [])
            st["counters"]["target"] = data.get("target", 0)
        elif event == "task_started":
            st["current"] = data
            st["blocked"] = None
            st["progress"] = {"phase": "scrape", "done": 0, "total": 0, "label": ""}
        elif event == "progress":
            st["progress"] = data
            if data.get("found") is not None:
                st["counters"]["found"] = data.get("found", 0)
        elif event == "stats":
            st["counters"]["found"] = data.get("found", 0)
            if data.get("target"):
                st["counters"]["target"] = data["target"]
        elif event == "task_done":
            st["results"] = [
                r for r in st["results"]
                if not (r["niche"] == data["niche"] and r["city"] == data["city"])
            ] + [data]
            totals = st["counters"]
            totals["phones"] = sum(r.get("with_phone", 0) for r in st["results"])
            totals["emails"] = sum(r.get("with_email", 0) for r in st["results"])
            totals["pushed"] = sum(r.get("pushed", 0) for r in st["results"])
        elif event == "sheet":
            st["sheet"] = data
        elif event == "blocked":
            st["blocked"] = data
        elif event == "unblocked":
            st["blocked"] = None
        elif event == "run_done":
            st["running"] = False
            st["cancelled"] = bool(data.get("cancelled"))
            st["finished_at"] = time.time()

    # --- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, config: RunConfig) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("A run is already going.")
            self.reset()
            self.stop_flag.clear()
            self.state["running"] = True
            self.state["started_at"] = time.time()
            self.state["counters"]["target"] = config.target

        def work() -> None:
            runner = Runner(
                config,
                emit=self.publish,
                should_stop=self.stop_flag.is_set,
            )
            try:
                import asyncio

                asyncio.run(runner.run())
            except Exception as exc:  # a crash must still unstick the page
                self.publish("log", level="error", message=f"Run failed: {exc}")
                self.publish("run_done", cancelled=True, results=[])
            finally:
                runner.close()

        self.thread = threading.Thread(target=work, name="lead-run", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_flag.set()
        self.publish("log", level="warn", message="Stopping after this step...")


job = Job()


# --- helpers -----------------------------------------------------------------


def credentials_path() -> Path:
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
    path = Path(raw)
    return path if path.is_absolute() else (ROOT / path)


def service_account_status() -> dict:
    path = credentials_path()
    if not path.exists():
        return {"ready": False, "email": "", "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "ready": True,
            "email": data.get("client_email", ""),
            "path": str(path),
        }
    except Exception as exc:
        return {"ready": False, "email": "", "path": str(path), "error": str(exc)}


def open_store() -> LeadStore:
    return LeadStore(DB_PATH)


# --- routes ------------------------------------------------------------------


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/bootstrap")
def bootstrap():
    """Everything the page needs to draw itself on first load."""
    with open_store() as store:
        summary = store.summary_rows()
        totals = {
            "leads": store.count(),
            "phones": store.count_with_phone(),
            "emails": store.count_with_email(),
        }
    return jsonify(
        {
            "countries": places.countries(),
            "presets": sorted(keywords.NICHE_VARIANTS.keys()),
            "settings": load_settings(),
            "credentials": service_account_status(),
            "summary": summary,
            "totals": totals,
            "state": job.state,
        }
    )


@app.get("/api/state")
def state():
    return jsonify(job.state)


@app.post("/api/settings")
def settings():
    save_settings(request.get_json(force=True) or {})
    return jsonify({"ok": True, "settings": load_settings()})


@app.post("/api/sheet-check")
def sheet_check():
    """Confirm the service account can actually open the spreadsheet."""
    body = request.get_json(force=True) or {}
    key = sheet_key_from_url(body.get("sheet_url", ""))
    creds = service_account_status()
    if not creds["ready"]:
        return jsonify(
            {
                "ok": False,
                "error": (
                    f"No service account key at {creds['path']}. "
                    "Follow 'Connecting Google Sheets' in the README."
                ),
                "credentials": creds,
            }
        )
    if not key:
        return jsonify({"ok": False, "error": "That is not a Google Sheets link.",
                        "credentials": creds})
    try:
        writer = SheetWriter(
            credentials_path=str(credentials_path()), sheet_key=key
        ).connect()
        tabs = writer.tab_names()
        rows = writer.load_index()
        return jsonify(
            {
                "ok": True,
                "title": writer.title,
                "url": writer.url,
                "tabs": tabs,
                "existing": rows,
                "credentials": creds,
            }
        )
    except SheetsError as exc:
        return jsonify({"ok": False, "error": str(exc), "credentials": creds})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "credentials": creds})


@app.post("/api/start")
def start():
    if job.running:
        return jsonify({"ok": False, "error": "A run is already going."}), 409

    body = request.get_json(force=True) or {}
    niches = [n.strip() for n in body.get("niches", []) if n and n.strip()]
    raw_cities = [c.strip() for c in body.get("cities", []) if c and c.strip()]
    country_code = (body.get("country") or "").upper()
    country = places.country_name(country_code) or body.get("country_name", "")

    if not niches:
        return jsonify({"ok": False, "error": "Add at least one client type."}), 400
    if not raw_cities:
        return jsonify({"ok": False, "error": "Add at least one city."}), 400

    # The geocoder wants "City, Country"; do not double it up if the user
    # already typed the country in.
    cities = []
    for city in raw_cities:
        if country and country.lower() not in city.lower():
            cities.append(f"{city}, {country}")
        else:
            cities.append(city)

    def num(key, default, cast=float):
        try:
            return cast(body.get(key, default))
        except (TypeError, ValueError):
            return default

    sheet_key = (
        sheet_key_from_url(body.get("sheet_url", ""))
        or os.getenv("GOOGLE_SHEET_KEY", "")
        or sheet_key_from_url(DEFAULT_SHEET)
    )

    config = RunConfig(
        niches=niches,
        cities=cities,
        country=country,
        target=max(1, int(num("target", 100, int))),
        extra_queries=[q.strip() for q in body.get("extra_queries", []) if q.strip()],
        tile_km=max(0.5, num("tile_km", 3.0)),
        max_tiles=max(1, int(num("max_tiles", 400, int))),
        min_delay=num("min_delay", 1.8),
        max_delay=num("max_delay", 4.0),
        headless=not bool(body.get("show_browser")),
        phone_lookup=bool(body.get("phone_lookup", True)),
        phone_cap=max(0, int(num("phone_cap", 250, int))),
        find_email=bool(body.get("find_email")),
        email_limit=max(0, int(num("email_limit", 25, int))),
        push_to_sheets=bool(body.get("push_to_sheets", True)),
        sheet_key=sheet_key,
        sheet_name=body.get("sheet_name") or "Maps Leads",
        share_with=body.get("share_with", ""),
        credentials_path=str(credentials_path()),
        write_csv=bool(body.get("write_csv", True)),
        out_dir=str(ROOT / "out"),
        db=str(DB_PATH),
        fresh=bool(body.get("fresh")),
    )

    if config.push_to_sheets and not service_account_status()["ready"]:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Google Sheets is not connected yet. Add service_account.json "
                    "(see the README), or switch off 'Save to Google Sheet' to "
                    "get a CSV instead."
                ),
            }
        ), 400

    save_settings(
        {
            "sheet_url": body.get("sheet_url") or DEFAULT_SHEET,
            "country": country_code,
            "cities": raw_cities,
            "niches": niches,
            "target": config.target,
            "find_email": config.find_email,
            "phone_lookup": config.phone_lookup,
            "headless": config.headless,
            "push_to_sheets": config.push_to_sheets,
            "write_csv": config.write_csv,
            "fresh": config.fresh,
            "tile_km": config.tile_km,
            "max_tiles": config.max_tiles,
            "email_limit": config.email_limit,
            "phone_cap": config.phone_cap,
            "min_delay": config.min_delay,
            "max_delay": config.max_delay,
            "share_with": config.share_with,
        }
    )

    job.start(config)
    return jsonify({"ok": True, "tasks": [{"niche": n, "city": c}
                                          for n, c in config.tasks()]})


@app.post("/api/stop")
def stop():
    if not job.running:
        return jsonify({"ok": False, "error": "Nothing is running."}), 400
    job.stop()
    return jsonify({"ok": True})


@app.get("/api/events")
def events():
    """Server-sent events: a snapshot, then everything as it happens."""

    def stream():
        q = job.subscribe()
        try:
            yield _sse("snapshot", job.state)
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(item["event"], item["data"])
        finally:
            job.unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/api/summary")
def summary():
    with open_store() as store:
        return jsonify(
            {
                "rows": store.summary_rows(),
                "totals": {
                    "leads": store.count(),
                    "phones": store.count_with_phone(),
                    "emails": store.count_with_email(),
                },
            }
        )


@app.get("/api/leads")
def leads():
    niche = request.args.get("niche") or None
    city = request.args.get("city") or None
    limit = min(int(request.args.get("limit", 200)), 2000)
    with open_store() as store:
        rows = [lead.to_dict() for lead in store.all_leads(city, niche)][:limit]
    return jsonify({"leads": rows})


@app.get("/api/export")
def export():
    niche = request.args.get("niche") or None
    city = request.args.get("city") or None
    name = "-".join(p for p in [(city or "all").split(",")[0], niche or "leads"] if p)
    out = ROOT / "out" / f"{name.replace(' ', '-')}.csv"
    with open_store() as store:
        store.export_csv(out, city, niche)
    return send_file(out, as_attachment=True, download_name=out.name)


def main() -> None:
    import webbrowser

    port = int(os.getenv("PORT", "5000"))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  Lead Finder UI running at {url}\n  Press Ctrl+C to stop.\n")
    if not os.getenv("LEADFINDER_NO_OPEN"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # threaded so the event stream and the API stay responsive during a run.
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
