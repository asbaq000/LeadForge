"""Contact extraction and outreach-prep for a qualified lead."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')]+|(?<!@)\bwww\.[^\s<>\"')]+", re.IGNORECASE)
HANDLE_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_.]{2,30})")

BOOKING_HINTS = ("calendly.com", "cal.com", "tidycal", "savvycal", "hubspot.com/meetings")

# Ignore links that are just Threads/Instagram chrome, not the lead's own site.
_IGNORE_DOMAINS = ("threads.net", "threads.com", "instagram.com", "fb.com", "facebook.com")


def extract_emails(*texts: str) -> list[str]:
    found: list[str] = []
    for t in texts:
        for m in EMAIL_RE.findall(t or ""):
            if m.lower() not in {f.lower() for f in found}:
                found.append(m)
    return found


def extract_links(*texts: str) -> list[str]:
    found: list[str] = []
    for t in texts:
        for m in URL_RE.findall(t or ""):
            url = m.rstrip(".,);:")
            if any(d in url.lower() for d in _IGNORE_DOMAINS):
                continue
            if url.lower() not in {f.lower() for f in found}:
                found.append(url)
    return found


def has_booking_link(*texts: str) -> bool:
    blob = " ".join(t or "" for t in texts).lower()
    return any(h in blob for h in BOOKING_HINTS)


# ---------------------------------------------------------------------------
#  Suggested DM opener
#  Deliberately template-based (no API key needed). It references the lead's own
#  words so the rep sending it isn't starting from a blank box.
# ---------------------------------------------------------------------------

_TOPIC_PHRASING = {
    "n8n": "n8n workflow",
    "make_zapier": "Make/Zapier setup",
    "chatbot": "chatbot build",
    "ai_agents": "AI agent",
    "scraping": "scraping/data-extraction job",
    "api_integr": "API integration",
    "python": "Python automation",
    "llm_custom": "custom LLM/GPT build",
    "nocode": "no-code build",
    "data_ops": "spreadsheet/data workflow",
}


_ASK_KEYWORDS = (
    "need", "looking for", "how do", "how to", "how can", "anyone", "help",
    "struggl", "stuck", "hiring", "trying to", "automate", "build", "want to",
    "is there a", "wish",
)


def _summarise_ask(text: str, limit: int = 100) -> str:
    """Pull the sentence carrying the actual ask, for quoting back to them.

    Ranks candidates rather than taking the first hit - "Anyone able to help?"
    matches a keyword but says nothing, so length is part of the score.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", (text or "").strip()) if s.strip()]
    if not sentences:
        return ""

    def rank(s: str) -> tuple[int, int]:
        low = s.lower()
        hits = sum(1 for k in _ASK_KEYWORDS if k in low)
        words = len(s.split())
        # Sentences under ~6 words carry no detail worth quoting.
        substance = words if words >= 6 else -1
        return (hits > 0, substance)

    best = max(sentences, key=rank)
    if rank(best) == (False, -1):
        best = sentences[0]

    best = re.sub(r"\s+", " ", best).strip(" .,!?")
    return best[:limit].rstrip() + ("..." if len(best) > limit else "")


def suggest_opener(post_text: str, topics: list[str], first_name: str = "") -> str:
    topic_phrase = "automation project"
    for t in topics:
        if t in _TOPIC_PHRASING:
            topic_phrase = _TOPIC_PHRASING[t]
            break

    greeting = f"Hey {first_name}" if first_name else "Hey"
    ask = _summarise_ask(post_text)

    return (
        f'{greeting} - saw your post about "{ask}". '
        f"We build exactly this kind of {topic_phrase} for teams. "
        f"Happy to sketch out how I'd approach it, no pitch. Want me to send a quick outline?"
    )


def first_name_from(full_name: str, username: str) -> str:
    """Best-effort first name; falls back to empty so the opener stays generic."""
    src = (full_name or "").strip()
    if src:
        token = re.split(r"[\s._\-]+", src)[0]
        if token.isalpha() and 2 <= len(token) <= 20:
            return token.capitalize()
    token = re.split(r"[._\-0-9]+", username or "")[0]
    if token.isalpha() and 3 <= len(token) <= 20:
        return token.capitalize()
    return ""
