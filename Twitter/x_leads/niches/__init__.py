"""Loading a niche preset — the phrase list a run searches for."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

NICHE_DIR = Path(__file__).parent
DEFAULT_NICHE = "video_editing"


@dataclass(slots=True)
class Niche:
    name: str
    description: str = ""
    phrases: list[str] = field(default_factory=list)
    language: str | None = "en"
    extra_operators: tuple[str, ...] = ()

    @property
    def path_hint(self) -> str:
        return str(NICHE_DIR / f"{self.name}.json")


def available() -> list[str]:
    return sorted(p.stem for p in NICHE_DIR.glob("*.json"))


def load_niche(name_or_path: str = DEFAULT_NICHE) -> Niche:
    """A built-in preset by name, or any JSON file by path.

    Keys beginning with `_comment` are ignored, which is what lets the shipped
    presets document themselves inline — JSON has no comment syntax and the
    reasoning behind a phrase list is worth keeping next to it.
    """
    path = Path(name_or_path)
    if not path.suffix:
        path = NICHE_DIR / f"{name_or_path}.json"

    if not path.exists():
        raise FileNotFoundError(
            f"No niche called '{name_or_path}'. Built-in presets: "
            f"{', '.join(available())}. You can also pass a path to a JSON file."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    phrases = [p for p in data.get("phrases", []) if isinstance(p, str) and p.strip()]
    if not phrases:
        raise ValueError(f"Niche '{path}' has no phrases in it.")

    return Niche(
        name=data.get("name") or path.stem,
        description=data.get("description", ""),
        phrases=phrases,
        language=data.get("language", "en"),
        extra_operators=tuple(data.get("extra_operators") or ()),
    )
