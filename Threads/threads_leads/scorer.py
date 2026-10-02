"""Lead qualification: turn raw post text into a 0-100 score plus explainable tags.

The whole point is separating people who WANT to buy AI/automation work from the
enormous volume of people SELLING it. Seller patterns carry a large negative
weight for exactly that reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Settings

# Threads posts are short; a post with almost no words can't demonstrate intent.
MIN_WORDS = 4

# Cheap normalisation so regexes see predictable text.
_WS = re.compile(r"\s+")
_ZERO_WIDTH = re.compile(r"[​-‏﻿]")


# Handles routinely contain our own keywords - "@ai.sahilbeniwal" made a Shopify
# request look like an AI job. Collapse mentions to a neutral token so they stop
# triggering domain matches, while still marking the post as a reply.
_MENTION = re.compile(r"@[A-Za-z0-9._]{2,30}")


def normalize(text: str) -> str:
    text = _ZERO_WIDTH.sub("", text or "")
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = _MENTION.sub("@user", text)
    return _WS.sub(" ", text).strip()


@dataclass
class Verdict:
    score: int
    level: str                 # HOT / WARM / COOL / REJECT
    signals: list[str]         # which scoring groups fired
    matched_terms: list[str]   # the actual phrases that fired, for auditing
    topics: list[str]          # topic tags for triage
    reject_reason: str = ""


def _level_for(score: int) -> str:
    if score >= 75:
        return "HOT"
    if score >= 60:
        return "WARM"
    return "COOL"


class LeadScorer:
    def __init__(self, settings: Settings):
        self.s = settings

    def topics_for(self, text: str) -> list[str]:
        low = text.lower()
        hits = []
        for tag, needles in self.s.topic_tags.items():
            if any(n in low for n in needles):
                hits.append(tag)
        return hits

    def score(self, text: str) -> Verdict:
        clean = normalize(text)

        if len(clean.split()) < MIN_WORDS:
            return Verdict(0, "REJECT", [], [], [], "too short")

        total = 0
        signals: list[str] = []
        matched_terms: list[str] = []
        positive_hit = False

        for group in self.s.score_groups:
            group_matches = []
            for pat in group.patterns:
                m = pat.search(clean)
                if m:
                    group_matches.append(m.group(0).strip()[:40])
            if not group_matches:
                continue

            total += group.weight
            signals.append(group.name)
            matched_terms.extend(group_matches[:3])
            if group.weight > 0:
                positive_hit = True

        # A post must touch our service domain at all - otherwise a generic
        # "looking for someone" (a plumber, a roommate) would sneak through.
        if "domain" not in signals:
            return Verdict(0, "REJECT", signals, matched_terms, [], "not in AI/automation domain")

        # And it must show some form of intent, not just mention the topic.
        if not ({"intent_high", "intent_medium"} & set(signals)):
            return Verdict(0, "REJECT", signals, matched_terms, [], "no buying/help intent")

        if not positive_hit:
            return Verdict(0, "REJECT", signals, matched_terms, [], "negative signals only")

        score = max(0, min(100, total))
        topics = self.topics_for(clean)

        if score < self.s.min_score:
            return Verdict(score, "REJECT", signals, matched_terms, topics, "below min_score")

        return Verdict(score, _level_for(score), signals, matched_terms, topics)
