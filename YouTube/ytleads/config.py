"""Config + secret loading."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted lookup: cfg.get('filters.min_subscribers')."""
        node: Any = self._d
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    # -- secrets -----------------------------------------------------------
    @property
    def youtube_api_key(self) -> str:
        key = os.getenv("YOUTUBE_API_KEY", "").strip()
        if not key:
            raise ConfigError(
                "YOUTUBE_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        return key

    @property
    def spreadsheet_id(self) -> str:
        """Accepts either the bare id or a pasted full spreadsheet URL --
        gspread's open_by_key() needs just the id, not the whole link."""
        raw = os.getenv("SPREADSHEET_ID", "").strip()
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", raw)
        return m.group(1) if m else raw

    @property
    def groq_api_key(self) -> str:
        return os.getenv("GROQ_API_KEY", "").strip()

    @property
    def gemini_api_key(self) -> str:
        return os.getenv("GEMINI_API_KEY", "").strip()

    @property
    def openrouter_api_keys(self) -> list[str]:
        """Comma-separated in .env: OPENROUTER_API_KEYS=key1,key2,key3"""
        raw = os.getenv("OPENROUTER_API_KEYS", "").strip()
        if not raw:
            return []
        seen: list[str] = []
        for part in raw.split(","):
            key = part.strip()
            if key and key not in seen:
                seen.append(key)
        return seen

    @property
    def service_account_file(self) -> Path:
        raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json").strip()
        p = Path(raw)
        return p if p.is_absolute() else ROOT / p


def load_config(path: str | Path = "config.yaml") -> Config:
    load_dotenv(ROOT / ".env")
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Config(data)


def load_seeds(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise ConfigError(f"Seeds file not found: {p}")
    seeds: list[str] = []
    seen: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line.lower() not in seen:
            seen.add(line.lower())
            seeds.append(line)
    return seeds
