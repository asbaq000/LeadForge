"""Niche presets: a keyword list plus the terms that decide what's on-topic.

A niche bundles three things:

- `keywords`     — what gets searched on Upwork (one request per keyword per page)
- `match_terms`  — a job is on-niche if any appears in its title or skills
- `exclude_terms`— dropped when one appears in the title

Presets ship as JSON next to this file. Load one by name (`load_niche("video")`)
or point at your own copy (`load_niche("./my-niche.json")`).
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from upwork_scraper.models.job_models import Job

LOGGER = logging.getLogger(__name__)

NICHE_DIR = Path(__file__).parent


@dataclass
class Niche:
    name: str
    keywords: list[str]
    match_terms: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    description: str = ""

    def is_relevant(self, job: Job) -> bool:
        """True when the job looks like it belongs to this niche.

        Title and skills are searched for a match term; only the title is
        searched for an exclusion, since a real video job can carry an
        unrelated skill tag.
        """
        if not self.match_terms:
            return True

        title = (job.title or "").lower()
        if any(bad in title for bad in self.exclude_terms):
            return False

        haystack = f"{title} {' '.join(job.skills or []).lower()}"
        return any(term in haystack for term in self.match_terms)

    def with_extra_keywords(self, extra: list[str] | None) -> "Niche":
        """Copy of this niche with more search keywords, duplicates removed."""
        if not extra:
            return self

        keywords = list(self.keywords)
        for keyword in extra:
            if keyword and keyword.lower() not in {k.lower() for k in keywords}:
                keywords.append(keyword)

        return Niche(
            name=self.name,
            keywords=keywords,
            match_terms=self.match_terms,
            exclude_terms=self.exclude_terms,
            description=self.description,
        )


def available_niches() -> list[str]:
    return sorted(p.stem for p in NICHE_DIR.glob("*.json"))


def load_niche(name_or_path: str) -> Niche:
    """Load a built-in preset by name, or any JSON file by path."""
    path = NICHE_DIR / f"{name_or_path}.json"
    if not path.exists():
        path = Path(name_or_path)

    if not path.exists():
        raise FileNotFoundError(
            f"No niche '{name_or_path}'. Built-in: {', '.join(available_niches())}. "
            f"Or pass a path to your own JSON file."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("keywords"):
        raise ValueError(f"Niche file {path} has no 'keywords' list")

    return Niche(
        name=data.get("name", path.stem),
        keywords=list(data["keywords"]),
        match_terms=[t.lower() for t in data.get("match_terms", [])],
        exclude_terms=[t.lower() for t in data.get("exclude_terms", [])],
        description=data.get("description", ""),
    )


def read_keywords_file(path: str) -> list[str]:
    """One keyword per line. Blank lines and `#` comments are ignored."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [
        stripped
        for line in lines
        if (stripped := line.split("#", 1)[0].strip())
    ]
