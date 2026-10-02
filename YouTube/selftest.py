"""Offline checks for the parts that do not need the API.

    python selftest.py

Exercises contact extraction, the cadence gate, exclusion rules and the
classifier against hand-built fixtures. Zero quota, zero network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ytleads import llm_classify
from ytleads.classify import classify
from ytleads.contacts import (
    build_contacts, extract_emails, extract_socials, parse_about_html,
)
from ytleads.filters import check_cadence, check_subscribers


def fake_settings(**overrides) -> SimpleNamespace:
    """Duck-typed stand-in for pipeline.Settings -- llm_classify only reads
    a handful of fields off it, so building a real one is unnecessary."""
    base = dict(
        classify_method="llm", llm_provider_order=["groq", "gemini", "openrouter"],
        groq_api_key="", gemini_api_key="", openrouter_api_keys=[],
        groq_model="llama-3.1-8b-instant", gemini_model="gemini-2.0-flash-lite",
        openrouter_model="meta-llama/llama-3.3-70b-instruct:free",
        llm_rpm_groq=6000, llm_rpm_gemini=6000, llm_rpm_openrouter=6000,
        llm_timeout=20, llm_max_retries=2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)

PASS, FAIL = "  ok  ", " FAIL "
_failures = 0


def check(name: str, got, want) -> None:
    global _failures
    ok = got == want
    if not ok:
        _failures += 1
    print(f"[{PASS if ok else FAIL}] {name}")
    if not ok:
        print(f"          got:  {got!r}")
        print(f"          want: {want!r}")


def truthy(name: str, got) -> None:
    global _failures
    if not got:
        _failures += 1
    print(f"[{PASS if got else FAIL}] {name}")
    if not got:
        print(f"          got: {got!r}")


def iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def uploads(*days_ago: float, titles: list[str] | None = None) -> list[dict]:
    titles = titles or []
    out = []
    for i, d in enumerate(days_ago):
        out.append({
            "contentDetails": {"videoPublishedAt": iso(d)},
            "snippet": {"title": titles[i] if i < len(titles) else f"Video {i}"},
        })
    return out


def channel(title="", desc="", keywords="", topics=None) -> dict:
    return {
        "id": "UCtest",
        "snippet": {"title": title, "description": desc},
        "brandingSettings": {"channel": {"keywords": keywords, "description": desc}},
        "topicDetails": {"topicCategories": topics or []},
    }


print("\n--- contact extraction ---")
check("plain email",
      extract_emails("Business: hello@studio.com for collabs"), ["hello@studio.com"])
check("obfuscated (at)/(dot)",
      extract_emails("reach me at john (at) example (dot) org"), ["john@example.org"])
check("obfuscated [at] .",
      extract_emails("mail: contact[at]myshow.tv"), ["contact@myshow.tv"])
check("business email ranks first",
      extract_emails("personal: me@gmail.com\nFor business inquiries: biz@brand.co")[0],
      "biz@brand.co")
check("rejects image filenames", extract_emails("logo@2x.png in header"), [])
check("rejects placeholder domains", extract_emails("you@example.com"), [])

# Regressions from a live run: an undelimited "at" turned ordinary prose into
# addresses. All three of these were emitted as real leads before the fix.
check("prose 'creation. Whether' is not an email",
      extract_emails("video creation. Whether you are new here..."), [])
check("prose 'appreciated. My' is not an email",
      extract_emails("Support is appreciated. My goal is to..."), [])
check("prose 'creator-entrepreneurs. We' is not an email",
      extract_emails("for creator-entrepreneurs. We publish weekly."), [])
check("prose 'available at info.example.com' is not an email",
      extract_emails("Rates available at info.example.com today"), [])
check("real obfuscated address still survives the fix",
      extract_emails("business inquiries: studio at brand dot com"), ["studio@brand.com"])

socials = extract_socials(
    "IG https://www.instagram.com/creator.name/ | FB facebook.com/MyPage "
    "| X https://x.com/handle | TikTok tiktok.com/@tikhandle "
    "| Discord discord.gg/abc123 | https://instagram.com/p/XYZ"
)
check("instagram", socials.get("instagram"), "https://instagram.com/creator.name")
check("facebook", socials.get("facebook"), "https://facebook.com/MyPage")
check("twitter/x", socials.get("twitter"), "https://x.com/handle")
check("tiktok", socials.get("tiktok"), "https://tiktok.com/@tikhandle")
check("discord", socials.get("discord"), "https://discord.gg/abc123")

c = build_contacts(
    "Docs channel. Business: team@docs.tv\nIG: instagram.com/docschannel\n"
    "Site: https://docs.tv/about\nMerch: https://shop.docs.tv",
    about_links=["https://twitter.com/docschannel"],
)
check("merged email", c["email"], "team@docs.tv")
check("merged website", c["website"], "https://docs.tv/about")
check("about-page link merged", c["twitter"], "https://x.com/docschannel")
truthy("has_any() true when contactable", c.has_any())
truthy("has_any() false when empty", not build_contacts("just a description").has_any())

print("\n--- About-page link parsing ---")
# Shape taken from real ytInitialData: "&" arrives as & and "/" is often
# escaped as \/. A regex that forbids backslashes silently truncates the match
# at "redirect?event=..." and never reaches the q= parameter -- which is what
# made live enrichment return zero links for every channel.
ABOUT_FIXTURE = (
    '{"channelExternalLinkViewModel":{"link":{"content":"instagram.com/creator"},'
    '"navigationEndpoint":{"urlEndpoint":{"url":"https://www.youtube.com/redirect?'
    'event=channel_description\\u0026redir_token=ABC123\\u0026q=https%3A%2F%2Fwww.instagram.com%2Fcreator"}}}},'
    '{"url":"https:\\/\\/www.youtube.com\\/redirect?q=https%3A%2F%2Fmystudio.co%2Fwork%3Fref%3Dyt"},'
    '<a href="mailto:hello@mystudio.co">mail</a>'
)
parsed = parse_about_html(ABOUT_FIXTURE)
truthy("unwraps \\u0026-escaped redirect",
       "https://www.instagram.com/creator" in parsed)
truthy("unwraps backslash-escaped slashes",
       "https://mystudio.co/work?ref=yt" in parsed)
truthy("picks up mailto links", "mailto:hello@mystudio.co" in parsed)
truthy("no half-parsed redirect leaks through",
       not any("redirect?" in p for p in parsed))

enriched = build_contacts("A channel about stuff.", parsed)
check("about-page instagram reaches contacts",
      enriched["instagram"], "https://instagram.com/creator")
check("about-page email reaches contacts", enriched["email"], "hello@mystudio.co")

print("\n--- LLM classification: response parsing (no network) ---")
check("clean JSON reply",
      llm_classify.extract_category_from_text('{"category": "vlog"}'), "vlog")
check("markdown-fenced JSON reply",
      llm_classify.extract_category_from_text('```json\n{"category": "gaming"}\n```'), "gaming")
check("bare word fallback when the model ignores the JSON instruction",
      llm_classify.extract_category_from_text("documentary"), "documentary")
check("invented category is rejected, not trusted blindly",
      llm_classify.extract_category_from_text('{"category": "underwater_basket_weaving"}'), None)
check("empty reply -> None", llm_classify.extract_category_from_text(""), None)
check("hyphenated slug is normalized",
      llm_classify.extract_category_from_text('{"category": "true-crime"}'), "true_crime")

print("\n--- LLM classification: provider chain construction (no network) ---")
only_groq_key = fake_settings(groq_api_key="gk", gemini_api_key="", openrouter_api_keys=[])
chain = llm_classify.build_chain(only_groq_key)
check("chain with only a groq key has exactly one attempt",
      [(k, l) for k, l, _, _ in chain], [("groq", "groq")])

no_keys = fake_settings(groq_api_key="", gemini_api_key="", openrouter_api_keys=[])
check("no keys anywhere -> empty chain", llm_classify.build_chain(no_keys), [])

ordered = fake_settings(
    llm_provider_order=["openrouter", "groq", "gemini"],
    groq_api_key="gk", gemini_api_key="gm", openrouter_api_keys=["or1"],
)
chain = llm_classify.build_chain(ordered)
check("chain honors classify.llm_provider_order",
      [k for k, _, _, _ in chain], ["openrouter", "groq", "gemini"])

multi_key = fake_settings(
    llm_provider_order=["openrouter"], openrouter_api_keys=["orA", "orB", "orC"],
)
chain = llm_classify.build_chain(multi_key)
check("each OpenRouter key becomes its own labeled attempt",
      [(k, l, key) for k, l, key, _ in chain],
      [("openrouter", "openrouter#1", "orA"),
       ("openrouter", "openrouter#2", "orB"),
       ("openrouter", "openrouter#3", "orC")])

single_or_key = fake_settings(llm_provider_order=["openrouter"], openrouter_api_keys=["orA"])
check("a single OpenRouter key keeps the plain 'openrouter' label (no #1 noise)",
      llm_classify.build_chain(single_or_key)[0][1], "openrouter")

print("\n--- LLM classification: fallback behavior (no network) ---")
keyword_mode = fake_settings(classify_method="keyword", groq_api_key="gk")
cat, note = llm_classify.resolve_category(keyword_mode, "vlog", {}, [])
check("classify.method=keyword short-circuits before touching any provider", cat, "vlog")
check("...and the note says so", note, "keyword")

no_key_llm_mode = fake_settings(classify_method="llm", groq_api_key="", gemini_api_key="",
                                 openrouter_api_keys=[])
cat, note = llm_classify.resolve_category(no_key_llm_mode, "vlog", {}, [], log=lambda *_: None)
check("llm mode with zero keys configured -> pending, NOT the keyword guess",
      cat, llm_classify.PENDING_CATEGORY)
check("...flagged as pending, not as a keyword fallback", note, "pending")

print("\n--- LLM classification: failover across the whole chain (mocked, no network) ---")
# Simulates: groq returns nothing usable, gemini crashes outright, the first
# OpenRouter key also fails, and the second OpenRouter key finally answers.
# This is the exact scenario the chain exists for -- one bad key (or three)
# must not stop classification as long as one working key is left anywhere
# in the chain.
_original_callers = dict(llm_classify._CALLERS)
try:
    def _flaky_openrouter(model, api_key, user_prompt, timeout, max_retries):
        if api_key == "or-bad":
            return None
        return '{"category": "sports"}'

    llm_classify._CALLERS["groq"] = lambda *a, **k: None
    llm_classify._CALLERS["gemini"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated outage"))
    llm_classify._CALLERS["openrouter"] = _flaky_openrouter

    sx = fake_settings(
        llm_provider_order=["groq", "gemini", "openrouter"],
        groq_api_key="gk", gemini_api_key="gm",
        openrouter_api_keys=["or-bad", "or-good"],
    )
    result = llm_classify.classify_category(
        {"snippet": {"title": "X", "description": "y"}}, [], sx, log=lambda *_: None
    )
    check("falls through a silent miss, a crash, and a bad key to reach a working one",
          result, ("sports", "openrouter#2"))

    llm_classify._CALLERS["openrouter"] = lambda *a, **k: None
    result = llm_classify.classify_category(
        {"snippet": {"title": "X", "description": "y"}}, [], sx, log=lambda *_: None
    )
    check("every attempt in the chain exhausted -> None (caller marks it pending)", result, None)

    cat, note = llm_classify.resolve_category(sx, "other", {"snippet": {}}, [], log=lambda *_: None)
    check("resolve_category turns a fully-exhausted chain into pending, not a keyword guess",
          cat, llm_classify.PENDING_CATEGORY)
finally:
    llm_classify._CALLERS.clear()
    llm_classify._CALLERS.update(_original_callers)

print("\n--- subscriber gate ---")
check("in range", check_subscribers({"subscriberCount": "45000"}, 1000, 1_000_000, True).ok, True)
check("below floor", check_subscribers({"subscriberCount": "800"}, 1000, 1_000_000, True).ok, False)
check("above ceiling", check_subscribers({"subscriberCount": "2500000"}, 1000, 1_000_000, True).ok, False)
check("hidden rejected",
      check_subscribers({"hiddenSubscriberCount": True}, 1000, 1_000_000, True).ok, False)
check("hidden allowed when configured",
      check_subscribers({"hiddenSubscriberCount": True}, 1000, 1_000_000, False).ok, True)

print("\n--- cadence gate (1 upload per 21 days) ---")
weekly = check_cadence(uploads(2, 9, 16, 23, 30, 37), 21, 21, 4)
check("weekly uploader passes", weekly.ok, True)
check("weekly median gap", weekly.median_gap_days, 7.0)

stale = check_cadence(uploads(40, 47, 54, 61), 21, 21, 4)
check("stale channel fails", stale.ok, False)
truthy("stale reason mentions last upload", "last upload" in stale.reason)

erratic = check_cadence(uploads(3, 45, 90, 140, 200), 21, 21, 4)
check("recent but erratic fails", erratic.ok, False)
truthy("erratic reason mentions gap", "median gap" in erratic.reason)

check("too few uploads fails", check_cadence(uploads(1, 5), 21, 21, 4).ok, False)
check("borderline 20d gap passes", check_cadence(uploads(1, 21, 41, 61, 81), 21, 21, 4).ok, True)

print("\n--- exclusions ---")
anim = classify(channel("Toon Tales Animation", "We make 2D animated shorts every week."))
check("animation excluded", anim.excluded, True)
check("animation kind", anim.exclusion_kind, "animation")

mograph = classify(channel(
    "MoGraph Lab",
    "After Effects and Cinema 4D motion graphics tutorials. Kinetic typography, mograph.",
    keywords="motion graphics, after effects, c4d",
))
check("motion graphics excluded", mograph.excluded, True)
check("motion graphics kind", mograph.exclusion_kind, "motion_graphics")

casual = classify(
    channel("Weekly Tech Desk", "Honest smartphone reviews and gadget unboxings."),
    video_titles=["iPhone 17 review", "Best budget laptop", "A quick animation test"],
)
check("stray 'animation' does not exclude", casual.excluded, False)

print("\n--- classification ---")
cases = [
    ("documentary",
     channel("Untold History", "Long-form documentary deep dives into forgotten events."),
     ["The untold story of the 1908 expedition", "What really happened at the summit"]),
    ("vlog",
     channel("Sara Daily", "Daily vlog, day in my life, life updates from Lisbon."),
     ["My morning routine", "A day in my life in Lisbon"]),
    ("gaming",
     channel("PixelRush", "Gameplay, walkthroughs and speedruns."),
     ["Elden Ring boss fight", "Minecraft survival ep 4"]),
    ("fitness",
     channel("IronPath", "Home workout plans and strength training from a personal trainer."),
     ["Full body workout", "How to build muscle"]),
    ("finance",
     channel("Market Notes", "Stock market analysis, investing and dividend portfolios."),
     ["My dividend portfolio", "Stock market outlook"]),
    ("food_cooking",
     channel("Simmer", "Easy recipes and baking from a home kitchen."),
     ["30 minute pasta recipe", "Sourdough baking guide"]),
    ("tech",
     channel("Bench Test", "Hardware reviews, teardowns and pc build guides."),
     ["GPU benchmark 2026", "Budget pc build"]),
]
for want, ch, titles in cases:
    got = classify(ch, video_titles=titles)
    check(f"{want:<14} -> {got.category}", got.category, want)

vague = classify(channel("Random Uploads", "Just stuff I like."))
check("weak signal falls back to other", vague.category, "other")

# Regression: "education" used to swallow anything containing tutorial/guide/
# explained, which was ~40% of a live run.
growth = classify(
    channel("Tube Sensei", "Tips to grow your channel, youtube algorithm and monetization."),
    video_titles=["How I got monetized fast", "YouTube SEO that works"],
)
check("creator-economy channel is not 'education'", growth.category, "youtube_growth")

generic = classify(
    channel("Bench Test", "A complete guide and tutorial series, everything explained."),
)
truthy("generic tutorial words alone do not mean education",
       generic.category != "education")

real_edu = classify(
    channel("IELTS Academy", "Exam preparation and language learning for IELTS and TOEFL."),
    video_titles=["IELTS writing task 2", "Grammar lesson: articles"],
)
check("genuine study channel still classifies as education", real_edu.category, "education")

topical = classify(channel("Kick Off", "Weekly match breakdowns.",
                           topics=["https://en.wikipedia.org/wiki/Association_football"]))
check("topicCategories feed the score", topical.category, "sports")

print()
if _failures:
    print(f"{_failures} check(s) failed.")
    sys.exit(1)
print("All checks passed.")
