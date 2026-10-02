"""Search sources and keyword vocabularies for the Reddit AI-lead generator.

Everything here is data, not logic, so the tuning knobs live in one place.
"""

# --- Where to look -----------------------------------------------------------
# Reddit's public JSON API is read-only and needs no account, no app
# registration and no OAuth token: every listing page also answers on `.json`.

# Subreddits where people hire / post paid gigs.
GIG_SUBREDDITS = [
    "forhire",
    "jobbit",
    "hiring",
    "freelance_forhire",
    "RemoteJobs",
    "remotejs",
    "WorkOnline",
    "b2bforhire",
    "slavelabour",
    "DoneDirtCheap",
]

# Subreddits about AI/automation where "I need someone to build X" shows up.
AI_SUBREDDITS = [
    "AI_Agents",
    "n8n",
    "LLMDevs",
    "MachineLearningJobs",
    "automation",
    "LocalLLaMA",
    "PromptEngineering",
    "AutoGenAI",
    "MachineLearning",
    "datascience",
    "ChatGPTCoding",
    "OpenAI",
    "SaaS",
    "Entrepreneur",
    "smallbusiness",
    "startups",
    "webdev",
    "freelance",
]

# Queries run against the gig subreddits (subreddit-restricted search).
# Reddit search supports OR, so a few broad queries cover the same ground as a
# long list of single-word ones while making far fewer requests.
GIG_QUERIES = [
    "AI OR LLM OR GPT",
    "machine learning OR deep learning OR NLP",
    "chatbot OR automation OR n8n OR prompt engineer",
]

# Queries run against AI subreddits, aimed at hiring intent.
AI_QUERIES = [
    "hiring OR recruiting OR vacancy",
    "freelancer OR contractor OR paid OR budget",
]

# Site-wide searches, to catch subreddits not on the lists above.
GLOBAL_QUERIES = [
    "hiring AI engineer remote",
    "hiring LLM developer remote",
    "looking for AI developer remote paid",
    "hiring machine learning engineer remote",
    "need AI automation developer paid remote",
    "hiring prompt engineer remote",
    "AI chatbot developer needed remote",
    "remote AI contract work",
]

# Subreddits that are remote-only by their own rules. A post from one of these
# counts as remote even when the author never types the word.
REMOTE_BY_DEFAULT_SUBREDDITS = {
    "remotejobs",
    "remotejs",
    "workonline",
    "jobbit",
    "digitalnomadjobs",
    "remotework",
}

# --- What counts as AI work --------------------------------------------------
# Weighted: strong terms are unambiguous, weak terms only count as AI when
# something else on the post already looks like AI work.
AI_TERMS_STRONG = [
    "artificial intelligence", "machine learning", "deep learning",
    "large language model", "llm", "llms", "genai", "generative ai",
    "gpt", "gpt-4", "gpt-5", "chatgpt", "openai", "anthropic", "claude",
    "gemini", "llama", "mistral", "huggingface", "hugging face",
    "langchain", "langgraph", "llamaindex", "crewai", "autogen",
    "rag", "retrieval augmented", "vector database", "pinecone", "weaviate",
    "qdrant", "chromadb", "embeddings", "embedding model",
    "chatbot", "chat bot", "voice agent", "voice ai", "conversational ai",
    "nlp", "natural language processing", "computer vision",
    "prompt engineer", "prompt engineering", "fine-tune", "fine tune",
    "fine-tuning", "finetuning", "stable diffusion", "comfyui", "midjourney",
    "pytorch", "tensorflow", "scikit-learn", "mlops", "ml engineer",
    "ai engineer", "ai developer", "ai agent", "ai agents", "agentic",
    "whisper", "text to speech", "speech to text", "tts", "ocr",
    "recommendation engine", "predictive model", "data annotation",
    "computer-use agent", "ai automation", "ai workflow",
]

AI_TERMS_WEAK = [
    "ai", "a.i.", "automation", "automate", "n8n", "zapier", "make.com",
    "scraper", "scraping", "data pipeline", "python", "api integration",
    "algorithm",
]

# --- What counts as someone with money to spend ------------------------------
HIRING_TERMS = [
    "[hiring]", "hiring", "we're hiring", "we are hiring", "now hiring",
    "looking for", "looking to hire", "seeking", "need someone",
    "need a developer", "need help", "want to hire", "in search of", "iso ",
    "job opening", "open position", "vacancy", "recruiting",
    "contract", "contractor", "freelance", "freelancer", "consultant",
    "budget", "paid", "will pay", "compensation", "salary", "rate",
    "apply", "send your portfolio", "dm me", "pm me",
]

# Posts where the author is selling their own services, not buying.
SELF_PROMO_TERMS = [
    "[for hire]", "[forhire]", "for hire", "[available]", "available for hire",
    "hire me", "i am available", "i'm available", "my portfolio",
    "offering my services", "i offer", "i can build", "i will build",
    "[offer]", "[advert]", "[showcase]",
]

# Words that make a post a full-time-ish job rather than a one-off gig.
JOB_TERMS = [
    "full-time", "full time", "part-time", "part time", "salary", "salaried",
    "benefits", "position", "role", "employee", "w2", "annual", "per year",
    "/yr", "fte",
]

# --- Remote vs onsite --------------------------------------------------------
REMOTE_TERMS = [
    "remote", "fully remote", "100% remote", "remote-first", "remote only",
    "work from home", "work-from-home", "wfh", "telecommute",
    "anywhere in the world", "work from anywhere", "anywhere", "worldwide",
    "global", "any timezone", "any time zone", "distributed team",
    "async", "asynchronous", "location independent", "no location requirement",
    "online", "virtual",
]

# Any of these disqualifies the post unless it is negated ("no onsite").
ONSITE_TERMS = [
    "onsite", "on-site", "on site", "in-person", "in person",
    "in office", "in-office", "on premise", "on-premise", "on-prem",
    "hybrid", "must be located", "must be based", "must reside",
    "must live in", "must be local", "local only", "locals only",
    "local candidates", "relocate", "relocation", "willing to relocate",
    "commute", "commutable", "days in the office", "days in office",
    "come to the office", "our office", "office-based", "office based",
    "based in our", "report to the office",
]

# Negations that appear right before an onsite term and cancel it.
NEGATION_PREFIXES = [
    "no", "not", "non", "never", "zero", "without", "isn't", "is not",
    "aren't", "are not", "won't", "will not", "don't", "do not", "avoid",
    "instead of", "rather than", "free of", "0",
]

# --- Recency -----------------------------------------------------------------
DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_LEAD_COUNT = 0  # 0 = return every lead that passes the filters

# --- HTTP --------------------------------------------------------------------
# Reddit asks for a descriptive, non-browser-impersonating User-Agent.
USER_AGENT = "python:reddit-ai-leads:1.0 (public JSON reader; no account)"
REQUEST_DELAY_SECONDS = 2.0
MAX_RETRIES = 4


# --- Demand intent -----------------------------------------------------------
# The original HIRING_TERMS list was too loose: "paid", "contract", "rate" and
# "apply" all show up in course ads, news posts and rants. A lead has to show
# that the poster *themselves* is hiring or commissioning work, so the phrases
# below are first-person and demand-side rather than merely job-flavoured.

DEMAND_STRONG = [
    # explicit hiring declarations
    "[hiring]", "(hiring)", "hiring:", "we are hiring", "we're hiring",
    "i am hiring", "i'm hiring", "now hiring", "is hiring", "currently hiring",
    "looking to hire", "want to hire", "wanting to hire", "need to hire",
    "ready to hire", "hiring a", "hiring an", "hiring for",
    # first-person searches for a person
    "we are looking for", "we're looking for", "i am looking for",
    "i'm looking for", "looking for someone", "looking for a developer",
    "looking for a dev", "looking for an engineer", "looking for a freelancer",
    "looking for a contractor", "looking for an ai", "looking for help building",
    "our team is looking", "my team is looking",
    "i need someone", "we need someone", "need someone to", "need someone who",
    "i need a developer", "we need a developer", "need a developer",
    "need an engineer", "need an ai", "need help building", "need built",
    "seeking a", "seeking an", "seeking someone", "in search of someone",
    # role-needed phrasings
    "developer needed", "dev needed", "engineer needed", "freelancer needed",
    "contractor needed", "consultant needed", "help wanted", "wanted:",
    # formal postings
    "job description", "job opening", "open position", "position available",
    "open role", "we have an opening", "join our team", "role available",
    # money the poster is offering
    "willing to pay", "i will pay", "we will pay", "happy to pay", "can pay",
    "ready to pay", "will compensate", "my budget", "our budget", "budget is",
    "budget of", "budget:", "paying $", "pay $", "offering $",
    "paid gig", "paid project", "paid opportunity", "paid role", "paid work",
    "freelance opportunity", "contract opportunity", "contract role",
    "contract position",
]

# Fixed phrases only match contiguous text, but real posts write "looking for
# a remote n8n automation freelancer". So a seek verb followed within a short
# window by a role noun also counts as demand.
DEMAND_SEEK_VERBS = [
    "looking for", "look for", "looking to", "seeking", "searching for",
    "in search of", "need", "needs", "needed", "want", "wanting",
    "hiring", "recruiting", "recruit", "after", "require", "requires",
]

DEMAND_ROLE_NOUNS = [
    "developer", "dev", "devs", "engineer", "engineers", "freelancer",
    "freelancers", "contractor", "consultant", "programmer", "coder",
    "expert", "experts", "specialist", "agency", "someone", "somebody",
    "person", "team", "builder", "architect", "scientist", "analyst",
    "professional", "pro", "talent", "candidate", "partner",
]

# How far after the verb the role noun may sit.
DEMAND_WINDOW_CHARS = 60

# An actionable hook: proof there is a real way to take the work.
ACTIONABLE_CONTACT = [
    "dm me", "pm me", "send me a dm", "message me", "email me", "contact me",
    "reach out", "get in touch", "hit me up", "apply here", "apply now",
    "to apply", "send your", "share your", "send me your", "comment below",
    "leave a comment", "if interested", "let me know if", "happy to discuss",
    "more details on request",
]

# --- Noise the demand gate cannot catch on its own ---------------------------
# Rejected wherever they appear: none of these can plausibly be a project
# brief. Deliberately narrow — "newsletter", "youtube" and "bootcamp" are NOT
# here, because "automate my newsletter" is a real lead.
VETO_PROMO = [
    "udemy", "coursera", "skillshare", "free course", "my course",
    "join my course", "enroll now", "enrollment", "coupon code",
    "discount code", "promo code", "100% off", "limited time offer",
    "masterclass", "webinar", "affiliate link", "referral link", "giveaway",
    "sign up for my", "link in bio", "dm for the link",
]

# Rejected only when they appear in the TITLE. The title says what kind of
# post it is; the same words in a body are often incidental ("we just
# launched, now we need an AI dev" is a real lead).
TITLE_NOISE = {
    "news/announcement": [
        "announced", "announces", "announcing", "has released", "releases",
        "launches", "launched today", "study finds", "research shows",
        "report says", "according to", "breaking", "just dropped",
        "is now available", "new model", "comparison",
    ],
    "discussion/venting": [
        "what do you think", "thoughts", "your thoughts", "discussion",
        "unpopular opinion", "am i the only one", "rant", "venting",
        "vent", "change my mind", "hot take", "poll", "survey", "eli5",
        "does anyone else", "why does everyone", "is it just me",
    ],
    "showcase": [
        "i built", "i made", "i created", "i developed", "we built", "we made",
        "just launched", "check out my", "feedback on my", "roast my",
        "review my", "sharing my", "showcase", "show off", "my first",
        "i open sourced", "open sourced my",
    ],
    "advice-seeking": [
        "how do i", "how can i", "how to", "any advice", "need advice",
        "recommendations", "recommend", "which tool", "what tool",
        "best way to", "is it worth", "should i", "worth learning",
        "career advice", "beginner question", "noob question",
        "getting started", "roadmap", "learning path", "help me understand",
    ],
    "seeking work": [
        "looking for work", "looking for a job", "seeking opportunities",
        "open to work", "my resume", "my cv", "my portfolio",
        "years of experience", "available for hire",
    ],
}

# Subreddits that exist to host gigs. Elsewhere (discussion-oriented AI subs)
# the bar is raised, because that is where courses, news and rants come from.
GIG_SUBREDDIT_SET = {s.lower() for s in GIG_SUBREDDITS}


# --- Posts that mention AI only to rule it out -------------------------------
# Task subreddits are full of "no AI generated answers, I want a real person".
# Those posts mention ChatGPT precisely because they are NOT AI work.
ANTI_AI_MARKERS = [
    "no ai", "no a.i.", "not ai", "without ai", "no chatgpt", "no gpt",
    "no ai generated", "not ai generated", "no ai-generated",
    "no ai written", "human written", "human-written", "written by a human",
    "real human", "actual human", "real person", "no bots", "no bot",
    "no automation", "no scripts", "manually only", "by hand only",
    "not generated", "no llm",
]

# How far back to look for a negation before an AI term.
AI_NEGATION_WINDOW = 30

# Micro-task subreddits, where a couple of weak words means nothing. A lead
# from these has to name real AI work.
LOW_SIGNAL_SUBREDDITS = {"slavelabour", "donedirtcheap"}

# LeadForge: editable local settings
import json as _lf_json
from pathlib import Path as _lf_Path
_lf_settings = _lf_Path(__file__).parent / "dashboard.config.json"
if _lf_settings.exists():
    for _lf_key, _lf_value in _lf_json.loads(_lf_settings.read_text(encoding="utf-8")).items():
        if _lf_key in ('GIG_SUBREDDITS', 'AI_SUBREDDITS', 'GIG_QUERIES', 'AI_QUERIES', 'GLOBAL_QUERIES', 'AI_TERMS_STRONG', 'AI_TERMS_WEAK', 'HIRING_TERMS', 'SELF_PROMO_TERMS', 'JOB_TERMS', 'REMOTE_TERMS', 'ONSITE_TERMS', 'NEGATION_PREFIXES', 'DEFAULT_MAX_AGE_DAYS', 'DEFAULT_LEAD_COUNT', 'USER_AGENT', 'REQUEST_DELAY_SECONDS', 'MAX_RETRIES', 'DEMAND_STRONG', 'DEMAND_SEEK_VERBS', 'DEMAND_ROLE_NOUNS', 'DEMAND_WINDOW_CHARS', 'ACTIONABLE_CONTACT', 'VETO_PROMO', 'TITLE_NOISE', 'ANTI_AI_MARKERS', 'AI_NEGATION_WINDOW'):
            if type(_lf_value) is not type(globals()[_lf_key]):
                raise ValueError(f"Invalid type for {_lf_key} in dashboard.config.json")
            globals()[_lf_key] = _lf_value

