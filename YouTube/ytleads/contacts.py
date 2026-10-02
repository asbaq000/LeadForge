"""Contact extraction: emails and social profiles.

Two sources:
  1. The channel description from the API (free, no extra requests).
  2. The public About page, which renders the social links the Data API has
     never exposed. Plain HTTP, rate-limited.

What is NOT obtainable: the email behind the About page's "View email address"
button. That is CAPTCHA-gated by design and there is no honest way around it.
In practice most creators who want business mail also paste it in their
description, which is what the parser below is tuned for.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any

import requests

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

# name (at) domain (dot) com  /  name [at] domain dot com  /  name AT domain DOT com
#
# The "at" MUST be delimited -- bracketed, or surrounded by whitespace. Allowing
# a bare "at" makes the pattern match ordinary prose: "cre-at-ion. Whether"
# parses as cre@ion.whether, and "appreci-at-ed.my" as appreci@ed.my. Both were
# observed in real channel descriptions.
_AT = r"(?:[\(\[\{<]\s*(?:at|@)\s*[\)\]\}>]|\s+(?:at|@)\s+)"
_DOT = r"(?:[\(\[\{<]\s*(?:dot|\.)\s*[\)\]\}>]|\s+dot\s+|\.)"

OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]{2,})"
    r"\s*" + _AT + r"\s*"
    r"([A-Za-z0-9\-]{2,}(?:\." + r"[A-Za-z0-9\-]{2,})*?)"
    r"\s*" + _DOT + r"\s*"
    r"([A-Za-z]{2,18})\b",
    re.IGNORECASE,
)

# Obfuscated matches are far lower-precision than plain ones, so their TLD has
# to be real. This is what stops "available at info.example.com" from becoming
# available@info.example. Plain user@host.tld matches skip this check.
COMMON_TLDS = frozenset("""
com net org io co dev app ai xyz me tv info biz online site store shop agency
studio media live email link club pro world today news team group work life
uk us ca au de fr es it nl se no fi dk pl pt ch be ie cz gr ro hu at eu
in pk bd lk np ph id my sg th vn jp kr cn hk tw
ae sa qa il tr ru ua za ng ke eg gh ma
br mx ar cl pe nz edu gov gg cc to ly sh is lv lt ee sk si hr rs bg
""".split())

# Words that mark an address as the business/booking one when several exist.
BUSINESS_HINTS = (
    "business", "inquir", "enquir", "collab", "sponsor", "brand", "partner",
    "contact", "booking", "work with", "promo", "advertis", "pr ", "media",
)

# Reject matches that are really filenames or asset refs.
BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".css", ".js")
BAD_EMAIL_DOMAINS = ("example.com", "domain.com", "email.com", "yourmail.com", "sentry.io")

SOCIAL_PATTERNS: dict[str, re.Pattern[str]] = {
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})", re.I),
    "facebook": re.compile(r"(?:https?://)?(?:www\.|web\.|m\.|business\.)?facebook\.com/([A-Za-z0-9_.\-]{2,60}(?:/[A-Za-z0-9_.\-]{2,60})?)", re.I),
    "twitter": re.compile(r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/([A-Za-z0-9_]{2,20})", re.I),
    "tiktok": re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{2,30})", re.I),
    "linkedin": re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|company)/([A-Za-z0-9_%\-]{2,60})", re.I),
    "discord": re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord\.com/invite)/([A-Za-z0-9\-]{2,30})", re.I),
    "telegram": re.compile(r"(?:https?://)?(?:www\.)?t\.me/([A-Za-z0-9_]{3,40})", re.I),
}

RESERVED_PATHS = {
    "instagram": {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about"},
    "facebook": {"sharer", "share", "tr", "dialog", "plugins", "profile.php", "pages", "login"},
    "twitter": {"intent", "share", "home", "search", "hashtag", "i"},
    "tiktok": {"tag", "music", "discover"},
    "telegram": {"share", "joinchat", "s"},
}

URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)

SOCIAL_HOSTS = (
    "instagram.com", "facebook.com", "twitter.com", "x.com", "tiktok.com",
    "linkedin.com", "discord.gg", "discord.com", "t.me", "youtube.com",
    "youtu.be", "linktr.ee", "beacons.ai", "bio.link", "snapchat.com",
    "pinterest.com", "reddit.com", "twitch.tv", "threads.net",
)


class Contacts(dict):
    """Plain dict with the contact fields, plus a truthiness test."""

    def has_any(self) -> bool:
        return any(self.get(k) for k in (
            "email", "instagram", "facebook", "twitter", "tiktok",
            "linkedin", "discord", "telegram", "website",
        ))


def _clean_email(raw: str) -> str | None:
    e = raw.strip().strip(".,;:()[]{}<>\"'").lower()
    if len(e) > 100 or e.count("@") != 1:
        return None
    local, _, domain = e.partition("@")
    if not local or not domain or "." not in domain:
        return None
    if e.endswith(BAD_EMAIL_SUFFIX) or domain in BAD_EMAIL_DOMAINS:
        return None
    if domain.split(".")[-1].isdigit():
        return None
    return e


def extract_emails(text: str) -> list[str]:
    """All plausible emails, business-looking ones first."""
    if not text:
        return []

    found: list[tuple[int, str]] = []
    for m in EMAIL_RE.finditer(text):
        e = _clean_email(m.group(0))
        if e:
            found.append((m.start(), e))

    for m in OBFUSCATED_RE.finditer(text):
        if m.group(3).lower() not in COMMON_TLDS:
            continue
        domain = re.sub(r"\s+dot\s+", ".", m.group(2), flags=re.I).strip()
        e = _clean_email(f"{m.group(1)}@{domain}.{m.group(3)}")
        if e:
            found.append((m.start(), e))

    lowered = text.lower()
    scored: dict[str, int] = {}
    for pos, email in found:
        window = lowered[max(0, pos - 120) : pos]
        score = 1 if any(h in window for h in BUSINESS_HINTS) else 0
        scored[email] = max(scored.get(email, 0), score)

    return sorted(scored, key=lambda e: (-scored[e], e))


def extract_socials(text: str) -> dict[str, str]:
    """Canonical profile URLs keyed by platform."""
    out: dict[str, str] = {}
    if not text:
        return out
    for platform, pattern in SOCIAL_PATTERNS.items():
        for m in pattern.finditer(text):
            handle = m.group(1).strip("/").strip()
            first = handle.split("/")[0].lower()
            if not handle or first in RESERVED_PATHS.get(platform, set()):
                continue
            out[platform] = _canonical(platform, handle)
            break
    return out


def _canonical(platform: str, handle: str) -> str:
    base = {
        "instagram": "https://instagram.com/",
        "facebook": "https://facebook.com/",
        "twitter": "https://x.com/",
        "tiktok": "https://tiktok.com/@",
        "linkedin": "https://linkedin.com/in/",
        "discord": "https://discord.gg/",
        "telegram": "https://t.me/",
    }[platform]
    if platform == "linkedin" and "company" in handle.lower():
        return f"https://linkedin.com/company/{handle.split('/')[-1]}"
    return base + handle


def extract_website(text: str, exclude: set[str] | None = None) -> tuple[str, list[str]]:
    """Best non-social URL (their site), plus any leftover links."""
    exclude = exclude or set()
    website = ""
    others: list[str] = []
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;:)\"'")
        host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
        if not host or url in exclude:
            continue
        if any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
            if host in ("linktr.ee", "beacons.ai", "bio.link") and url not in others:
                others.append(url)
            continue
        if not website:
            website = url
        elif url not in others:
            others.append(url)
    return website, others[:6]


# ── About-page enrichment ────────────────────────────────────────────────

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Skips the EU consent interstitial that otherwise replaces the page.
        _session.cookies.set("CONSENT", "YES+cb", domain=".youtube.com")
    return _session


def fetch_about_links(channel_id: str, timeout: int = 15, delay: float = 1.5) -> list[str]:
    """External links from the channel's About page.

    YouTube wraps them in /redirect?q=<urlencoded>, so we unwrap rather than
    trying to match whichever renderer name is current this month.
    """
    url = f"https://www.youtube.com/channel/{channel_id}/about"
    try:
        resp = _get_session().get(url, timeout=timeout)
        if resp.status_code != 200:
            return []
        html = resp.text
    except requests.RequestException:
        return []
    finally:
        if delay:
            time.sleep(delay)

    return parse_about_html(html)


def parse_about_html(html: str) -> list[str]:
    """Pull external links out of an About page's HTML. Pure; no network."""
    links: list[str] = []
    # The redirect URLs live inside ytInitialData's JSON, where "&" is encoded
    # as "&" and "/" is sometimes escaped as "\/". The character class must
    # therefore ALLOW backslashes -- excluding them truncates every match at
    # "redirect?event=channel_description", before the q= parameter that holds
    # the actual destination. Only the JSON string terminator stops the match.
    for m in re.finditer(r"youtube\.com\\?/redirect\?[^\"'<>\s]+", html):
        raw = (m.group(0)
               .replace("\\u0026", "&")
               .replace("&amp;", "&")
               .replace("\\/", "/"))
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse("https://www." + raw).query)
        except ValueError:
            continue
        # parse_qs already percent-decodes; decoding again would corrupt any
        # target URL that legitimately contains a "%".
        target = (qs.get("q") or [""])[0].strip()
        if target.startswith(("http://", "https://")) and target not in links:
            links.append(target)

    for m in re.finditer(r"mailto:([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24})", html):
        entry = "mailto:" + m.group(1)
        if entry not in links:
            links.append(entry)

    return links[:40]


def build_contacts(
    description: str,
    about_links: list[str] | None = None,
) -> Contacts:
    """Merge description text and About-page links into one contact record."""
    about_links = about_links or []
    blob = description or ""
    if about_links:
        blob = blob + "\n" + "\n".join(about_links)

    emails = extract_emails(blob)
    socials = extract_socials(blob)
    website, others = extract_website(blob, exclude=set(socials.values()))

    c = Contacts({
        "email": emails[0] if emails else "",
        "all_emails": ", ".join(emails[:4]),
        "instagram": socials.get("instagram", ""),
        "facebook": socials.get("facebook", ""),
        "twitter": socials.get("twitter", ""),
        "tiktok": socials.get("tiktok", ""),
        "linkedin": socials.get("linkedin", ""),
        "discord": socials.get("discord", ""),
        "telegram": socials.get("telegram", ""),
        "website": website,
        "other_links": ", ".join(others),
    })
    return c
