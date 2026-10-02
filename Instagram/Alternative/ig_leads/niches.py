"""Loading the niche list."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

NICHE_DIR = Path(__file__).parent / "niches"
DEFAULT_NICHE_FILE = "creator_niches"


def to_hashtag(text: str) -> str:
    """'Classic Car Restoration' -> 'classiccarrestoration'."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass(slots=True)
class Niche:
    name: str
    hashtags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NicheSet:
    name: str
    description: str
    niches: list[Niche]

    def pick(self, wanted: list[str] | None) -> list[Niche]:
        """Select niches by name, case- and punctuation-insensitively.

        Matching is loose on purpose: `--niches "wildlife"` should find
        "Wildlife Documentary" without anyone having to type it exactly.
        """
        if not wanted:
            return list(self.niches)

        keys = [to_hashtag(w) for w in wanted]
        out, seen = [], set()
        for niche in self.niches:
            target = to_hashtag(niche.name)
            if any(k in target or target in k for k in keys) and niche.name not in seen:
                seen.add(niche.name)
                out.append(niche)
        return out

    @property
    def hashtags(self) -> list[str]:
        seen, out = set(), []
        for n in self.niches:
            for h in n.hashtags:
                if h not in seen:
                    seen.add(h)
                    out.append(h)
        return out


def available() -> list[str]:
    return sorted(p.stem for p in NICHE_DIR.glob("*.json"))


def load_niches(name_or_path: str = DEFAULT_NICHE_FILE) -> NicheSet:
    path = Path(name_or_path)
    if not path.suffix:
        path = NICHE_DIR / f"{name_or_path}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No niche file '{name_or_path}'. Built in: {', '.join(available())}"
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    niches = []
    for entry in data.get("niches", []):
        if isinstance(entry, str):
            niches.append(Niche(name=entry, hashtags=[to_hashtag(entry)]))
            continue
        name = entry.get("name", "")
        tags = [t for t in entry.get("hashtags", []) if t] or [to_hashtag(name)]
        if name:
            niches.append(Niche(name=name, hashtags=tags))

    if not niches:
        raise ValueError(f"Niche file '{path}' has no niches in it.")

    return NicheSet(
        name=data.get("name", path.stem),
        description=data.get("description", ""),
        niches=niches,
    )
