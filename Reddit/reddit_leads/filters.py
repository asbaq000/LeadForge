"""Classification rules: is this post AI work, is it paid, and is it remote?

Matching is word-boundary based so that "ai" does not fire on "said" or
"maintain", and onsite detection is negation-aware so that a post saying
"remote, no onsite" is not thrown away as an onsite job.
"""

import re
import time

from . import config

_WORD_CACHE = {}


def _pattern(term):
    """Compile a word-boundary pattern for a term, tolerating inner spacing."""
    if term not in _WORD_CACHE:
        escaped = re.escape(term).replace(r"\ ", r"\s+")
        # \b does not work next to punctuation like "[hiring]" or "a.i.", so
        # only anchor the sides that start/end with a word character.
        left = r"\b" if term[:1].isalnum() else ""
        right = r"\b" if term[-1:].isalnum() else ""
        _WORD_CACHE[term] = re.compile(left + escaped + right, re.IGNORECASE)
    return _WORD_CACHE[term]


def normalize(text):
    """Lowercase and collapse whitespace so patterns match across line breaks."""
    return re.sub(r"\s+", " ", (text or "").replace("’", "'")).strip().lower()


def find_terms(text, terms):
    """Return the terms that appear in text, longest match first."""
    hits = []
    for term in terms:
        if _pattern(term).search(text):
            hits.append(term)
    # Prefer the most specific phrasing when terms overlap ("ai engineer" > "ai").
    hits.sort(key=len, reverse=True)
    return hits


def _is_negated(text, match_start):
    """True when a negation word sits just before an onsite mention."""
    window = text[max(0, match_start - 30):match_start]
    for negation in config.NEGATION_PREFIXES:
        if re.search(r"\b" + re.escape(negation) + r"\b[\s\-–—,:()\"']*$", window):
            return True
    return False


def onsite_signals(text):
    """Onsite/hybrid terms that are *not* negated. Any hit disqualifies a lead."""
    signals = []
    for term in config.ONSITE_TERMS:
        for match in _pattern(term).finditer(text):
            if not _is_negated(text, match.start()):
                signals.append(term)
                break
    return signals


def remote_signals(text):
    """Explicit remote/work-from-anywhere evidence."""
    return find_terms(text, config.REMOTE_TERMS)


def classify_location(text, subreddit):
    """Decide remote vs onsite.

    Returns (is_remote, evidence, reason). The rule is deliberately strict:
    a lead must show positive remote evidence AND carry no live onsite marker.
    Silence is not treated as remote, except in subreddits whose own rules
    make every post remote.
    """
    onsite = onsite_signals(text)
    if onsite:
        return False, "", "onsite/hybrid marker: " + ", ".join(onsite[:3])

    remote = remote_signals(text)
    if remote:
        return True, ", ".join(remote[:3]), ""

    if subreddit.lower() in config.REMOTE_BY_DEFAULT_SUBREDDITS:
        return True, "r/%s is remote-only by subreddit rule" % subreddit, ""

    return False, "", "no explicit remote statement"


def _negated_nearby(text, start, window=config.AI_NEGATION_WINDOW):
    """True when a negation word appears shortly before this position.

    Looser than the onsite check, which only looks at the immediately
    preceding word: "do not use ChatGPT" and "without using AI" both put
    words between the negation and the term.
    """
    before = text[max(0, start - window):start]
    for negation in config.NEGATION_PREFIXES:
        if re.search(r"\b" + re.escape(negation) + r"\b", before):
            return True
    return False


def find_terms_unnegated(text, terms):
    """Terms that appear in text without a negation just before them."""
    found = []
    for term in terms:
        for match in _pattern(term).finditer(text):
            if not _negated_nearby(text, match.start()):
                found.append(term)
                break
    found.sort(key=len, reverse=True)
    return found


def anti_ai_markers(text):
    """Phrases saying the poster wants AI kept out of the work."""
    return find_terms(text, config.ANTI_AI_MARKERS)


def ai_relevance(text):
    """Score how much this post is about AI work.

    Strong terms stand on their own; weak terms only add once a strong term
    has already established that the post is AI work. Negated mentions do not
    count at all - "do not use ChatGPT" is a post about anything but AI.
    """
    strong = find_terms_unnegated(text, config.AI_TERMS_STRONG)
    weak = find_terms_unnegated(text, config.AI_TERMS_WEAK)
    score = len(strong) * 3
    if strong:
        score += min(len(weak), 4)
    elif len(weak) >= 2:
        # No strong term, but several weak ones ("ai" + "automation" + "n8n").
        score += 2
    return score, strong, weak


def is_ai_related(text):
    score, strong, _ = ai_relevance(text)
    return bool(strong) or score >= 2


def hiring_signals(text):
    return find_terms(text, config.HIRING_TERMS)


def self_promo_signals(text):
    """Detect posts where the author is selling, not buying."""
    return find_terms(text, config.SELF_PROMO_TERMS)


def is_seeking_work(title, body):
    """True for [FOR HIRE]-style posts, which are not leads for us."""
    title_n = normalize(title)
    if re.match(r"^\s*[\[\(]?\s*(for\s*hire|available|hire\s*me)\b", title_n):
        return True
    if find_terms(title_n, ["[for hire]", "[forhire]", "[available]", "hire me"]):
        return True
    # A body that is all self-promotion with no hiring intent.
    body_n = normalize(body)
    if self_promo_signals(body_n) and not hiring_signals(title_n + " " + body_n):
        return True
    return False


PAY_PATTERN = re.compile(
    r"(\$\s?\d[\d,.]*\s*(?:k|,000)?(?:\s*(?:-|–|to)\s*\$?\s?\d[\d,.]*\s*k?)?"
    r"(?:\s*(?:/|per\s+)(?:hr|hour|h|day|week|month|mo|year|yr|project))?"
    r"|\d[\d,.]*\s*(?:usd|eur|gbp|cad|aud)\b"
    r"|\b\d{2,3}\s*(?:/|per\s+)(?:hr|hour)\b)",
    re.IGNORECASE,
)


def extract_pay(text):
    """Pull the first money-looking string out of the post, if any."""
    match = PAY_PATTERN.search(text or "")
    if not match:
        return ""
    # The number pattern happily swallows a sentence-ending "." or ",".
    return re.sub(r"\s+", " ", match.group(0)).strip().rstrip(".,")


def lead_type(text):
    """Label the lead as an employment-style job or a one-off project/gig."""
    return "job" if find_terms(text, config.JOB_TERMS) else "project/gig"


def age_days(created_utc, now=None):
    now = time.time() if now is None else now
    return (now - float(created_utc)) / 86400.0


def is_recent(created_utc, max_age_days=config.DEFAULT_MAX_AGE_DAYS, now=None):
    age = age_days(created_utc, now)
    return 0 <= age <= max_age_days


# --- Demand intent -----------------------------------------------------------

HIRING_TAG_RE = re.compile(r"^\s*[\[\(]?\s*hiring\b|\[\s*hiring\s*\]|\(\s*hiring\s*\)",
                           re.IGNORECASE)


def has_hiring_tag(title):
    """True for [Hiring]-tagged or "Hiring: ..."-style titles."""
    return bool(HIRING_TAG_RE.search(normalize(title)))


def demand_patterns(text):
    """A seek verb followed closely by a role noun.

    Catches "looking for a remote n8n automation freelancer", which no fixed
    phrase can match because of the words wedged in the middle.
    """
    found = []
    for verb in config.DEMAND_SEEK_VERBS:
        for match in _pattern(verb).finditer(text):
            window = text[match.end():match.end() + config.DEMAND_WINDOW_CHARS]
            roles = find_terms(window, config.DEMAND_ROLE_NOUNS)
            if roles:
                found.append("%s ... %s" % (verb, roles[0]))
                break
    return found


def demand_signals(text):
    """Phrases showing the poster is themselves hiring or commissioning work."""
    return find_terms(text, config.DEMAND_STRONG) + demand_patterns(text)


def actionable_signals(text):
    """Phrases giving a concrete way to take the work forward."""
    return find_terms(text, config.ACTIONABLE_CONTACT)


def promo_veto(text):
    """Course ads and referral spam, rejected wherever they appear."""
    return find_terms(text, config.VETO_PROMO)


def title_noise(title_text):
    """Classify a title as news, venting, showcase, advice or job-seeking."""
    for category, terms in config.TITLE_NOISE.items():
        hits = find_terms(title_text, terms)
        if hits:
            return category, hits
    return None, []


def classify_intent(title, body, subreddit):
    """Decide whether a post is a real gig from the person offering it.

    Returns (is_lead, evidence, reason). The gates, in order:
      1. no course/referral promotion anywhere;
      2. the title must not announce a news, venting, showcase, advice or
         job-seeking post - the title is what says which kind of post it is;
      3. the poster must show first-person hiring intent;
      4. there must be an actionable hook - money, a contact route, or an
         explicit [Hiring] tag;
      5. outside the gig subreddits the bar is higher: money or a [Hiring]
         tag, since that is where courses, news and rants come from.
    """
    title_n = normalize(title)
    text = normalize(title + " . " + body)

    promo = promo_veto(text)
    if promo:
        return False, "", "promotional/course content (%s)" % ", ".join(promo[:2])

    category, noise_hits = title_noise(title_n)
    if category:
        return False, "", "%s post, not a gig (title: %s)" % (category, noise_hits[0])

    demand = demand_signals(text)
    if not demand:
        return False, "", "no first-person hiring intent"

    pay = extract_pay(title + " " + body)
    tagged = has_hiring_tag(title)
    contact = actionable_signals(text)

    if not (pay or tagged or contact):
        return False, "", "hiring intent but no budget, contact route or [Hiring] tag"

    if subreddit.lower() not in config.GIG_SUBREDDIT_SET and not (pay or tagged):
        return False, "", "discussion subreddit without a stated budget or [Hiring] tag"

    evidence = demand[:3]
    if pay:
        evidence.append("pays %s" % pay)
    elif contact:
        evidence.append(contact[0])
    return True, "; ".join(evidence), ""


def classify_ai_work(text, subreddit):
    """Decide whether the post is genuinely about AI work.

    Returns (is_ai, evidence, reason).
    """
    strong = find_terms_unnegated(text, config.AI_TERMS_STRONG)

    # A post can legitimately say "no AI-written cover letters" while still
    # being an AI job, so this only rejects when nothing real is named.
    if not strong:
        excluded = anti_ai_markers(text)
        if excluded:
            return False, "", "post rules AI out (%s)" % excluded[0]

    if subreddit.lower() in config.LOW_SIGNAL_SUBREDDITS and not strong:
        return False, "", "micro-task subreddit with no specific AI work named"

    if not is_ai_related(text):
        return False, "", "not AI-related"

    weak = find_terms_unnegated(text, config.AI_TERMS_WEAK)
    return True, ", ".join((strong or weak)[:4]), ""
