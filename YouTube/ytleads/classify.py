"""Keyword-heuristic content classifier + exclusion rules.

Signals, in descending trust: channel title > channel keywords > description >
recent video titles > the API's topicCategories. Each category accumulates a
weighted score; the winner takes the channel if it clears `min_score`,
otherwise the channel is filed under "other".

Exclusions run first and independently. Per the brief, animated channels and
heavy motion-graphics channels are dropped rather than categorised.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

# ── Exclusions ───────────────────────────────────────────────────────────

EXCLUSION_RULES: dict[str, dict[str, list[str]]] = {
    "animation": {
        "strong": [
            "animation", "animated", "animations", "cartoon", "cartoons", "anime",
            "2d animation", "3d animation", "stop motion", "claymation", "animatic",
            "motion comic", "stick figure", "toon", "webtoon", "manga", "animator",
            "animated short", "animated story", "animated series", "cgi short",
        ],
        "weak": [
            "blender", "maya", "toon boom", "rigging", "character design",
            "storyboard", "voice acting", "illustration", "drawing", "sketchbook",
        ],
    },
    "motion_graphics": {
        "strong": [
            "motion graphics", "motion design", "motion designer", "mograph",
            "after effects", "aftereffects", "kinetic typography", "cinema 4d",
            "c4d", "houdini fx", "nuke compositing", "vfx breakdown",
            "visual effects breakdown", "element 3d", "trapcode", "redshift render",
            "octane render", "graphic design tutorial", "logo animation",
        ],
        "weak": [
            "vfx", "compositing", "davinci fusion", "premiere pro tutorial",
            "typography", "title sequence", "lower thirds", "render", "shader",
        ],
    },
}

# ── Categories ───────────────────────────────────────────────────────────

CATEGORY_RULES: dict[str, dict[str, list[str]]] = {
    "documentary": {
        "strong": ["documentary", "docuseries", "video essay", "deep dive",
                   "investigation", "untold story", "the rise and fall",
                   "what really happened", "case study", "expose"],
        "weak": ["history", "explained", "story of", "the truth about", "archive",
                 "forgotten", "mystery", "unsolved", "timeline"],
    },
    "true_crime": {
        "strong": ["true crime", "murder case", "serial killer", "cold case",
                   "criminal case", "crime documentary", "missing person"],
        "weak": ["detective", "investigation", "trial", "verdict", "suspect", "police"],
    },
    "vlog": {
        "strong": ["vlog", "vlogs", "vlogger", "daily vlog", "day in my life",
                   "weekly vlog", "life update", "storytime", "van life"],
        "weak": ["my life", "routine", "morning routine", "week in", "diaries", "diary"],
    },
    "travel": {
        "strong": ["travel", "traveling", "travelling", "backpacking", "world tour",
                   "country in", "solo travel", "road trip", "nomad"],
        "weak": ["adventure", "destination", "flight", "hotel review", "airbnb",
                 "visa", "itinerary", "explore"],
    },
    "gaming": {
        "strong": ["gaming", "gameplay", "lets play", "let's play", "walkthrough",
                   "speedrun", "esports", "gamer", "playthrough", "no commentary gameplay"],
        "weak": ["minecraft", "fortnite", "roblox", "valorant", "gta", "fps",
                 "rpg", "console", "steam", "boss fight", "mods"],
    },
    "tech": {
        "strong": ["tech review", "unboxing", "gadget", "smartphone review",
                   "pc build", "laptop review", "tech news", "hardware review",
                   "benchmark", "teardown"],
        "weak": ["iphone", "android", "gpu", "cpu", "ssd", "specs", "review",
                 "setup tour", "keyboard", "monitor"],
    },
    "software_dev": {
        "strong": ["coding", "programming", "software engineer", "web development",
                   "python tutorial", "javascript", "full stack", "devops",
                   "data science", "machine learning", "leetcode"],
        "weak": ["react", "typescript", "api", "database", "sql", "docker",
                 "github", "framework", "algorithm", "backend", "frontend"],
    },
    "ai_tools": {
        "strong": ["ai tools", "artificial intelligence", "chatgpt", "midjourney",
                   "prompt engineering", "ai automation", "generative ai", "ai news"],
        "weak": ["llm", "gpt", "stable diffusion", "automation", "no code", "agent"],
    },
    "finance": {
        "strong": ["personal finance", "stock market", "investing", "investor",
                   "trading", "day trading", "crypto", "bitcoin", "financial freedom",
                   "dividend", "portfolio"],
        "weak": ["money", "budget", "savings", "wealth", "tax", "retirement",
                 "etf", "forex", "market analysis", "recession"],
    },
    "business_marketing": {
        "strong": ["digital marketing", "dropshipping", "ecommerce", "side hustle",
                   "make money online", "entrepreneur", "smma", "affiliate marketing",
                   "shopify", "startup", "freelancing", "copywriting"],
        "weak": ["business", "agency", "sales", "leads", "clients", "revenue",
                 "branding", "seo", "funnel", "productivity"],
    },
    "real_estate": {
        "strong": ["real estate", "realtor", "property investing", "house flipping",
                   "rental property", "landlord", "mortgage"],
        "weak": ["home tour", "property", "listing", "apartment", "housing market"],
    },
    "fitness": {
        "strong": ["fitness", "workout", "bodybuilding", "personal trainer",
                   "gym", "weight loss", "calisthenics", "home workout",
                   "strength training", "fitness coach"],
        "weak": ["exercise", "muscle", "reps", "cardio", "abs", "transformation",
                 "protein", "physique", "training program"],
    },
    "health_nutrition": {
        "strong": ["nutrition", "dietitian", "healthy eating", "mental health",
                   "meditation", "wellness", "holistic health", "sleep science",
                   "biohacking", "therapist"],
        "weak": ["diet", "supplement", "anxiety", "mindfulness", "healing",
                 "immune", "doctor", "symptoms", "recovery"],
    },
    "food_cooking": {
        "strong": ["cooking", "recipe", "recipes", "baking", "chef", "food review",
                   "street food", "restaurant review", "meal prep", "kitchen"],
        "weak": ["food", "cuisine", "dessert", "grill", "vegan", "bbq",
                 "homemade", "eat", "tasting"],
    },
    # Deliberately narrow. Generic words -- "tutorial", "course", "guide",
    # "explained", "beginner" -- appear in most channel descriptions regardless
    # of niche, and made this bucket swallow ~40% of a live run. Only
    # schooling-specific phrases count as strong signals now.
    "education": {
        "strong": ["language learning", "learn english", "learn spanish",
                   "exam preparation", "exam prep", "lecture", "classroom",
                   "revision notes", "study tips", "homework help", "curriculum",
                   "physics class", "chemistry class", "math tutorial",
                   "maths tutorial", "ielts", "toefl", "sat prep", "gcse",
                   "online course", "study abroad"],
        "weak": ["study", "exam", "lesson", "teacher", "student", "school",
                 "university", "syllabus", "tuition", "grammar", "vocabulary"],
    },
    # Creator-economy channels: a big, distinct niche that otherwise scatters
    # across education and business_marketing.
    "youtube_growth": {
        "strong": ["youtube growth", "grow your channel", "youtube automation",
                   "faceless youtube", "youtube algorithm", "youtube seo",
                   "content creator tips", "creator economy", "youtube tips",
                   "thumbnail design", "start a youtube channel", "monetization",
                   "monetized", "adsense", "youtube shorts strategy"],
        "weak": ["channel growth", "subscribers", "cpm", "rpm", "viral",
                 "content strategy", "creator", "watch time", "niche"],
    },
    "science": {
        "strong": ["science", "astronomy", "space exploration", "physics",
                   "biology", "chemistry", "neuroscience", "engineering explained",
                   "quantum", "geology"],
        "weak": ["universe", "nasa", "experiment", "research", "theory",
                 "evolution", "climate", "microscope"],
    },
    "news_commentary": {
        "strong": ["news", "breaking news", "political commentary", "geopolitics",
                   "current affairs", "daily news", "world news", "analysis show"],
        "weak": ["politics", "election", "government", "debate", "opinion",
                 "economy", "conflict", "report"],
    },
    "podcast_interview": {
        "strong": ["podcast", "podcast clips", "interview", "conversations with",
                   "talk show", "episode", "guest"],
        "weak": ["host", "discussion", "roundtable", "ama", "sit down", "full episode"],
    },
    "reaction": {
        "strong": ["reaction", "reacts to", "reacting to", "first time watching",
                   "reaction channel", "review and react"],
        "weak": ["watching", "reacts", "commentary", "watchalong"],
    },
    "comedy_entertainment": {
        "strong": ["comedy", "sketch", "standup", "stand up", "parody", "prank",
                   "funny moments", "meme", "satire", "skits"],
        "weak": ["humor", "hilarious", "laugh", "roast", "jokes", "impression"],
    },
    "beauty_fashion": {
        "strong": ["beauty", "makeup", "skincare", "fashion", "haul", "grwm",
                   "get ready with me", "outfit", "hairstyle", "nails"],
        "weak": ["style", "clothing", "cosmetics", "lookbook", "thrift", "glow up"],
    },
    "automotive": {
        "strong": ["car review", "automotive", "motorcycle", "car build",
                   "supercar", "car detailing", "mechanic", "restoration project",
                   "off road", "drift"],
        "weak": ["car", "engine", "truck", "bike", "garage", "horsepower",
                 "tuning", "test drive"],
    },
    "diy_crafts": {
        "strong": ["diy", "woodworking", "home improvement", "crafts", "handmade",
                   "restoration", "workshop build", "3d printing", "electronics project"],
        "weak": ["build", "tools", "repair", "renovation", "makers", "sewing",
                 "resin", "project"],
    },
    "gardening_outdoors": {
        "strong": ["gardening", "homestead", "permaculture", "bushcraft",
                   "camping", "hiking", "survival skills", "fishing", "farming"],
        "weak": ["garden", "plants", "harvest", "soil", "outdoors", "trail",
                 "wilderness", "greenhouse"],
    },
    "sports": {
        "strong": ["football analysis", "soccer", "basketball", "nba", "nfl",
                   "cricket", "mma", "boxing", "sports analysis", "tactics",
                   "highlights", "f1"],
        "weak": ["match", "player", "team", "league", "coach", "training drills",
                 "tournament", "score"],
    },
    "music": {
        "strong": ["music production", "beat making", "guitar lesson", "piano tutorial",
                   "songwriting", "cover song", "mixing and mastering", "fl studio",
                   "ableton", "singer"],
        "weak": ["music", "song", "album", "chords", "vocals", "producer",
                 "instrumental", "band"],
    },
    "photography_film": {
        "strong": ["photography", "photographer", "filmmaking", "cinematography",
                   "camera review", "video editing", "color grading", "lightroom",
                   "photoshop tutorial", "premiere pro"],
        "weak": ["lens", "shoot", "portrait", "editing", "lighting", "drone",
                 "composition", "bts"],
    },
    "motivation_selfhelp": {
        "strong": ["motivation", "motivational", "self improvement", "personal development",
                   "discipline", "mindset", "life advice", "productivity system",
                   "stoicism", "habits"],
        "weak": ["success", "goals", "inspire", "growth", "confidence",
                 "focus", "routine", "purpose"],
    },
    "kids_family": {
        "strong": ["kids", "toddler", "nursery rhymes", "parenting", "family channel",
                   "toys review", "children learning", "baby"],
        "weak": ["family", "mom", "dad", "school run", "playtime", "educational kids"],
    },
    "pets_animals": {
        "strong": ["dog training", "pet care", "aquarium", "reptile", "cat care",
                   "wildlife", "animal rescue", "veterinarian", "bird keeping"],
        "weak": ["puppy", "kitten", "pets", "animals", "breed", "tank", "rescue"],
    },
    "books_writing": {
        "strong": ["book review", "booktube", "writing advice", "author",
                   "novel writing", "reading vlog", "literature"],
        "weak": ["books", "reading", "chapter", "publishing", "story writing"],
    },
}

# YouTube topicDetails -> our categories. Worth a solid bonus; it comes from
# YouTube's own classification rather than our string matching.
TOPIC_MAP: dict[str, str] = {
    "Video_game_culture": "gaming", "Action_game": "gaming", "Role-playing_video_game": "gaming",
    "Strategy_video_game": "gaming", "Sports_game": "gaming", "Puzzle_video_game": "gaming",
    "Casual_game": "gaming", "Action-adventure_game": "gaming", "Simulation_video_game": "gaming",
    "Racing_video_game": "gaming", "Music_video_game": "gaming",
    "Association_football": "sports", "Basketball": "sports", "American_football": "sports",
    "Cricket": "sports", "Boxing": "sports", "Mixed_martial_arts": "sports",
    "Motorsport": "sports", "Tennis": "sports", "Golf": "sports", "Volleyball": "sports",
    "Sport": "sports", "Professional_wrestling": "sports", "Ice_hockey": "sports",
    "Baseball": "sports",
    "Physical_fitness": "fitness", "Health": "health_nutrition", "Food": "food_cooking",
    "Tourism": "travel", "Vehicle": "automotive", "Pet": "pets_animals",
    "Fashion": "beauty_fashion", "Physical_attractiveness": "beauty_fashion",
    "Knowledge": "education", "Technology": "tech", "Business": "business_marketing",
    "Politics": "news_commentary", "Society": "news_commentary", "Religion": "news_commentary",
    "Military": "documentary", "Humour": "comedy_entertainment", "Entertainment": "comedy_entertainment",
    "Television_program": "comedy_entertainment", "Film": "photography_film",
    "Performing_arts": "music", "Music": "music", "Pop_music": "music",
    "Hip_hop_music": "music", "Rock_music": "music", "Electronic_music": "music",
    "Classical_music": "music", "Country_music": "music", "Independent_music": "music",
    "Soul_music": "music", "Jazz": "music", "Reggae": "music", "Music_of_Asia": "music",
    "Hobby": "diy_crafts", "Lifestyle_(sociology)": "vlog",
}


def _compile(words: list[str]) -> re.Pattern[str]:
    parts = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])", re.I)


_CAT_RE = {
    cat: {tier: _compile(words) for tier, words in tiers.items() if words}
    for cat, tiers in CATEGORY_RULES.items()
}
_EXC_RE = {
    key: {tier: _compile(words) for tier, words in tiers.items() if words}
    for key, tiers in EXCLUSION_RULES.items()
}

# Per-field multipliers.
W_TITLE_STRONG, W_TITLE_WEAK = 3.0, 1.0
W_KEYWORDS_STRONG, W_KEYWORDS_WEAK = 1.5, 0.5
W_DESC_STRONG, W_DESC_WEAK = 1.5, 0.5
W_VIDEO_STRONG, W_VIDEO_WEAK = 0.8, 0.25
VIDEO_CAP = 4.0
TOPIC_BONUS = 2.0


class Verdict(NamedTuple):
    excluded: bool
    exclusion_kind: str
    category: str
    score: float
    runner_up: str
    detail: str


def _hits(pattern: re.Pattern[str] | None, text: str) -> int:
    if not pattern or not text:
        return 0
    return len(set(m.group(0).lower() for m in pattern.finditer(text)))


def _score_group(
    rules: dict[str, re.Pattern[str]],
    title: str,
    keywords: str,
    description: str,
    video_titles: list[str],
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


def classify(
    channel: dict[str, Any],
    video_titles: list[str] | None = None,
    min_score: float = 2.0,
    exclusion_threshold: float = 4.0,
    exclude_animation: bool = True,
    exclude_motion_graphics: bool = True,
) -> Verdict:
    snippet = channel.get("snippet", {}) or {}
    branding = (channel.get("brandingSettings", {}) or {}).get("channel", {}) or {}

    title = snippet.get("title", "") or ""
    description = (snippet.get("description") or branding.get("description") or "")[:5000]
    keywords = branding.get("keywords", "") or ""
    video_titles = [t for t in (video_titles or []) if t][:20]

    # -- exclusions --------------------------------------------------------
    enabled = {
        "animation": exclude_animation,
        "motion_graphics": exclude_motion_graphics,
    }
    for kind, rules in _EXC_RE.items():
        if not enabled.get(kind):
            continue
        score = _score_group(rules, title, keywords, description, video_titles)
        # A strong term in the channel name is decisive on its own.
        name_hit = bool(rules.get("strong") and rules["strong"].search(title))
        if name_hit or score >= exclusion_threshold:
            return Verdict(
                True, kind, kind, round(score, 2), "",
                f"{kind} score {score:.1f}" + (" (name match)" if name_hit else ""),
            )

    # -- categories --------------------------------------------------------
    scores: dict[str, float] = {}
    for cat, rules in _CAT_RE.items():
        s = _score_group(rules, title, keywords, description, video_titles)
        if s > 0:
            scores[cat] = s

    for url in (channel.get("topicDetails", {}) or {}).get("topicCategories", []) or []:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        mapped = TOPIC_MAP.get(slug)
        if mapped:
            scores[mapped] = scores.get(mapped, 0.0) + TOPIC_BONUS

    if not scores:
        return Verdict(False, "", "other", 0.0, "", "no keyword signal")

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else ""

    if best_score < min_score:
        return Verdict(False, "", "other", round(best_score, 2), best,
                       f"top signal {best} too weak ({best_score:.1f})")

    return Verdict(False, "", best, round(best_score, 2), runner_up,
                   f"{best} {best_score:.1f}" + (f", then {runner_up}" if runner_up else ""))


def all_categories() -> list[str]:
    return sorted(CATEGORY_RULES.keys()) + ["other"]
