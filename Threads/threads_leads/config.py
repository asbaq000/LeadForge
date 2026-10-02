"""Configuration loading: config.yaml for targeting, .env for secrets."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: str) -> Path:
    raw = os.getenv(name, default)
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p)


@dataclass
class ScoreGroup:
    """One weighted bucket of regex patterns."""

    name: str
    weight: int
    patterns: list[re.Pattern] = field(default_factory=list)
    raw_patterns: list[str] = field(default_factory=list)


@dataclass
class Settings:
    # --- run behaviour ---
    daily_target: int
    per_run_cap: int
    min_score: int
    max_post_age_hours: int
    scrolls_per_query: int
    author_cooldown_days: int
    enrich_profiles: bool
    enrich_min_score: int

    # --- pacing ---
    min_delay_ms: int
    max_delay_ms: int
    query_pause_ms: tuple[int, int]
    max_queries_per_run: int

    # --- targeting ---
    queries: list[str]
    hashtags: list[str]
    score_groups: list[ScoreGroup]
    topic_tags: dict[str, list[str]]

    # --- safety ---
    max_searches_per_hour: int
    max_searches_per_day: int
    max_profile_visits_per_hour: int
    session_max_minutes: int
    active_hours: tuple[int, int]
    cooldown_hours_on_challenge: list[int]
    strike_reset_days: int
    warmup_feed_scrolls: int
    warmup_probability: float
    interleave_feed_probability: float
    single_instance: bool

    # --- credentials / paths ---
    sheet_id: str
    sheet_tab: str
    service_account_json: Path
    storage_state: Path
    user_data_dir: Path
    headless: bool

    # --- browser identity (must stay stable across runs) ---
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    timezone_id: str = "UTC"
    locale: str = "en-US"

    db_path: Path = ROOT / "data" / "leads.db"
    csv_backup: Path = ROOT / "data" / "leads_backup.csv"
    lock_path: Path = ROOT / "data" / "scraper.lock"


def load_settings(config_path: Path | str | None = None) -> Settings:
    cfg_file = Path(config_path) if config_path else (ROOT / "config.yaml")
    with open(cfg_file, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    run = cfg.get("run", {})
    pacing = cfg.get("pacing", {})
    safety = cfg.get("safety", {})

    groups: list[ScoreGroup] = []
    for name, spec in (cfg.get("scoring") or {}).items():
        raw = list(spec.get("patterns") or [])
        groups.append(
            ScoreGroup(
                name=name,
                weight=int(spec.get("weight", 0)),
                patterns=[re.compile(p, re.IGNORECASE | re.MULTILINE) for p in raw],
                raw_patterns=raw,
            )
        )

    pause = pacing.get("query_pause_ms", [6000, 14000])
    hours = safety.get("active_hours", [7, 23])

    settings = Settings(
        daily_target=int(run.get("daily_target", 70)),
        per_run_cap=int(run.get("per_run_cap", 40)),
        min_score=int(run.get("min_score", 45)),
        max_post_age_hours=int(run.get("max_post_age_hours", 96)),
        scrolls_per_query=int(run.get("scrolls_per_query", 6)),
        author_cooldown_days=int(run.get("author_cooldown_days", 21)),
        enrich_profiles=bool(run.get("enrich_profiles", True)),
        enrich_min_score=int(run.get("enrich_min_score", 60)),
        min_delay_ms=int(pacing.get("min_delay_ms", 1800)),
        max_delay_ms=int(pacing.get("max_delay_ms", 4600)),
        query_pause_ms=(int(pause[0]), int(pause[1])),
        max_queries_per_run=int(pacing.get("max_queries_per_run", 22)),
        queries=list(cfg.get("queries") or []),
        hashtags=list(cfg.get("hashtags") or []),
        score_groups=groups,
        topic_tags={k: [s.lower() for s in v] for k, v in (cfg.get("topic_tags") or {}).items()},
        max_searches_per_hour=int(safety.get("max_searches_per_hour", 12)),
        max_searches_per_day=int(safety.get("max_searches_per_day", 80)),
        max_profile_visits_per_hour=int(safety.get("max_profile_visits_per_hour", 18)),
        session_max_minutes=int(safety.get("session_max_minutes", 18)),
        active_hours=(int(hours[0]), int(hours[1])),
        cooldown_hours_on_challenge=[
            int(h) for h in safety.get("cooldown_hours_on_challenge", [6, 24, 72])
        ],
        strike_reset_days=int(safety.get("strike_reset_days", 7)),
        warmup_feed_scrolls=int(safety.get("warmup_feed_scrolls", 3)),
        warmup_probability=float(safety.get("warmup_probability", 0.85)),
        interleave_feed_probability=float(safety.get("interleave_feed_probability", 0.25)),
        single_instance=bool(safety.get("single_instance", True)),
        sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip(),
        sheet_tab=os.getenv("GOOGLE_SHEET_TAB", "Leads").strip() or "Leads",
        service_account_json=_env_path("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json"),
        storage_state=_env_path("THREADS_STORAGE_STATE", "./auth/threads_state.json"),
        user_data_dir=_env_path("THREADS_PROFILE_DIR", "./auth/profile"),
        headless=_env_bool("HEADLESS", True),
        proxy_server=os.getenv("PROXY_SERVER", "").strip(),
        proxy_username=os.getenv("PROXY_USERNAME", "").strip(),
        proxy_password=os.getenv("PROXY_PASSWORD", "").strip(),
        timezone_id=os.getenv("BROWSER_TIMEZONE", "UTC").strip() or "UTC",
        locale=os.getenv("BROWSER_LOCALE", "en-US").strip() or "en-US",
    )

    # DAILY_TARGET in .env overrides config.yaml when present.
    if os.getenv("DAILY_TARGET"):
        try:
            settings.daily_target = int(os.environ["DAILY_TARGET"])
        except ValueError:
            pass

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.storage_state.parent.mkdir(parents=True, exist_ok=True)
    settings.user_data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def proxy_config(s: Settings) -> dict | None:
    """Playwright proxy dict, or None when no proxy is configured."""
    if not s.proxy_server:
        return None
    cfg: dict = {"server": s.proxy_server}
    if s.proxy_username:
        cfg["username"] = s.proxy_username
        cfg["password"] = s.proxy_password
    return cfg
