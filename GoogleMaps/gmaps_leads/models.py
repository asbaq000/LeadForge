"""The Lead record and the shape it takes in the spreadsheet."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone

# Column order used for both the CSV and the Google Sheet header row.
# Phone and Email sit early because those are the columns you actually work from.
# Matches the layout already in use in the spreadsheet, Status column and all,
# so old and new tabs line up and the Place ID column stays where it is. City
# carries the country ("Lahore, Pakistan"), so there is no separate column for
# it. Status is left empty for you to work in.
SHEET_COLUMNS = [
    "Name",
    "Category",
    "Phone",
    "Status",
    "Email",
    "Address",
    "City",
    "Rating",
    "Reviews",
    "Maps URL",
    "Email Source",
    "Search Query",
    "Scraped At",
    "Place ID",
]

# Columns the sheet is deduped on, by 1-based position in SHEET_COLUMNS.
NAME_COL = SHEET_COLUMNS.index("Name") + 1
PHONE_COL = SHEET_COLUMNS.index("Phone") + 1
PLACE_ID_COL = SHEET_COLUMNS.index("Place ID") + 1


@dataclass
class Lead:
    """One business with no website, plus whatever contact info we recovered."""

    place_id: str
    name: str
    category: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    email: str = ""
    email_source: str = ""
    rating: float | None = None
    reviews: int | None = None
    maps_url: str = ""
    city: str = ""
    country: str = ""
    niche: str = ""
    query: str = ""
    lat: float | None = None
    lng: float | None = None
    # True once the listing's own page confirmed it has no website. Feed cards
    # sometimes render without the Website button at all, and then their
    # silence proves nothing.
    verified: bool = False
    scraped_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def to_row(self) -> list:
        """Flatten to a spreadsheet row matching SHEET_COLUMNS."""
        return [
            self.name,
            self.category,
            self.phone,
            "",  # Status: yours to fill in, never written over
            self.email,
            self.address,
            self.city,
            self.rating if self.rating is not None else "",
            self.reviews if self.reviews is not None else "",
            self.maps_url,
            self.email_source,
            self.query,
            self.scraped_at,
            self.place_id,
        ]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Lead":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# --- parsing helpers for the raw text scraped off a result card -------------
#
# Real card text looks like this. The name repeats, the rating sits alone on
# its own line, and the phone number is tacked onto the end of the opening
# hours line:
#
#     Super Dentists The Dental Clinic
#     Super Dentists The Dental Clinic
#     4.8
#     Dental clinic - Bahadur Yar Jang Rd, Soldier Bazaar
#     Open - Closes 9:30 PM - +92 332 3742203
#
# (the separator above is really an interpunct). So we split each line on the
# interpunct as well as on newlines, then classify every resulting segment by
# its shape rather than by its position.

INTERPUNCT = "·"
_NARROW_NBSP = " "
_NBSP = " "

# "4.8" alone, or "4.8(1,234)" in the other layout Google sometimes serves.
_RATING_ONLY_RE = re.compile(r"^([0-5](?:[.,][0-9])?)\s*(?:\(\s*([\d,.\s]+)\s*\))?$")

# "4.9 stars 212 Reviews", read off the star element's aria-label.
_STAR_LABEL_RE = re.compile(
    r"([0-5](?:[.,][0-9])?)\s*stars?(?:\s*([\d,.\s]+)\s*review)?", re.I
)

# A phone segment: starts with + or a digit, then digits and separators only.
_PHONE_RE = re.compile(r"^[+(]?\d[\d\s\-().]{7,}\d$")

# Cards often carry a review excerpt, which Google always wraps in quotes:
#     "By far the best dentist I've ever dealt with in Karachi."
# Left alone it is longer than the street address and wins the address slot.
_QUOTE_OPENERS = "\"“”„«‘’"

# Opening hours, clock times, and other chrome that is never a field we want.
# Anchored, so a street name containing "Open" is not thrown away.
_NOISE = re.compile(
    r"^(open|clos(e|ed|es|ing)|opens|hours|24 hours|temporarily|permanently|"
    r"delivery|takeaway|dine-in|in-store|wheelchair|years in business|"
    r"reserve|book online|order online|no reviews|website|directions|"
    r"\d{1,2}(:\d{2})?\s*(am|pm))",
    re.I,
)

# Price hints Google puts on food and retail cards: "Rs 1-1,000", "$$", "10-20".
# Left alone they take the Category slot on every bakery in the list.
_PRICE = re.compile(
    r"^(\$+|€+|£+|₹+)$"
    r"|^(rs\.?|pkr|inr|aed|sar|usd|eur|gbp|₨|₹|\$|€|£)\s*[\d,.]"
    r"|^[\d,.]+\s*[-–—]\s*[\d,.]+$",
    re.I,
)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _normalize_spaces(value: str) -> str:
    return value.replace(_NARROW_NBSP, " ").replace(_NBSP, " ").strip()


def is_review_quote(seg: str) -> bool:
    """A quoted review excerpt, not a field we want."""
    s = seg.strip()
    return len(s) > 2 and s[0] in _QUOTE_OPENERS


def _is_phone(seg: str) -> bool:
    cleaned = _normalize_spaces(seg)
    # A colon means it is a clock time, not a phone number.
    if ":" in cleaned or not _PHONE_RE.match(cleaned):
        return False
    return 9 <= len(_digits(cleaned)) <= 16


def parse_rating_label(label: str) -> tuple[float | None, int | None]:
    """Read rating and review count off a star element's aria-label."""
    m = _STAR_LABEL_RE.search(label or "")
    if not m:
        return None, None
    try:
        rating = float(m.group(1).replace(",", "."))
    except ValueError:
        return None, None
    reviews = None
    if m.group(2):
        digits = _digits(m.group(2))
        reviews = int(digits) if digits else None
    return rating, reviews


def parse_card_text(text: str, name_hint: str = "") -> dict:
    """Pull category / address / phone / rating out of a result card's innerText."""
    out: dict = {
        "category": "",
        "address": "",
        "phone": "",
        "rating": None,
        "reviews": None,
    }
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return out

    name = (name_hint or lines[0]).strip()
    # Google repeats the business name on the first two lines; drop every copy.
    body = [ln for ln in lines if ln != name]

    content: list[str] = []
    for line in body:
        for seg in (s.strip() for s in line.split(INTERPUNCT)):
            if not seg:
                continue

            if _is_phone(seg):
                if not out["phone"]:
                    out["phone"] = _normalize_spaces(seg)
                continue

            m = _RATING_ONLY_RE.match(seg)
            if m:
                if out["rating"] is None:
                    try:
                        out["rating"] = float(m.group(1).replace(",", "."))
                    except ValueError:
                        pass
                    if m.group(2):
                        digits = _digits(m.group(2))
                        out["reviews"] = int(digits) if digits else None
                continue

            if _NOISE.match(seg) or _PRICE.match(seg) or is_review_quote(seg):
                continue

            content.append(seg)

    if content:
        # Google puts the category first, then the street address, with
        # occasional short filler between them ("Service", "In-store shopping").
        out["category"] = content[0]
        rest = content[1:]
        if rest:
            # A real address nearly always carries a house number or a comma.
            # Prefer those; fall back to the longest remaining segment.
            addressy = [s for s in rest if any(c.isdigit() for c in s) or "," in s]
            out["address"] = max(addressy or rest, key=len)

    return out


def dedupe_key(name: str, phone: str, address: str = "") -> str:
    """A second dedupe handle, for when the same shop has two place ids.

    Phone is the strongest signal a small business has: two listings sharing a
    number are the same business. Without one we fall back to the name plus the
    first chunk of the address.
    """
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 9:
        # Local vs international prefixes for one number differ only at the
        # front, so compare on the last 9 digits.
        return "tel:" + digits[-9:]
    slug = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    where = re.sub(r"[^a-z0-9]+", "", (address or "").lower())[:14]
    return f"nm:{slug}|{where}" if slug else ""
