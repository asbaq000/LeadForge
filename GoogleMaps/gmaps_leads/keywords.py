"""Expand a niche into several Google Maps search phrasings.

Maps matches on business category, and any one phrasing surfaces only part of a
category. Running "dentist", "dental clinic" and "orthodontist" over the same
tile returns overlapping but meaningfully different sets, which is the other
half of how we get from ~120 results to 1,000.
"""

from __future__ import annotations

# Curated expansions for niches people actually prospect. Anything not listed
# falls back to generic modifiers, which still works, just less thoroughly.
NICHE_VARIANTS: dict[str, list[str]] = {
    "dentist": [
        "dentist", "dental clinic", "dental care", "orthodontist",
        "dental surgeon", "teeth whitening", "dental implants",
    ],
    "gym": [
        "gym", "fitness center", "fitness studio", "crossfit box",
        "personal trainer", "yoga studio", "pilates studio",
    ],
    "restaurant": [
        "restaurant", "cafe", "diner", "bistro", "fast food restaurant",
        "family restaurant", "takeaway",
    ],
    "salon": [
        "beauty salon", "hair salon", "barber shop", "nail salon",
        "spa", "beauty parlour", "makeup artist",
    ],
    "plumber": [
        "plumber", "plumbing service", "drainage service",
        "emergency plumber", "bathroom fitter",
    ],
    "electrician": [
        "electrician", "electrical contractor", "electrical repair",
        "rewiring service",
    ],
    "lawyer": [
        "lawyer", "law firm", "attorney", "legal services",
        "solicitor", "advocate",
    ],
    "doctor": [
        "doctor", "medical clinic", "general practitioner",
        "family physician", "health clinic", "polyclinic",
    ],
    "arena": [
        "arena", "sports complex", "stadium", "indoor sports arena",
        "cricket ground", "football ground", "sports club",
    ],
    "mechanic": [
        "auto repair shop", "car mechanic", "car service center",
        "tyre shop", "car wash", "auto workshop",
    ],
    "real estate": [
        "real estate agency", "property dealer", "estate agent",
        "real estate consultant",
    ],
    "photographer": [
        "photographer", "photo studio", "wedding photographer",
        "photography service",
    ],
    "veterinarian": [
        "veterinarian", "vet clinic", "animal hospital", "pet clinic",
    ],
    "school": [
        "school", "academy", "tuition center", "coaching center",
        "montessori", "learning center",
    ],
    "hotel": [
        "hotel", "guest house", "motel", "lodge", "boutique hotel",
    ],
    "pharmacy": [
        "pharmacy", "chemist", "medical store", "drug store",
    ],
    "bakery": [
        "bakery", "cake shop", "pastry shop", "confectionery",
    ],
    "contractor": [
        "contractor", "construction company", "builder",
        "renovation service", "interior designer",
    ],
}

_GENERIC_MODIFIERS = ["{n}", "{n} service", "{n} shop", "{n} center", "{n} company"]


def expand(niche: str, extra: list[str] | None = None, generic: bool = True) -> list[str]:
    """Return the list of search phrasings to run for a niche."""
    key = niche.strip().lower()
    variants: list[str] = []

    if key in NICHE_VARIANTS:
        variants.extend(NICHE_VARIANTS[key])
    else:
        # Try a loose match so "dentists" and "gyms" still hit the curated list.
        singular = key[:-1] if key.endswith("s") else key
        if singular in NICHE_VARIANTS:
            variants.extend(NICHE_VARIANTS[singular])
        else:
            for hit in NICHE_VARIANTS:
                if hit in key or key in hit:
                    variants.extend(NICHE_VARIANTS[hit])
                    break

    if not variants:
        base = niche.strip()
        variants = [m.format(n=base) for m in _GENERIC_MODIFIERS] if generic else [base]

    if niche.strip() not in variants:
        variants.insert(0, niche.strip())
    if extra:
        variants.extend(e.strip() for e in extra if e.strip())

    # Preserve order, drop duplicates.
    seen: set[str] = set()
    ordered = []
    for v in variants:
        low = v.lower()
        if low not in seen:
            seen.add(low)
            ordered.append(v)
    return ordered
