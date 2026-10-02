"""Podcast detection, format guessing and genre taxonomy.

This module replaces the old niche classifier. The question is no longer
"which of thirty content niches is this channel" -- it is:

    1. Is this channel a PODCAST at all?      (the gate; rejects everything else)
    2. If so, what format is it?              (interview / solo / clips / ...)
    3. What is the show about?                (genre, for tabs + segmentation)

Only (1) is load-bearing. Getting it wrong in either direction costs money:
a false negative throws away a real lead, a false positive puts a gaming
channel into an outreach list addressed to podcasters.

Detection is evidence-based rather than keyword-guessy. The strongest
signals, in order:

  * a link to a podcast platform (Spotify show, Apple Podcasts, an RSS feed,
    Buzzsprout/Libsyn/Captivate/...). Nobody links these but podcasters.
  * the word "podcast" in the channel NAME -- in any of ~20 languages.
  * episode numbering across recent video titles ("Ep. 42", "#128", "S2E7").
  * long-form median runtime (a 90-minute upload is not a vlog).
  * guest markers ("ft.", "with Dr. ...", "Guest:") across recent titles.

Either of the first two is decisive on its own. The rest accumulate, which
is what catches shows that never say the word -- interview and talk shows
branded as "The X Show", audio-first feeds that dump episodes to YouTube.
"""

from __future__ import annotations

import re
import statistics
from typing import Any, NamedTuple

# -- "podcast" in the languages a worldwide sweep actually turns up --------
# Latin-script spellings first (they also match inside hashtags and handles),
# then the non-Latin scripts. The regex builder below word-bounds only on
# [a-z0-9], so CJK/Arabic/Devanagari entries match wherever they appear.
PODCAST_WORDS: list[str] = [
    "podcast", "podcasts", "podcaster", "podcasting", "pod cast",
    "podcastu", "podcastul", "podcastas", "podkast", "podkasts",
    "podcasten", "podcastet", "poddcast", "podcastim",
    "podkasti", "podcastera", "podcasty", "podcastes",
    "подкаст",              # ru
    "подкасты",        # ru pl.
    "подкасті",        # uk
    "بودكاست",              # ar
    "البودكاست",  # ar (definite)
    "پادکست",                    # fa
    "पॉडकास्ट",        # hi
    "পডকাস্ট",              # bn
    "پوڈکاسٹ",              # ur
    "ポッドキャスト",              # ja
    "팟캐스트",                                # ko
    "播客",                                            # zh
    "พอดแคสต์",        # th
]

# Show-format words that are not "podcast" but mean the same thing in
# practice. Weaker on their own -- they need corroboration.
SHOW_WORDS: list[str] = [
    "talk show", "chat show", "interview show", "conversations with",
    "in conversation", "radio show", "long form interview",
    "longform interview", "sit down with", "sitdown", "roundtable",
    "fireside chat", "candid conversation", "deep conversations",
    "real talk", "one on one", "unfiltered conversations",
]

# Words that mark a clips/highlights channel cut from a parent show. These
# still qualify -- a clips channel is a podcaster, and often the best lead --
# but they change the FORMAT, not the verdict.
CLIP_WORDS: list[str] = [
    "clips", "highlights", "best moments", "cuts", "moments", "snippets",
    "best of", "full episode on", "watch the full", "full episode here",
    "listen to the full",
]

# -- podcast distribution platforms ---------------------------------------
# A link to any of these is the single highest-precision podcast signal
# there is. Kept as host substrings, matched against link text.
PODCAST_HOSTS: dict[str, str] = {
    "open.spotify.com": "spotify",
    "spotify.link": "spotify",
    "podcasters.spotify.com": "spotify",
    "anchor.fm": "spotify",
    "podcasts.apple.com": "apple_podcasts",
    "itunes.apple.com": "apple_podcasts",
    "podcasts.google.com": "other_platform",
    "music.amazon.com": "other_platform",
    "pod.link": "other_platform",
    "podlink.to": "other_platform",
    "buzzsprout.com": "other_platform",
    "libsyn.com": "other_platform",
    "captivate.fm": "other_platform",
    "podbean.com": "other_platform",
    "transistor.fm": "other_platform",
    "simplecast.com": "other_platform",
    "redcircle.com": "other_platform",
    "spreaker.com": "other_platform",
    "castbox.fm": "other_platform",
    "iheart.com": "other_platform",
    "audioboom.com": "other_platform",
    "megaphone.fm": "other_platform",
    "acast.com": "other_platform",
    "fireside.fm": "other_platform",
    "podcastone.com": "other_platform",
    "podomatic.com": "other_platform",
    "blubrry.com": "other_platform",
    "deezer.com": "other_platform",
    "player.fm": "other_platform",
    "overcast.fm": "other_platform",
    "pocketcasts.com": "other_platform",
    "podcastaddict.com": "other_platform",
    "rss.com": "other_platform",
}

# Links that mean "we book guests" -- a decent secondary podcast signal and
# a genuinely useful outreach field on its own.
BOOKING_HOSTS: tuple[str, ...] = (
    "calendly.com", "typeform.com", "podmatch.com", "matchmaker.fm",
    "guestio.com", "tally.so", "jotform.com", "cal.com", "forms.gle",
    "docs.google.com/forms", "airtable.com",
)

MEMBERSHIP_HOSTS: tuple[str, ...] = (
    "patreon.com", "buymeacoffee.com", "supercast.com", "substack.com",
    "ko-fi.com", "memberful.com", "gumroad.com",
)

# -- episode / guest markers in video titles ------------------------------
# "Ep 12", "Ep. 12", "Episode 12", "#128", "E07", "S2E14"
EPISODE_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:ep|epi|episode|eps|folge|episodio|episodul|epizoda|"
    r"bölüm|bolum)\s*\.?\s*#?\d{1,4}(?![0-9]))"
    r"|(?:(?<![a-z0-9])s\d{1,2}\s*[\-:x ]?\s*e\d{1,3}(?![0-9]))"
    r"|(?:(?<![a-z0-9#])#\s?\d{1,4}(?![0-9]))",
    re.I,
)

# "ft. X", "feat. X", "w/ X", "with Dr. X", "Guest:", "interview with".
#
# The keywords are case-insensitive via scoped (?i:...) groups, but the NAME
# that follows must stay capitalised -- that capital letter is the whole
# reason this doesn't fire on ordinary prose like "cooking with garlic".
GUEST_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])(?i:ft|feat|featuring)\.?\s+[A-ZÀ-ɏ])"
    r"|(?:(?<![A-Za-z0-9])(?i:w/)\s*[A-ZÀ-ɏ])"
    r"|(?:(?<![A-Za-z0-9])(?i:with)\s+(?i:dr\.?\s|prof\.?\s|mr\.?\s|ms\.?\s|mrs\.?\s|sir\s)?"
    r"[A-ZÀ-ɏ][a-zÀ-ɏ]+\s+[A-ZÀ-ɏ])"
    r"|(?:(?<![A-Za-z0-9])(?i:guest)\s*[:\-|])"
    r"|(?:(?<![A-Za-z0-9])(?i:interview\s+with)(?![A-Za-z0-9]))",
    re.UNICODE,
)

# Narrative / scripted-audio shows: no guests, long runtime, story language.
NARRATIVE_WORDS = (
    "storytelling", "audio drama", "narrated", "true story", "case file",
    "investigative series", "documentary series", "audio series",
)

SOLO_WORDS = (
    "solo episode", "monologue", "q&a", "ask me anything",
    "listener questions", "mailbag", "solo show",
)

PANEL_WORDS = (
    "panel", "roundtable", "co-host", "cohost", "co host", "the boys",
    "the guys", "the girls", "we discuss", "our take", "hosts",
)

# -- genres ---------------------------------------------------------------
# Podcast genres, not YouTube niches. These become the sheet's tab names and
# the segmentation you sort outreach by. Shaped around how podcast
# directories actually categorise shows.
GENRE_RULES: dict[str, dict[str, list[str]]] = {
    "business_entrepreneurship": {
        "strong": ["entrepreneur", "entrepreneurship", "founder", "startup",
                   "business podcast", "scaling a business", "bootstrapped",
                   "small business", "ecommerce", "e-commerce", "saas"],
        "weak": ["business", "company", "revenue", "hustle", "operations",
                 "b2b", "agency", "ceo"],
    },
    "marketing_sales": {
        "strong": ["marketing podcast", "digital marketing", "copywriting",
                   "sales podcast", "paid ads", "brand strategy",
                   "growth marketing", "cold email", "lead generation", "seo"],
        "weak": ["marketing", "sales", "funnel", "conversion", "advertising",
                 "positioning", "pipeline", "branding"],
    },
    "finance_investing": {
        "strong": ["investing", "investor", "stock market", "personal finance",
                   "financial freedom", "wealth building", "dividend",
                   "portfolio", "trading podcast", "fund manager"],
        "weak": ["money", "finance", "budget", "retirement", "markets",
                 "economy", "savings", "tax"],
    },
    "crypto_web3": {
        "strong": ["crypto", "bitcoin", "ethereum", "web3", "blockchain",
                   "defi", "nft", "altcoin", "onchain", "digital assets"],
        "weak": ["token", "wallet", "mining", "protocol", "stablecoin"],
    },
    "tech_ai": {
        "strong": ["artificial intelligence", "ai podcast", "machine learning",
                   "tech podcast", "technology podcast", "silicon valley",
                   "product management", "cybersecurity", "robotics", "llm"],
        "weak": ["tech", "technology", "gadgets", "innovation", "data",
                 "automation", "hardware"],
    },
    "software_dev": {
        "strong": ["software engineering", "developer podcast", "programming",
                   "web development", "devops", "open source", "coding podcast",
                   "javascript", "python", "backend"],
        "weak": ["developer", "code", "framework", "api", "database",
                 "architecture", "engineer"],
    },
    "health_fitness": {
        "strong": ["fitness podcast", "strength training", "bodybuilding",
                   "personal trainer", "weight loss", "athletic performance",
                   "hypertrophy", "crossfit", "running coach"],
        "weak": ["fitness", "workout", "gym", "training", "muscle", "exercise",
                 "athlete", "physique"],
    },
    "health_medicine": {
        "strong": ["longevity", "functional medicine", "biohacking",
                   "sleep science", "hormone health", "gut health",
                   "medical podcast", "physician", "public health",
                   "dietitian", "nutrition podcast"],
        "weak": ["health", "wellness", "doctor", "nutrition", "supplements",
                 "recovery", "disease", "clinical"],
    },
    "mental_health": {
        "strong": ["mental health", "therapy podcast", "therapist",
                   "psychology podcast", "anxiety", "depression", "trauma",
                   "counselling", "counseling", "psychiatrist", "burnout"],
        "weak": ["emotions", "healing", "psychology", "wellbeing",
                 "self care", "stress"],
    },
    "self_improvement": {
        "strong": ["self improvement", "personal development",
                   "productivity podcast", "discipline", "stoicism",
                   "motivation podcast", "life coach", "high performance",
                   "mindset podcast", "habits"],
        "weak": ["motivation", "purpose", "goals", "success", "routine",
                 "focus", "coaching", "mindset"],
    },
    "spirituality_religion": {
        "strong": ["christian podcast", "bible study", "islamic podcast",
                   "dawah", "spirituality", "meditation podcast", "buddhism",
                   "theology", "sermon", "gospel", "quran"],
        "weak": ["god", "prayer", "spiritual", "church", "faith", "religion",
                 "scripture", "mindfulness"],
    },
    "relationships_dating": {
        "strong": ["dating podcast", "relationship podcast", "marriage",
                   "love and relationships", "divorce", "modern dating",
                   "sex and relationships", "situationship"],
        "weak": ["dating", "relationships", "love", "couples", "breakup",
                 "attraction", "intimacy"],
    },
    "parenting_family": {
        "strong": ["parenting podcast", "motherhood", "fatherhood",
                   "raising kids", "pregnancy", "postpartum", "homeschool",
                   "family podcast"],
        "weak": ["parenting", "kids", "children", "toddler", "family",
                 "baby", "mom", "dad"],
    },
    "true_crime": {
        "strong": ["true crime", "murder", "serial killer", "cold case",
                   "unsolved", "missing person", "criminal case",
                   "crime podcast", "forensic"],
        "weak": ["crime", "detective", "investigation", "trial", "suspect",
                 "police", "victim"],
    },
    "news_politics": {
        "strong": ["political podcast", "geopolitics", "current affairs",
                   "news podcast", "election", "foreign policy",
                   "policy debate", "commentary podcast", "journalism"],
        "weak": ["news", "politics", "government", "debate", "opinion",
                 "media", "war", "world affairs"],
    },
    "history": {
        "strong": ["history podcast", "ancient history", "world war",
                   "military history", "historian", "forgotten history",
                   "empire", "civilisation", "civilization"],
        "weak": ["history", "historical", "archive", "century", "ancient",
                 "revolution", "dynasty"],
    },
    "science": {
        "strong": ["science podcast", "neuroscience", "astronomy",
                   "physics podcast", "space exploration",
                   "evolutionary biology", "climate science", "quantum"],
        "weak": ["science", "research", "universe", "brain", "biology",
                 "experiment", "theory", "scientist"],
    },
    "education_language": {
        "strong": ["learn english", "language learning", "english podcast",
                   "esl podcast", "exam prep", "academic podcast",
                   "teaching podcast", "learn spanish", "study podcast"],
        "weak": ["learning", "students", "teacher", "school", "university",
                 "education", "lesson", "vocabulary"],
    },
    "comedy": {
        "strong": ["comedy podcast", "comedian", "stand up", "standup",
                   "funny podcast", "improv", "sketch", "satire", "roast"],
        "weak": ["comedy", "funny", "humour", "humor", "jokes", "laugh",
                 "banter", "hilarious"],
    },
    "pop_culture_film": {
        "strong": ["pop culture", "movie podcast", "film podcast", "tv recap",
                   "rewatch podcast", "celebrity", "reality tv",
                   "anime podcast", "media criticism"],
        "weak": ["movies", "film", "cinema", "hollywood", "streaming",
                 "fandom", "series"],
    },
    "music": {
        "strong": ["music podcast", "hip hop podcast", "musician",
                   "producer podcast", "songwriting", "record label",
                   "music industry", "rap podcast"],
        "weak": ["music", "album", "artist", "song", "rap", "beats",
                 "studio", "band", "dj"],
    },
    "gaming": {
        "strong": ["gaming podcast", "esports podcast", "game development",
                   "video game podcast", "game design", "speedrun"],
        "weak": ["gaming", "gamer", "games", "console", "playstation",
                 "xbox", "nintendo", "steam", "twitch"],
    },
    "sports": {
        "strong": ["sports podcast", "football podcast", "soccer podcast",
                   "nba podcast", "nfl podcast", "cricket podcast",
                   "mma podcast", "boxing podcast", "f1 podcast",
                   "fantasy football"],
        "weak": ["sports", "football", "basketball", "cricket", "match",
                 "league", "team", "athlete", "coach"],
    },
    "travel_lifestyle": {
        "strong": ["travel podcast", "digital nomad", "expat life", "van life",
                   "slow living", "minimalism", "lifestyle podcast",
                   "adventure podcast"],
        "weak": ["travel", "lifestyle", "nomad", "abroad", "adventure",
                 "culture shock"],
    },
    "food_drink": {
        "strong": ["food podcast", "chef", "restaurant industry",
                   "coffee podcast", "wine podcast", "culinary", "whisky",
                   "hospitality podcast"],
        "weak": ["food", "cooking", "recipe", "kitchen", "restaurant",
                 "drink", "brewing", "taste"],
    },
    "real_estate": {
        "strong": ["real estate podcast", "property investing", "realtor",
                   "rental property", "house flipping", "landlord",
                   "commercial real estate", "mortgage"],
        "weak": ["real estate", "property", "housing", "tenants", "listing"],
    },
    "career_hr": {
        "strong": ["career podcast", "recruiting", "hr podcast",
                   "leadership podcast", "management podcast",
                   "workplace culture", "job search", "future of work",
                   "remote work"],
        "weak": ["career", "leadership", "hiring", "manager", "employees",
                 "team building"],
    },
    "creator_economy": {
        "strong": ["creator economy", "content creator podcast",
                   "youtube podcast", "creator podcast", "influencer marketing",
                   "personal brand", "audience building", "podcasting tips",
                   "newsletter business"],
        "weak": ["creator", "audience", "monetization", "brand deals",
                 "subscribers", "content strategy"],
    },
    "society_culture": {
        "strong": ["society and culture", "philosophy podcast", "sociology",
                   "anthropology", "social commentary", "activism",
                   "cultural criticism"],
        "weak": ["culture", "society", "philosophy", "community",
                 "generation", "values", "identity"],
    },
    "interview_general": {
        "strong": ["long form interview", "longform interview",
                   "conversations with", "life stories",
                   "in conversation with", "guest interviews"],
        "weak": ["interview", "conversation", "guest", "stories", "journey",
                 "sit down"],
    },
}

FORMATS = (
    "interview", "solo", "co_hosted", "panel", "clips", "narrative",
    "video_first", "other",
)

PENDING_GENRE = "pending_classification"


def all_genres() -> list[str]:
    return sorted(GENRE_RULES.keys()) + ["other"]


def all_formats() -> list[str]:
    return list(FORMATS)


# -- regex assembly -------------------------------------------------------

def _compile(words: list[str]) -> re.Pattern[str]:
    parts = sorted({re.escape(w) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])", re.I)


_PODCAST_RE = _compile(PODCAST_WORDS)
_SHOW_RE = _compile(SHOW_WORDS)
_CLIP_RE = _compile(CLIP_WORDS)
_NARRATIVE_RE = _compile(list(NARRATIVE_WORDS))
_SOLO_RE = _compile(list(SOLO_WORDS))
_PANEL_RE = _compile(list(PANEL_WORDS))

_GENRE_RE = {
    genre: {tier: _compile(words) for tier, words in tiers.items() if words}
    for genre, tiers in GENRE_RULES.items()
}

# Field weights for genre scoring. A podcast's NAME carries most of the
# signal, its recent episode titles the rest. Descriptions are noisy --
# sponsor copy and link dumps drown out the actual subject.
W_TITLE_STRONG, W_TITLE_WEAK = 3.0, 1.0
W_KEYWORDS_STRONG, W_KEYWORDS_WEAK = 1.5, 0.5
W_DESC_STRONG, W_DESC_WEAK = 1.5, 0.5
W_VIDEO_STRONG, W_VIDEO_WEAK = 0.8, 0.25
VIDEO_CAP = 4.0


class PodcastVerdict(NamedTuple):
    is_podcast: bool
    score: float
    confidence: str          # high | medium | low
    fmt: str                 # one of FORMATS
    genre: str
    genre_score: float
    platforms: dict[str, str]
    signals: list[str]
    detail: str


def _hits(pattern: re.Pattern[str] | None, text: str) -> int:
    if not pattern or not text:
        return 0
    return len({m.group(0).lower() for m in pattern.finditer(text)})


def _frac(pattern: re.Pattern[str], titles: list[str]) -> float:
    if not titles:
        return 0.0
    return sum(1 for t in titles if pattern.search(t)) / len(titles)


def detect_platforms(links: list[str]) -> dict[str, str]:
    """Map podcast-platform links found anywhere in the channel's text.

    Returns e.g. {"spotify": "https://open.spotify.com/show/...",
                  "apple_podcasts": "..."}. Only the first URL per slot is
    kept -- outreach needs one good link, not every mirror of one feed.
    """
    found: dict[str, str] = {}
    for url in links:
        low = url.lower()
        for host, slot in PODCAST_HOSTS.items():
            if host not in low:
                continue
            # A Spotify ARTIST or TRACK link is a musician, not a podcaster.
            if "open.spotify.com" in low and not (
                "/show/" in low or "/episode/" in low
            ):
                continue
            if slot == "apple_podcasts" and "podcast" not in low:
                continue
            found.setdefault(slot, url)
            break
    return found


def detect_rss(links: list[str]) -> str:
    for url in links:
        low = url.lower()
        if low.endswith((".rss", ".xml")) or "/rss" in low or "/feed" in low:
            return url
    return ""


def detect_booking(links: list[str]) -> str:
    for url in links:
        if any(h in url.lower() for h in BOOKING_HOSTS):
            return url
    return ""


def detect_membership(links: list[str]) -> str:
    for url in links:
        if any(h in url.lower() for h in MEMBERSHIP_HOSTS):
            return url
    return ""


def _genre_score(
    rules: dict[str, re.Pattern[str]], title: str, keywords: str,
    description: str, video_titles: list[str],
) -> float:
    strong, weak = rules.get("strong"), rules.get("weak")
    score = 0.0
    score += W_TITLE_STRONG * _hits(strong, title)
    score += W_TITLE_WEAK * _hits(weak, title)
    score += W_KEYWORDS_STRONG * _hits(strong, keywords)
    score += W_KEYWORDS_WEAK * _hits(weak, keywords)
    score += W_DESC_STRONG * min(_hits(strong, description), 3)
    score += W_DESC_WEAK * min(_hits(weak, description), 4)

    vid = 0.0
    for t in video_titles:
        if strong and strong.search(t):
            vid += W_VIDEO_STRONG
        elif weak and weak.search(t):
            vid += W_VIDEO_WEAK
    return score + min(vid, VIDEO_CAP)


def guess_genre(
    title: str, keywords: str, description: str, video_titles: list[str],
    min_score: float = 2.0,
) -> tuple[str, float]:
    """Keyword genre guess. Used when the LLM chain is off or has nothing to
    say -- the LLM is far better at this and runs first."""
    scores: dict[str, float] = {}
    for genre, rules in _GENRE_RE.items():
        s = _genre_score(rules, title, keywords, description, video_titles)
        if s > 0:
            scores[genre] = s
    if not scores:
        return "other", 0.0
    best, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score < min_score:
        return "other", round(best_score, 2)
    return best, round(best_score, 2)


def guess_format(
    title: str, description: str, video_titles: list[str],
    median_minutes: float | None, shorts_ratio: float,
) -> str:
    """Best-effort format from runtime shape + title patterns.

    Runtime is the honest signal: a channel whose median upload is 90 seconds
    is a clips channel no matter what its bio claims, and one whose median is
    75 minutes is publishing whole episodes.
    """
    blob = f"{title}\n{description}"
    med = median_minutes if median_minutes is not None else 0.0

    if _CLIP_RE.search(title) or shorts_ratio >= 0.6 or (0 < med <= 5):
        return "clips"

    guest_frac = _frac(GUEST_RE, video_titles)
    if guest_frac >= 0.25:
        return "interview"
    if _NARRATIVE_RE.search(blob) and guest_frac < 0.1:
        return "narrative"
    if _PANEL_RE.search(blob):
        return "panel"
    if _SOLO_RE.search(blob) and guest_frac < 0.1:
        return "solo"
    if med >= 20:
        return "video_first"
    return "other"


def evaluate(
    channel: dict[str, Any],
    video_titles: list[str] | None = None,
    links: list[str] | None = None,
    median_minutes: float | None = None,
    shorts_ratio: float = 0.0,
    min_score: float = 5.0,
    genre_min_score: float = 2.0,
) -> PodcastVerdict:
    """Decide whether this channel is a podcast, and describe it if so.

    `links` should be every URL known for the channel (description URLs plus
    About-page links) -- platform detection is the highest-value signal and
    it lives almost entirely in those links.
    """
    snippet = channel.get("snippet", {}) or {}
    branding = (channel.get("brandingSettings", {}) or {}).get("channel", {}) or {}

    title = snippet.get("title", "") or ""
    handle = (snippet.get("customUrl", "") or "").lstrip("@")
    description = (snippet.get("description") or branding.get("description") or "")[:5000]
    keywords = branding.get("keywords", "") or ""
    video_titles = [t for t in (video_titles or []) if t][:25]
    links = links or []

    platforms = detect_platforms(links + [description])
    signals: list[str] = []
    score = 0.0

    # -- decisive signals --------------------------------------------------
    name_hit = bool(_PODCAST_RE.search(title)) or bool(_PODCAST_RE.search(handle))
    if name_hit:
        score += 6.0
        signals.append("podcast-in-name")

    if platforms:
        score += 4.0 + 1.0 * (len(platforms) - 1)
        signals.append("platform:" + "/".join(sorted(platforms)))

    # -- corroborating signals ---------------------------------------------
    kw_hits = _hits(_PODCAST_RE, keywords)
    if kw_hits:
        score += min(3.0, 1.5 * kw_hits)
        signals.append("podcast-in-keywords")

    desc_hits = _hits(_PODCAST_RE, description)
    if desc_hits:
        score += min(3.0, 1.5 * desc_hits)
        signals.append("podcast-in-description")

    show_wording = bool(_SHOW_RE.search(title) or _SHOW_RE.search(keywords))
    if show_wording:
        score += 2.0
        signals.append("show-format-wording")
    elif _SHOW_RE.search(description):
        show_wording = True
        score += 1.0
        signals.append("show-format-wording")

    # A clips/highlights channel that points back at a podcast is a
    # podcaster lead in its own right -- often the most useful one, since
    # whoever runs it is already paying to have episodes cut up. Without
    # this, "Deep End Clips" scores only the single mention in its bio and
    # falls short of the bar. The clip wording is worthless on its own
    # (football highlights channels use it too), so it only counts when a
    # podcast is actually referenced somewhere.
    vid_pod_frac = _frac(_PODCAST_RE, video_titles)
    mentions_podcast = bool(name_hit or kw_hits or desc_hits or vid_pod_frac)
    clips_of_podcast = bool(_CLIP_RE.search(title)) and bool(mentions_podcast or platforms)
    if clips_of_podcast:
        score += 4.0
        signals.append("clips-of-a-podcast")

    ep_frac = _frac(EPISODE_RE, video_titles)
    if ep_frac >= 0.5:
        score += 4.0
        signals.append(f"episode-numbering({ep_frac:.0%})")
    elif ep_frac >= 0.2:
        score += 2.5
        signals.append(f"episode-numbering({ep_frac:.0%})")

    guest_frac = _frac(GUEST_RE, video_titles)
    if guest_frac >= 0.4:
        score += 3.0
        signals.append(f"guest-titles({guest_frac:.0%})")
    elif guest_frac >= 0.2:
        score += 1.5
        signals.append(f"guest-titles({guest_frac:.0%})")

    if median_minutes is not None:
        if median_minutes >= 45:
            score += 3.5
            signals.append(f"long-form({median_minutes:.0f}m median)")
        elif median_minutes >= 20:
            score += 2.0
            signals.append(f"mid-form({median_minutes:.0f}m median)")

    if vid_pod_frac >= 0.2:
        score += 2.0
        signals.append("podcast-in-episode-titles")

    # -- verdict -----------------------------------------------------------
    # Name or platform link alone is enough. Everything else has to add up
    # AND include at least one signal that is actually about the format.
    #
    # That second requirement is not decoration. Episode numbering and a
    # 20-minute median are both format-agnostic: a Let's Play channel
    # posting "Minecraft survival #11" at 22 minutes clears the raw score
    # on its own and was qualifying as a podcast before this gate existed.
    # A show has to look like a show somewhere -- in its name, its bio, its
    # keywords, its platform links, talk-show wording, or guests in its
    # episode titles.
    #
    # The one exception is a genuinely long-form numbered series: a 45+
    # minute median with episode numbers is not a gaming channel, and this
    # is what keeps famously unbranded interview shows (whose titles are
    # just "#412 - Guest Name") in the list.
    topical = bool(
        name_hit or platforms or kw_hits or desc_hits or show_wording
        or clips_of_podcast or vid_pod_frac >= 0.2 or guest_frac >= 0.2
    )
    long_form_series = (median_minutes or 0) >= 45 and ep_frac >= 0.2

    decisive = name_hit or bool(platforms)
    is_podcast = decisive or (
        score >= min_score and (topical or long_form_series)
    )

    if decisive and score >= min_score + 3:
        confidence = "high"
    elif decisive or score >= min_score + 3:
        confidence = "medium"
    else:
        confidence = "low"

    genre, genre_score = guess_genre(
        title, keywords, description, video_titles, genre_min_score
    )
    fmt = guess_format(title, description, video_titles, median_minutes, shorts_ratio)

    detail = f"score {score:.1f} [{', '.join(signals) or 'no signals'}]"
    return PodcastVerdict(
        is_podcast=is_podcast,
        score=round(score, 2),
        confidence=confidence,
        fmt=fmt,
        genre=genre,
        genre_score=genre_score,
        platforms=platforms,
        signals=signals,
        detail=detail,
    )


# -- offline host-name extraction -----------------------------------------
# The host's name is the most valuable field in a cold email and the one an
# LLM is normally needed for. But podcast channels name their host in the
# title far more often than not -- "Matt Beall Podcast", "Podcast With
# Jeegar", "TAP - The Aman Podcast", "The Ravi Kapoor Show" -- and bios say
# "hosted by ...". That is worth extracting for free, so a dead API key
# costs you a genre, not a name.
#
# The rule throughout: a WRONG host name is far worse than a blank one. A
# candidate is rejected unless it looks like a person.

# Words that prove a title fragment is a topic, not a person. "The Deep End
# Podcast" must never yield a host called "Deep End".
_NOT_A_NAME = frozenset("""
the a an my our your his her their this that these those and or of for with
podcast podcasts show shows episode episodes series channel network studio
media radio talk talks conversation conversations interview interviews
chat chats live daily weekly monthly morning evening night late early
deep end real true false new old big small good bad best worst top
business money finance crypto tech technology ai health fitness food travel
crime news politics history science education comedy music gaming sports
life lifestyle story stories mind mindset growth success motivation
club room table corner hour minute time world life work career
project files file case cases diary journal notes note report
untold unfiltered uncut honest raw open close inside outside
men women guys girls boys people human humans folks
""".split())

# Case-insensitive via a scoped group: the patterns below are case-SENSITIVE
# on purpose (the capital letter is what proves the next word is a name), so
# the honorific has to opt into case-insensitivity by itself or "Dr. Andrew
# Huberman" silently yields no host at all.
_HONORIFICS = r"(?i:dr|prof|mr|mrs|ms|sir|shaikh|sheikh|imam|pastor|coach|chef|capt)\.?"

# "Hosted by X", "your host X", "I'm X", "My name is X" -- description forms.
_HOST_DESC_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i:hosted\s+by)\s+(?:" + _HONORIFICS + r"\s+)?"
               r"([A-Z\u00c0-\u024f][\w'’\-]+(?:\s+[A-Z\u00c0-\u024f][\w'’\-]+){0,2})"),
    re.compile(r"(?i:your\s+host,?)\s+(?:" + _HONORIFICS + r"\s+)?"
               r"([A-Z\u00c0-\u024f][\w'’\-]+(?:\s+[A-Z\u00c0-\u024f][\w'’\-]+){0,2})"),
    re.compile(r"(?i:host(?:ed)?\s*[:\-]\s*)(?:" + _HONORIFICS + r"\s+)?"
               r"([A-Z\u00c0-\u024f][\w'’\-]+(?:\s+[A-Z\u00c0-\u024f][\w'’\-]+){0,2})"),
    re.compile(r"(?i:my\s+name\s+is)\s+"
               r"([A-Z\u00c0-\u024f][\w'’\-]+(?:\s+[A-Z\u00c0-\u024f][\w'’\-]+){0,2})"),
    re.compile(r"(?i:i\s*['’]?\s*m)\s+"
               r"([A-Z\u00c0-\u024f][\w'’\-]+\s+[A-Z\u00c0-\u024f][\w'’\-]+)"),
)

# Title forms, most reliable first. "<anything> with NAME" is the strongest,
# because "with" is followed by a person almost every time.
_HOST_TITLE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i:\b(?:with|w/|by|ft\.?|featuring))\s+(?:" + _HONORIFICS + r"\s+)?"
               r"([A-Z\u00c0-\u024f][\w'’\-]+(?:\s+[A-Z\u00c0-\u024f][\w'’\-]+){0,2})\s*$"),
    re.compile(r"^(?:" + _HONORIFICS + r"\s+)?"
               r"([A-Z\u00c0-\u024f][\w'’\-]+(?:\s+[A-Z\u00c0-\u024f][\w'’\-]+){0,2})"
               r"\s*['’]?s?\s+(?i:podcast|show|pod)\b"),
    re.compile(r"^(?i:the)\s+"
               r"([A-Z\u00c0-\u024f][\w'’\-]+(?:\s+[A-Z\u00c0-\u024f][\w'’\-]+){0,2})"
               r"\s+(?i:podcast|show|pod)\b"),
)

# "TAP - The Aman Podcast" / "TAP | The Aman Podcast": drop the acronym.
_TITLE_PREFIX_RE = re.compile(r"^\s*[A-Z0-9]{2,6}\s*[\-–—|:]\s*")


def _looks_like_a_person(candidate: str) -> str:
    """Return the name if it plausibly belongs to a human, else ''."""
    name = " ".join(candidate.replace("_", " ").split()).strip(" -–—|:'’\"")
    if not name or any(ch.isdigit() for ch in name):
        return ""
    words = name.split()
    if not 1 <= len(words) <= 3 or len(name) > 40:
        return ""
    for w in words:
        bare = w.strip("'’-.").lower()
        if not bare or bare in _NOT_A_NAME:
            return ""
        if not w[0].isupper():
            return ""
    # A single all-caps token is an acronym (TAP, BBC), not a first name.
    if len(words) == 1 and (name.isupper() or len(name) < 3):
        return ""
    return name


def guess_host(title: str, description: str = "") -> str:
    """Best-effort host name from the channel title and bio. '' when unsure.

    Description patterns are tried first -- "hosted by X" states outright
    what a title only implies. Nothing here guesses: every candidate has to
    survive `_looks_like_a_person`, so a topic-named show returns blank
    rather than inventing a host called "Deep End".
    """
    for pattern in _HOST_DESC_RES:
        m = pattern.search(description or "")
        if m:
            name = _looks_like_a_person(m.group(1))
            if name:
                return name

    cleaned = _TITLE_PREFIX_RE.sub("", title or "")
    for pattern in _HOST_TITLE_RES:
        m = pattern.search(cleaned)
        if m:
            name = _looks_like_a_person(m.group(1))
            if name:
                return name
    return ""


# -- offline language detection -------------------------------------------
# Script ranges are decisive for non-Latin languages, which is most of what
# a worldwide sweep turns up and exactly where an English-tuned model is
# least reliable. Latin-script languages stay blank rather than guessed --
# Spanish and Portuguese are not separable by character ranges.
_SCRIPTS: tuple[tuple[str, str], ...] = (
    ("Arabic", r"[\u0600-\u06ff\u0750-\u077f]"),
    ("Hebrew", r"[\u0590-\u05ff]"),
    ("Russian", r"[\u0400-\u04ff]"),           # Cyrillic; ru is the safe default
    ("Greek", r"[\u0370-\u03ff]"),
    ("Hindi", r"[\u0900-\u097f]"),             # Devanagari
    ("Bengali", r"[\u0980-\u09ff]"),
    ("Tamil", r"[\u0b80-\u0bff]"),
    ("Telugu", r"[\u0c00-\u0c7f]"),
    ("Thai", r"[\u0e00-\u0e7f]"),
    ("Korean", r"[\uac00-\ud7af\u1100-\u11ff]"),
    ("Japanese", r"[\u3040-\u309f\u30a0-\u30ff]"),   # kana; must precede Han
    ("Chinese", r"[\u4e00-\u9fff]"),
)
_SCRIPT_RES = tuple((name, re.compile(rx)) for name, rx in _SCRIPTS)


def guess_language(title: str, description: str = "") -> str:
    """Language from the writing system. '' for Latin script (unguessable)."""
    blob = f"{title}\n{description[:400]}"
    for name, rx in _SCRIPT_RES:
        if rx.search(blob):
            return name
    return ""


# -- episode-shape helpers ------------------------------------------------

_DURATION_RE = re.compile(
    r"^P(?:(?P<d>\d+)D)?T?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?$"
)


def parse_duration(iso: str) -> float:
    """ISO-8601 duration -> minutes. 'PT1H23M4S' -> 83.07. 0.0 if unparseable."""
    if not iso:
        return 0.0
    m = _DURATION_RE.match(iso.strip())
    if not m:
        return 0.0
    return round(
        int(m.group("d") or 0) * 1440
        + int(m.group("h") or 0) * 60
        + int(m.group("m") or 0)
        + int(m.group("s") or 0) / 60.0,
        2,
    )


class EpisodeShape(NamedTuple):
    median_minutes: float | None
    longest_minutes: float | None
    shorts_ratio: float          # share of sampled uploads under ~60s
    avg_views: int | None
    sampled: int


def episode_shape(videos: list[dict[str, Any]]) -> EpisodeShape:
    """Runtime + reach profile from a videos.list batch (1 quota unit per 50).

    Shorts are excluded from the median so a podcast that also posts daily
    clips still reads as long-form -- but their share is reported separately,
    because "does this show already cut clips" is exactly what an agency
    wants to know before pitching.
    """
    minutes: list[float] = []
    views: list[int] = []
    shorts = 0
    for v in videos:
        dur = parse_duration((v.get("contentDetails") or {}).get("duration") or "")
        if dur <= 0:
            continue
        minutes.append(dur)
        if dur <= 1.05:
            shorts += 1
        try:
            views.append(int((v.get("statistics") or {}).get("viewCount", 0)))
        except (TypeError, ValueError):
            pass

    if not minutes:
        return EpisodeShape(None, None, 0.0, None, 0)

    long_form = [m for m in minutes if m > 1.05] or minutes
    return EpisodeShape(
        median_minutes=round(statistics.median(long_form), 1),
        longest_minutes=round(max(minutes), 1),
        shorts_ratio=round(shorts / len(minutes), 3),
        avg_views=int(statistics.mean(views)) if views else None,
        sampled=len(minutes),
    )
