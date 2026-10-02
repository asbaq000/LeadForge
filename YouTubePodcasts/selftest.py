"""Offline checks for the parts that do not need the API.

    python selftest.py

Exercises the podcast gate, contact + platform extraction, the cadence gate,
episode-shape maths, the LLM reply parser and the sheet layout against
hand-built fixtures. Zero quota, zero network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ytleads import llm_classify, podcast, sheets
from ytleads.contacts import (
    build_contacts, extract_emails, extract_socials, parse_about_html,
)
from ytleads.filters import check_cadence, check_subscribers

# This suite prints Arabic, Japanese, Russian and Chinese show names. On a
# default Windows console that is cp1252, and an unguarded print() dies with
# UnicodeEncodeError before it can report a single result.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def fake_settings(**overrides) -> SimpleNamespace:
    """Duck-typed stand-in for pipeline.Settings -- llm_classify only reads
    a handful of fields off it, so building a real one is unnecessary."""
    base = dict(
        classify_method="llm", llm_provider_order=["groq", "gemini", "openrouter"],
        groq_api_key="", gemini_api_key="", openrouter_api_keys=[],
        groq_model="llama-3.1-8b-instant", gemini_model="gemini-2.0-flash-lite",
        openrouter_model="meta-llama/llama-3.3-70b-instruct:free",
        llm_rpm_groq=6000, llm_rpm_gemini=6000, llm_rpm_openrouter=6000,
        llm_timeout=20, llm_max_retries=2, llm_max_tokens=900,
        llm_fallback_keyword=True,
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
            "contentDetails": {"videoPublishedAt": iso(d), "videoId": f"v{i}"},
            "snippet": {"title": titles[i] if i < len(titles) else f"Video {i}"},
        })
    return out


def channel(title="", desc="", keywords="", handle="") -> dict:
    return {
        "id": "UCtest",
        "snippet": {"title": title, "description": desc, "customUrl": handle},
        "brandingSettings": {"channel": {"keywords": keywords, "description": desc}},
    }


def vid(duration: str, views: int = 0) -> dict:
    return {"contentDetails": {"duration": duration}, "statistics": {"viewCount": str(views)}}


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

print("\n--- podcast distribution + booking links ---")
pod_contacts = build_contacts(
    "The Longform Show. Business: team@longform.fm\n"
    "Listen: https://open.spotify.com/show/7abc | "
    "https://podcasts.apple.com/us/podcast/longform/id123\n"
    "Be a guest: https://calendly.com/longform/guest\n"
    "Support us: https://patreon.com/longform\n"
    "Feed: https://feeds.buzzsprout.com/98765.rss\n"
    "Site: https://longform.fm"
)
check("spotify show link", pod_contacts["spotify"], "https://open.spotify.com/show/7abc")
check("apple podcasts link",
      pod_contacts["apple_podcasts"], "https://podcasts.apple.com/us/podcast/longform/id123")
check("rss feed", pod_contacts["rss_feed"], "https://feeds.buzzsprout.com/98765.rss")
check("guest booking form", pod_contacts["booking_link"], "https://calendly.com/longform/guest")
check("membership link", pod_contacts["membership_link"], "https://patreon.com/longform")
check("the show's real site wins the Website column -- not Spotify or Calendly",
      pod_contacts["website"], "https://longform.fm")

check("a Spotify ARTIST link is a musician, not a podcast",
      podcast.detect_platforms(["https://open.spotify.com/artist/xyz"]), {})
check("a Spotify SHOW link is a podcast",
      list(podcast.detect_platforms(["https://open.spotify.com/show/xyz"])), ["spotify"])

truthy("a booking form alone counts as contactable",
       build_contacts("Apply to be a guest: https://podmatch.com/guest/theshow").has_any())
truthy("a Spotify link alone does NOT count as contactable",
       not build_contacts("https://open.spotify.com/show/7abc").has_any())

print("\n--- About-page link parsing ---")
# Shape taken from real ytInitialData: "&" arrives as & and "/" is often
# escaped as \/. A regex that forbids backslashes silently truncates the match
# at "redirect?event=..." and never reaches the q= parameter -- which is what
# made live enrichment return zero links for every channel.
ABOUT_FIXTURE = (
    '{"channelExternalLinkViewModel":{"link":{"content":"instagram.com/creator"},'
    '"navigationEndpoint":{"urlEndpoint":{"url":"https://www.youtube.com/redirect?'
    'event=channel_description\\u0026redir_token=ABC123\\u0026q=https%3A%2F%2Fwww.instagram.com%2Fcreator"}}}},'
    '{"url":"https:\\/\\/www.youtube.com\\/redirect?q=https%3A%2F%2Fopen.spotify.com%2Fshow%2F4xyz"},'
    '<a href="mailto:hello@mystudio.co">mail</a>'
)
parsed = parse_about_html(ABOUT_FIXTURE)
truthy("unwraps \\u0026-escaped redirect",
       "https://www.instagram.com/creator" in parsed)
truthy("unwraps backslash-escaped slashes",
       "https://open.spotify.com/show/4xyz" in parsed)
truthy("picks up mailto links", "mailto:hello@mystudio.co" in parsed)
truthy("no half-parsed redirect leaks through",
       not any("redirect?" in p for p in parsed))

enriched = build_contacts("A show about stuff.", parsed)
check("about-page instagram reaches contacts",
      enriched["instagram"], "https://instagram.com/creator")
check("about-page email reaches contacts", enriched["email"], "hello@mystudio.co")
check("about-page Spotify reaches the podcast column -- the whole reason the "
      "About fetch runs before the gate",
      enriched["spotify"], "https://open.spotify.com/show/4xyz")

print("\n--- episode shape (durations + reach) ---")
check("ISO duration H/M/S", podcast.parse_duration("PT1H23M4S"), 83.07)
check("ISO duration minutes only", podcast.parse_duration("PT45M"), 45.0)
check("ISO duration seconds only", podcast.parse_duration("PT58S"), 0.97)
check("unparseable duration", podcast.parse_duration("garbage"), 0.0)

shape = podcast.episode_shape([
    vid("PT1H12M", 9000), vid("PT58M", 11000), vid("PT1H30M", 7000),
    vid("PT45S", 40000), vid("PT52S", 61000),
])
check("shorts excluded from the median so a clipping show still reads long-form",
      shape.median_minutes, 72.0)
check("longest episode", shape.longest_minutes, 90.0)
check("shorts share reported separately", shape.shorts_ratio, 0.4)
check("average views across everything sampled", shape.avg_views, 25600)
check("no parseable videos -> everything blank",
      podcast.episode_shape([]).median_minutes, None)

print("\n--- podcast gate: things that ARE podcasts ---")
named = podcast.evaluate(channel("The Deep End Podcast", "Weekly conversations."))
truthy("'Podcast' in the channel name passes on its own", named.is_podcast)

platform_only = podcast.evaluate(
    channel("The Deep End", "A weekly show."),
    links=["https://open.spotify.com/show/9zz"],
)
truthy("a Spotify show link passes on its own, with no keyword anywhere",
       platform_only.is_podcast)

unbranded = podcast.evaluate(
    channel("The Ravi Kapoor Show", "Long conversations with founders and athletes."),
    video_titles=[
        "Ep 41 - Building a company from a garage w/ Priya Nair",
        "Ep 40 - ft. Daniel Okoro on leaving finance",
        "Ep 39 - with Dr. Lena Fischer",
        "Ep 38 - The comeback story ft. Marco Silva",
    ],
    median_minutes=78.0,
)
truthy("an interview show that never says 'podcast' still qualifies on evidence",
       unbranded.is_podcast)
check("...and reads as an interview show", unbranded.fmt, "interview")

foreign = podcast.evaluate(channel("بودكاست الحكاية", "حلقات أسبوعية"))
truthy("Arabic 'بودكاست' in the name qualifies", foreign.is_podcast)
truthy("Japanese 'ポッドキャスト' qualifies",
       podcast.evaluate(channel("週刊ポッドキャスト", "毎週配信")).is_podcast)
truthy("Russian 'подкаст' qualifies",
       podcast.evaluate(channel("Подкаст о жизни", "Разговоры")).is_podcast)
truthy("Chinese '播客' qualifies",
       podcast.evaluate(channel("科技播客", "每周更新")).is_podcast)

clips = podcast.evaluate(
    channel("Deep End Clips", "Best moments from The Deep End Podcast."),
    median_minutes=2.0, shorts_ratio=0.7,
)
truthy("a clips channel is still a podcaster lead", clips.is_podcast)
check("...filed as clips, not as a full show", clips.fmt, "clips")

print("\n--- podcast gate: things that are NOT podcasts ---")
gaming = podcast.evaluate(
    channel("PixelRush", "Gameplay, walkthroughs and speedruns."),
    video_titles=["Elden Ring boss fight #12", "Minecraft survival #11",
                  "GTA heist #10", "Valorant ranked #9"],
    median_minutes=18.0,
)
truthy("numbered gaming uploads do not make a podcast", not gaming.is_podcast)

# Regression from a full-pipeline dry run: episode numbering (+4) plus a
# mid-form median (+2) cleared the score bar all by itself, and a Let's Play
# channel came out the far end labelled a podcast. Both signals are
# format-agnostic, so neither can qualify a channel without something that
# actually says "show" somewhere.
lets_play = podcast.evaluate(
    channel("PixelRush", "Gameplay and speedruns. biz@pixelrush.gg"),
    video_titles=["Elden Ring boss #12", "Minecraft survival #11", "GTA heist #10"] * 2,
    median_minutes=22.0,
)
truthy("numbering + a 22-minute median is NOT enough on its own",
       not lets_play.is_podcast)

# ...but the same two signals at genuine long-form length are, which is what
# keeps unbranded interview shows titled just "#412 - Guest Name" in the list.
longform_series = podcast.evaluate(
    channel("Fridman Talks", "Conversations about science and power."),
    video_titles=["#412 - Ada Okonkwo", "#411 - Ben Carter", "#410 - Yuki Tanaka",
                  "#409 - Maria Rossi"],
    median_minutes=140.0,
)
truthy("a 140-minute numbered series qualifies even with no podcast wording",
       longform_series.is_podcast)

vlog = podcast.evaluate(
    channel("Sara Daily", "Daily vlog, day in my life, life updates from Lisbon."),
    video_titles=["My morning routine", "A day in my life in Lisbon",
                  "What I eat in a day", "Weekend reset"],
    median_minutes=12.0,
)
truthy("a vlog channel is rejected", not vlog.is_podcast)

tutorial = podcast.evaluate(
    channel("Bench Test", "Hardware reviews, teardowns and pc build guides."),
    video_titles=["GPU benchmark 2026", "Budget pc build", "SSD teardown"],
    median_minutes=14.0,
)
truthy("a review channel is rejected", not tutorial.is_podcast)

music = podcast.evaluate(
    channel("Nova Beats", "New singles every month."),
    links=["https://open.spotify.com/artist/abc", "https://open.spotify.com/track/xyz"],
)
truthy("a musician's Spotify artist page does not fake a podcast",
       not music.is_podcast)

print("\n--- podcast gate: confidence + strict mode ---")
strong = podcast.evaluate(
    channel("The Deep End Podcast", "Weekly interviews.",
            keywords="podcast, interviews"),
    links=["https://open.spotify.com/show/9zz"],
    video_titles=["Ep 12 ft. Amara Nwosu", "Ep 11 w/ Tom Barrett"],
    median_minutes=64.0,
)
check("name + platform + numbering + runtime = high confidence",
      strong.confidence, "high")
truthy("evidence-only qualification is not marked high confidence",
       unbranded.confidence != "high")

print("\n--- genre + format guessing (keyword mode) ---")
genre_cases = [
    ("true_crime", "Cold Case Files Podcast",
     "Unsolved murder cases and cold case investigations, weekly."),
    ("business_entrepreneurship", "Founder Notes Podcast",
     "Interviews with startup founders about scaling a business."),
    ("mental_health", "The Therapy Room Podcast",
     "A therapist on anxiety, trauma and burnout."),
    ("finance_investing", "Dividend Desk Podcast",
     "Personal finance, investing and dividend portfolios."),
    ("comedy", "Two Idiots Podcast",
     "A comedy podcast by two stand up comedians."),
    ("spirituality_religion", "Morning Faith Podcast",
     "Bible study and sermons for a christian podcast audience."),
]
for want, title, desc in genre_cases:
    got, _ = podcast.guess_genre(title, "", desc, [])
    check(f"genre {want:<26} -> {got}", got, want)

vague_genre, _ = podcast.guess_genre("The Show", "", "We talk about things.", [])
check("weak genre signal falls back to other", vague_genre, "other")

check("long solo uploads with no guests read as solo",
      podcast.guess_format("The Daily Note", "A solo episode every morning.",
                           ["Monday thoughts", "Tuesday thoughts"], 25.0, 0.0),
      "solo")
check("long uploads with no other signal read as a video podcast",
      podcast.guess_format("The Show", "Weekly.", ["Part one", "Part two"], 55.0, 0.0),
      "video_first")

print("\n--- LLM reply parsing (no network) ---")
good = llm_classify.parse_reply(
    '{"is_podcast": true, "genre": "true_crime", "format": "narrative", '
    '"host": "Maria Lopez", "language": "Spanish"}'
)
check("is_podcast parsed", good.is_podcast, True)
check("genre parsed", good.genre, "true_crime")
check("format parsed", good.fmt, "narrative")
check("host parsed", good.host, "Maria Lopez")
check("language parsed", good.language, "Spanish")

fenced = llm_classify.parse_reply(
    '```json\n{"is_podcast": false, "genre": "gaming", "format": "clips", '
    '"host": "", "language": "English"}\n```'
)
check("markdown-fenced reply still parses", fenced.is_podcast, False)

hyphen = llm_classify.parse_reply('{"is_podcast": true, "genre": "true-crime", "format": "solo"}')
check("hyphenated slug is normalized", hyphen.genre, "true_crime")

invented = llm_classify.parse_reply(
    '{"is_podcast": true, "genre": "underwater_basket_weaving", "format": "podcasty"}'
)
check("invented genre is dropped, not trusted", invented.genre, "")
check("invented format is dropped, not trusted", invented.fmt, "")
truthy("...but a usable is_podcast still comes through", invented.is_podcast is True)

placeholder = llm_classify.parse_reply(
    '{"is_podcast": true, "genre": "comedy", "format": "solo", "host": "Unknown"}'
)
check("'Unknown' host is treated as no host, not as a name", placeholder.host, "")

check("string booleans are accepted",
      llm_classify.parse_reply('{"is_podcast": "true", "genre": "comedy"}').is_podcast, True)
check("empty reply -> None", llm_classify.parse_reply(""), None)
check("prose with no JSON -> None", llm_classify.parse_reply("I think it is a podcast."), None)
check("JSON with nothing usable in it -> None (chain moves on)",
      llm_classify.parse_reply('{"note": "not sure"}'), None)

print("\n--- LLM provider chain construction (no network) ---")
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
check("chain honors classify.llm_provider_order",
      [k for k, _, _, _ in llm_classify.build_chain(ordered)],
      ["openrouter", "groq", "gemini"])

multi_key = fake_settings(
    llm_provider_order=["openrouter"], openrouter_api_keys=["orA", "orB", "orC"],
)
check("each OpenRouter key becomes its own labeled attempt",
      [(k, l, key) for k, l, key, _ in llm_classify.build_chain(multi_key)],
      [("openrouter", "openrouter#1", "orA"),
       ("openrouter", "openrouter#2", "orB"),
       ("openrouter", "openrouter#3", "orC")])

single_or_key = fake_settings(llm_provider_order=["openrouter"], openrouter_api_keys=["orA"])
check("a single OpenRouter key keeps the plain 'openrouter' label (no #1 noise)",
      llm_classify.build_chain(single_or_key)[0][1], "openrouter")

print("\n--- LLM resolve(): fallback behaviour (no network) ---")
KEYWORD_VERDICT = podcast.PodcastVerdict(
    True, 9.0, "high", "interview", "comedy", 4.0, {}, [], "fixture"
)

keyword_mode = fake_settings(classify_method="keyword", groq_api_key="gk")
info, note = llm_classify.resolve(keyword_mode, KEYWORD_VERDICT, {}, [])
check("classify.method=keyword short-circuits before touching any provider",
      info.genre, "comedy")
check("...and the note says so", note, "keyword")

no_key_llm_mode = fake_settings(classify_method="llm", groq_api_key="",
                                gemini_api_key="", openrouter_api_keys=[])
info, note = llm_classify.resolve(no_key_llm_mode, KEYWORD_VERDICT,
                                  {"snippet": {"title": "Matt Beall Podcast"}}, [],
                                  log=lambda *_: None)
check("llm mode with zero keys falls back to the offline profile", note, "offline")
check("...keeping a real genre rather than parking at pending",
      info.genre, "comedy")
check("...and still reading the host name straight off the title",
      info.host, "Matt Beall")
truthy("...and the lead is still treated as a podcast", info.is_podcast)

strict = fake_settings(classify_method="llm", groq_api_key="", gemini_api_key="",
                       openrouter_api_keys=[], llm_fallback_keyword=False)
info, note = llm_classify.resolve(strict, KEYWORD_VERDICT, {}, [], log=lambda *_: None)
check("fallback_to_keyword=false still parks the lead at pending",
      info.genre, podcast.PENDING_GENRE)
check("...flagged as pending", note, "pending")

print("\n--- LLM failover across the whole chain (mocked, no network) ---")
# Simulates: groq returns nothing usable, gemini crashes outright, the first
# OpenRouter key also fails, and the second OpenRouter key finally answers.
# This is the exact scenario the chain exists for -- one bad key (or three)
# must not stop enrichment as long as one working key is left in the chain.
_original_callers = dict(llm_classify._CALLERS)
try:
    def _flaky_openrouter(model, api_key, user_prompt, timeout, max_retries, max_tokens):
        if api_key == "or-bad":
            return None, "HTTP 401: invalid key"
        return ('{"is_podcast": true, "genre": "sports", "format": "panel", '
                '"host": "Ade Musa"}'), ""

    llm_classify.reset_health()
    llm_classify._CALLERS["groq"] = lambda *a, **k: (None, "HTTP 404: model retired")
    llm_classify._CALLERS["gemini"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated outage"))
    llm_classify._CALLERS["openrouter"] = _flaky_openrouter

    sx = fake_settings(
        llm_provider_order=["groq", "gemini", "openrouter"],
        groq_api_key="gk", gemini_api_key="gm",
        openrouter_api_keys=["or-bad", "or-good"],
    )
    result = llm_classify.analyze(
        {"snippet": {"title": "X", "description": "y"}}, [], "", sx, log=lambda *_: None
    )
    truthy("falls through a silent miss, a crash and a bad key to a working one",
           result is not None)
    check("...and reports which attempt answered", result[1], "openrouter#2")
    check("...with the profile intact", (result[0].genre, result[0].host),
          ("sports", "Ade Musa"))

    llm_classify.reset_health()
    llm_classify._CALLERS["openrouter"] = lambda *a, **k: (None, "HTTP 429: rate limited")
    check("every attempt exhausted -> None (caller falls back)",
          llm_classify.analyze({"snippet": {}}, [], "", sx, log=lambda *_: None), None)

    llm_classify.reset_health()
    info, note = llm_classify.resolve(sx, KEYWORD_VERDICT, {"snippet": {}}, [],
                                      log=lambda *_: None)
    check("resolve() falls back to the offline profile when the chain dies",
          (info.genre, note), ("comedy", "offline"))

    # A provider failing identically for every channel must be reported once
    # and then benched, not logged 30 times.
    llm_classify.reset_health()
    seen = []
    for _ in range(6):
        llm_classify.analyze({"snippet": {"title": "X"}}, [], "", sx, log=seen.append)
    # One "failing" line per ATTEMPT IN THE CHAIN, total -- not one per
    # attempt per channel. The production log had 7 lines x 30 channels.
    check("a dead provider is reported once, not once per channel",
          sum(1 for line in seen if "failing --" in line),
          len(llm_classify.build_chain(sx)))
    truthy("...and is benched after repeated failure",
           any("skipping it for the rest of this run" in line for line in seen))
    truthy("health_report names the benched providers",
           "groq" in llm_classify.health_report())

    # A model vetoing a channel that links its own Spotify feed must lose.
    llm_classify.reset_health()
    llm_classify._CALLERS["openrouter"] = lambda *a, **k: ('{"is_podcast": false, "genre": "gaming"}', "")
    info, note = llm_classify.resolve(sx, KEYWORD_VERDICT, {"snippet": {}}, [],
                                      log=lambda *_: None)
    truthy("an llm veto cannot overrule high-confidence hard evidence",
           info.is_podcast is True)
    truthy("...and the disagreement is recorded", "disputed" in note)

    low_conf = KEYWORD_VERDICT._replace(confidence="low")
    info, note = llm_classify.resolve(sx, low_conf, {"snippet": {}}, [],
                                      log=lambda *_: None)
    check("an llm veto DOES stand against a low-confidence guess",
          info.is_podcast, False)
finally:
    llm_classify._CALLERS.clear()
    llm_classify._CALLERS.update(_original_callers)

print("\n--- subscriber gate ---")
check("in range", check_subscribers({"subscriberCount": "45000"}, 500, 500_000, True).ok, True)
check("below floor", check_subscribers({"subscriberCount": "300"}, 500, 500_000, True).ok, False)
check("above ceiling", check_subscribers({"subscriberCount": "2500000"}, 500, 500_000, True).ok, False)
check("hidden rejected",
      check_subscribers({"hiddenSubscriberCount": True}, 500, 500_000, True).ok, False)
check("hidden allowed when configured",
      check_subscribers({"hiddenSubscriberCount": True}, 500, 500_000, False).ok, True)

print("\n--- cadence gate (podcast defaults: 45d fresh, 30d median gap) ---")
weekly = check_cadence(uploads(2, 9, 16, 23, 30, 37), 45, 30, 4)
check("weekly show passes", weekly.ok, True)
check("weekly median gap", weekly.median_gap_days, 7.0)
check("weekly rate in episodes/month", weekly.episodes_per_month, 4.3)
check("video ids come through for the durations call", weekly.video_ids[:2], ["v0", "v1"])

fortnightly = check_cadence(uploads(5, 19, 33, 47, 61), 45, 30, 4)
check("a fortnightly show passes -- it would have failed the old 21d gate",
      fortnightly.ok, True)

stale = check_cadence(uploads(60, 74, 88, 102), 45, 30, 4)
check("abandoned show fails", stale.ok, False)
truthy("stale reason mentions last upload", "last upload" in stale.reason)

erratic = check_cadence(uploads(3, 45, 90, 140, 200), 45, 30, 4)
check("recent but erratic fails", erratic.ok, False)
truthy("erratic reason mentions gap", "median gap" in erratic.reason)

check("too few uploads fails", check_cadence(uploads(1, 5), 45, 30, 4).ok, False)

print("\n--- sheet + CSV layout ---")
check("every header has a column width",
      len(sheets.COLUMN_WIDTHS), len(sheets.HEADERS))
check("ID_COL really points at Channel ID",
      sheets.HEADERS[sheets.ID_COL - 1], "Channel ID")

row = sheets.row_from_lead({
    "title": "The Deep End Podcast", "host_name": "Ravi Kapoor",
    "subscribers": 55600, "genre": "true_crime", "podcast_format": "interview",
    "channel_id": "UC123", "shorts_ratio": 0.42, "email": "team@deepend.fm",
})
check("a row is exactly as wide as the header", len(row), len(sheets.HEADERS))
check("channel id lands in the dedupe column", row[sheets.ID_COL - 1], "UC123")
check("genre renders as a human label", row[3], "True Crime")
check("format renders as a human label", row[4], "Interview")
check("shorts share renders as a percentage", row[26], "42%")
check("CSV keeps the raw subscriber number for sorting", row[2], 55600)
check("the sheet renders subscribers for humans",
      sheets.row_from_lead({"subscribers": 55600}, for_sheet=True)[2], "55.6k")
check("a blank shorts ratio stays blank, not 0%",
      sheets.row_from_lead({"title": "x"})[26], "")
truthy("every genre slug has a sheet label",
       all(g in sheets.GENRE_LABELS for g in podcast.all_genres()))
truthy("every format slug has a sheet label",
       all(f in sheets.FORMAT_LABELS for f in podcast.all_formats()))

print("\n--- per-run lead isolation ---")
# Each run must hand back only what IT found. If this ever regresses, every
# run's CSV starts repeating the previous run's leads, which is exactly the
# thing the run_id stamp exists to prevent.
import tempfile
from pathlib import Path as _Path

from ytleads.store import QUALIFIED, Store

_db = _Path(tempfile.mkdtemp()) / "isolation.db"
with Store(_db) as _store:
    r1 = _store.start_run()
    for n in range(3):
        _store.upsert({"channel_id": f"UC_r1_{n}", "status": QUALIFIED,
                       "title": f"Show {n}", "subscribers": 1000 + n})
    _store.commit()

    r2 = _store.start_run()
    for n in range(2):
        _store.upsert({"channel_id": f"UC_r2_{n}", "status": QUALIFIED,
                       "title": f"Later Show {n}", "subscribers": 2000 + n})
    # A reject in run 2 must not show up in run 2's lead list.
    _store.upsert({"channel_id": "UC_r2_bad", "status": "rejected_not_podcast",
                   "title": "Not A Podcast"})
    _store.commit()

    check("run 1 sees only its own leads",
          sorted(l["channel_id"] for l in _store.leads_for_run(r1)),
          ["UC_r1_0", "UC_r1_1", "UC_r1_2"])
    check("run 2 sees only its own leads -- none of run 1's",
          sorted(l["channel_id"] for l in _store.leads_for_run(r2)),
          ["UC_r2_0", "UC_r2_1"])
    check("rejects never reach a run's lead list",
          [l for l in _store.leads_for_run(r2) if l["channel_id"] == "UC_r2_bad"], [])
    check("last_run_id() finds the most recent run that produced leads",
          _store.last_run_id(), r2)
    check("all_leads() still spans every run", len(_store.all_leads()), 5)
    check("an unknown run id yields nothing rather than the whole table",
          _store.leads_for_run(9999), [])
    check("a None run id yields nothing rather than the whole table",
          _store.leads_for_run(None), [])

print()
if _failures:
    print(f"{_failures} check(s) failed.")
    sys.exit(1)
print("All checks passed.")
