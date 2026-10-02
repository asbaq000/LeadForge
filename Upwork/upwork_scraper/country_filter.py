"""Drop jobs whose poster is in an excluded country.

Five countries are excluded by default — India, Pakistan, Bangladesh, Egypt and
the Philippines. Nothing about the list is special to this module: it is a
default, replaceable per run (`--exclude-country` / `--allow-country`), per
environment (`EXCLUDED_COUNTRIES`), or per caller (`CountryFilter.from_names`).

Where the country comes from
----------------------------
`job.client.country`, filled by the enrichment stage. It is *not* in the search
API: every country-bearing field on `PubJobSearchResult` (`client`, `location`,
`clientCountry`, `buyer`, `upworkHistoryData`) answers a visitor token with

    doesn't have enough oauth2 permissions/scopes to access: [...]

(checked against the live API on 2026-08-17). So a run without
`--enrich-clients` has no country on any job and this filter cannot act — it
passes everything through rather than pretending otherwise.

Matching
--------
Upwork writes the country in whichever form the page used: the rendered card
gives full names ("India"), other markup gives codes ("IND", "NLD"). Aliases
below cover the long name, ISO-3166 alpha-2 and alpha-3, and the official long
forms, so "PH", "PHL", "Philippines" and "Republic of the Philippines" are one
country. Names outside the table are matched on their normalised text, so any
country can be excluded without an entry here.
"""

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

LOGGER = logging.getLogger(__name__)

# The default block list. Names here are canonical keys — see `_ALIASES`.
DEFAULT_EXCLUDED_COUNTRIES: tuple[str, ...] = (
    "india",
    "pakistan",
    "bangladesh",
    "egypt",
    "philippines",
)

# canonical name -> every spelling seen or plausible for it.
_ALIASES: dict[str, tuple[str, ...]] = {
    "india": ("india", "ind", "in", "republic of india", "bharat"),
    "pakistan": (
        "pakistan", "pak", "pk", "islamic republic of pakistan",
    ),
    "bangladesh": (
        "bangladesh", "bgd", "bd", "peoples republic of bangladesh",
    ),
    "egypt": ("egypt", "egy", "eg", "arab republic of egypt"),
    "philippines": (
        "philippines", "phl", "ph", "republic of the philippines",
        "the philippines",
    ),
    # Not excluded by default, but normalised so an --allow-country/
    # --exclude-country argument and a scraped value agree on spelling.
    "united states": (
        "united states", "usa", "us", "united states of america",
        "united states minor outlying islands",
    ),
    "united kingdom": (
        "united kingdom", "gbr", "gb", "uk", "great britain",
    ),
    "united arab emirates": ("united arab emirates", "are", "ae", "uae"),
    "australia": ("australia", "aus", "au"),
    "canada": ("canada", "can", "ca"),
    "germany": ("germany", "deu", "de", "deutschland"),
    "netherlands": ("netherlands", "nld", "nl", "the netherlands", "holland"),
    "nigeria": ("nigeria", "nga", "ng"),
    "kenya": ("kenya", "ken", "ke"),
    "sri lanka": ("sri lanka", "lka", "lk"),
    "nepal": ("nepal", "npl", "np"),
    "indonesia": ("indonesia", "idn", "id"),
    "vietnam": ("vietnam", "vnm", "vn", "viet nam"),
}

# alias -> canonical, built once.
_CANONICAL: dict[str, str] = {
    alias: canonical
    for canonical, aliases in _ALIASES.items()
    for alias in aliases
}

_PUNCTUATION = re.compile(r"[.,'’()]")
_WHITESPACE = re.compile(r"\s+")


def normalize_country(value: str | None) -> str | None:
    """Reduce a country as written to a canonical key, or None if it is empty.

    Unknown countries normalise to their own cleaned text rather than being
    discarded, so `--exclude-country Latvia` works with no table entry.
    """
    if value is None:
        return None

    cleaned = _PUNCTUATION.sub("", str(value)).strip().casefold()
    cleaned = _WHITESPACE.sub(" ", cleaned)
    if not cleaned:
        return None

    return _CANONICAL.get(cleaned, cleaned)


def _normalize_all(names: Iterable[str]) -> set[str]:
    return {n for n in (normalize_country(x) for x in names) if n}


def parse_country_list(raw: str | None) -> list[str] | None:
    """Read a comma-separated country list, e.g. from `EXCLUDED_COUNTRIES`.

    Returns None when unset (meaning "use the default list") and an empty list
    for an explicit "none"/"off"/empty value (meaning "exclude nothing"). The
    two are different: one is silence, the other is an instruction.
    """
    if raw is None:
        return None

    stripped = raw.strip()
    if not stripped or stripped.casefold() in {"none", "off", "false", "0"}:
        return []

    return [part.strip() for part in stripped.split(",") if part.strip()]


@dataclass(frozen=True)
class CountryFilter:
    """Decides whether a job's poster is somewhere you want to work with.

    `drop_unknown` settles the case the filter cannot answer: a job that was
    never enriched, or whose enrichment failed, has no country at all. The
    default keeps those — a lookup that failed is not evidence about where the
    client is, and silently binning jobs over a browser timeout loses work you
    would have wanted.
    """

    excluded: frozenset[str] = frozenset()
    drop_unknown: bool = False

    @classmethod
    def default(cls, drop_unknown: bool = False) -> "CountryFilter":
        return cls(frozenset(DEFAULT_EXCLUDED_COUNTRIES), drop_unknown)

    @classmethod
    def from_names(
        cls, names: Iterable[str], drop_unknown: bool = False
    ) -> "CountryFilter":
        return cls(frozenset(_normalize_all(names)), drop_unknown)

    @classmethod
    def build(
        cls,
        base: Sequence[str] | None = None,
        add: Iterable[str] | None = None,
        remove: Iterable[str] | None = None,
        drop_unknown: bool = False,
    ) -> "CountryFilter":
        """The full resolution order: base list, plus additions, less removals.

        `base` of None means the built-in default; an empty sequence means
        start from nothing.
        """
        names = _normalize_all(
            DEFAULT_EXCLUDED_COUNTRIES if base is None else base
        )
        names |= _normalize_all(add or [])
        names -= _normalize_all(remove or [])
        return cls(frozenset(names), drop_unknown)

    @property
    def is_active(self) -> bool:
        return bool(self.excluded)

    @property
    def names(self) -> list[str]:
        """Excluded countries, title-cased for display."""
        return sorted(name.title() for name in self.excluded)

    def status(self, country: str | None) -> str:
        """`blocked`, `allowed`, or `unknown` when there is no country to judge."""
        normalized = normalize_country(country)
        if normalized is None:
            return "unknown"
        return "blocked" if normalized in self.excluded else "allowed"

    def allows(self, job) -> bool:
        """True when the job should be kept.

        Signature matches `UpworkScraper.scrape(job_filter=...)`, so it can be
        passed straight in — though at scrape time no job has a country yet, so
        it only does real work after enrichment.
        """
        client = getattr(job, "client", None)
        result = self.status(getattr(client, "country", None))
        if result == "unknown":
            return not self.drop_unknown
        return result == "allowed"

    def partition(self, jobs: Sequence) -> tuple[list, list]:
        """Split jobs into (kept, dropped) without reordering either."""
        kept, dropped = [], []
        for job in jobs:
            (kept if self.allows(job) else dropped).append(job)
        return kept, dropped

    def apply(self, jobs: Sequence, log: bool = True) -> list:
        """Keep the allowed jobs, logging what went and why."""
        if not self.is_active:
            return list(jobs)

        kept, dropped = self.partition(jobs)
        if log and dropped:
            counts: dict[str, int] = {}
            for job in dropped:
                country = getattr(getattr(job, "client", None), "country", None)
                label = normalize_country(country) or "unknown"
                counts[label] = counts.get(label, 0) + 1
            breakdown = ", ".join(
                f"{name.title()} {count}" for name, count in sorted(counts.items())
            )
            LOGGER.info(
                "Country filter dropped %d of %d jobs (%s)",
                len(dropped), len(jobs), breakdown,
            )
        return kept

    def describe(self) -> str:
        if not self.is_active:
            return "country filter off"
        unknown = "dropped" if self.drop_unknown else "kept"
        return f"excluding {', '.join(self.names)} (unknown country: {unknown})"
