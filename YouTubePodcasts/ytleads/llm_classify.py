"""LLM enrichment for podcast leads, with free-provider failover.

`podcast.py`'s evidence scoring is the gate -- it is fast, free, and reads
the signals a model cannot see reliably (platform links, runtime, episode
numbering). This module runs *after* that gate and does the part keyword
matching is bad at:

    * confirm (or dispute) that the channel really is a podcast
    * name the genre
    * name the format (interview / solo / co-hosted / panel / clips / ...)
    * pull the HOST'S NAME out of the bio -- the single most valuable field
      in a cold email, and one no regex gets right
    * name the show's primary language, for a worldwide list

It walks an ordered chain -- Groq, Gemini, then every configured OpenRouter
key in turn -- moving to the next attempt on ANY failure (network error,
retries exhausted, unparseable reply). Only once every configured attempt
has failed does a lead get PENDING_GENRE instead of a real genre; the lead
is still saved in full, and `python main.py reclassify` retries the whole
chain later once keys have room.

The model can veto a lead (`is_podcast: false`), but only against a
LOW/MEDIUM-confidence keyword verdict -- see `resolve()`. A channel that
links its own Spotify show is a podcast whatever the model thinks.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

import requests

from . import podcast

if TYPE_CHECKING:
    from .pipeline import Settings

Logger = Callable[[str], None]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

PENDING_GENRE = podcast.PENDING_GENRE

_VALID_GENRES = frozenset(podcast.all_genres())
_VALID_FORMATS = frozenset(podcast.all_formats())

_SYSTEM_PROMPT = (
    "You profile YouTube channels for a podcast lead-generation tool. Read "
    "the channel's name, description, recent video titles and the supplied "
    "metadata, then answer about what the channel ACTUALLY publishes -- not "
    "just words that happen to appear in its bio.\n\n"
    "Reply with ONLY a JSON object, nothing else -- no markdown fences, no "
    "explanation:\n"
    '{"is_podcast": true, "genre": "<slug>", "format": "<slug>", '
    '"host": "<person or empty>", "language": "<English name of the language>"}\n\n'
    "is_podcast: true if this channel publishes a podcast in any form -- "
    "full video episodes, an interview or talk show, an audio-first show "
    "uploaded to YouTube, OR a channel of clips cut from such a show. "
    "false for vlogs, gaming, tutorials, music, news packages and every "
    "other ordinary YouTube format.\n\n"
    "genre, one of:\n" + ", ".join(podcast.all_genres()) + "\n\n"
    "format, one of:\n" + ", ".join(podcast.all_formats()) + "\n\n"
    "host: the name of the host or hosts if the text states it, otherwise "
    'an empty string. Never guess a name.\n'
    'Use "other" for genre or format only if genuinely nothing fits.'
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# -- rate limiting --------------------------------------------------------
# Keyed by LABEL, not kind -- each OpenRouter key gets its own independent
# limiter so one exhausted key doesn't throttle the others' pacing. The RPM
# ceiling itself still comes from the shared per-kind config value, since
# that's the free-tier limit each individual key is subject to.

class _RateLimiter:
    def __init__(self, rpm: float):
        self.min_interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


_limiters: dict[str, _RateLimiter] = {}


def _limiter(label: str, rpm: float) -> _RateLimiter:
    want = 60.0 / rpm if rpm and rpm > 0 else 0.0
    lim = _limiters.get(label)
    if lim is None or lim.min_interval != want:
        lim = _RateLimiter(rpm)
        _limiters[label] = lim
    return lim


def _rpm_for(sx: "Settings", kind: str) -> float:
    return {
        "groq": sx.llm_rpm_groq,
        "gemini": sx.llm_rpm_gemini,
        "openrouter": sx.llm_rpm_openrouter,
    }.get(kind, 20.0)


# -- provider chain -------------------------------------------------------
# Each entry is (kind, label, api_key, model). `kind` selects which HTTP
# function handles it; `label` is the display/log/rate-limit identity --
# distinct for each OpenRouter key (openrouter#1, openrouter#2, ...) so logs
# say exactly which key failed and multiple keys don't share one rate bucket.

ChainEntry = tuple[str, str, str, str]


def build_chain(sx: "Settings") -> list[ChainEntry]:
    """Every configured attempt, in classify.llm_provider_order. A provider
    is skipped entirely if it has no key; OpenRouter expands to one entry
    per key in OPENROUTER_API_KEYS."""
    chain: list[ChainEntry] = []
    for name in sx.llm_provider_order:
        if name == "groq" and sx.groq_api_key:
            chain.append(("groq", "groq", sx.groq_api_key, sx.groq_model))
        elif name == "gemini" and sx.gemini_api_key:
            chain.append(("gemini", "gemini", sx.gemini_api_key, sx.gemini_model))
        elif name == "openrouter" and sx.openrouter_api_keys:
            multi = len(sx.openrouter_api_keys) > 1
            for i, key in enumerate(sx.openrouter_api_keys, 1):
                label = f"openrouter#{i}" if multi else "openrouter"
                chain.append(("openrouter", label, key, sx.openrouter_model))
    return chain


# -- HTTP -----------------------------------------------------------------

# Every call returns (text, error). `error` is a short human-readable reason
# and is "" on success.
#
# This shape exists because of a real outage: all three providers started
# returning HTTP 404 (every model id had been retired -- Groq's
# llama-3.1-8b-instant, Gemini's 2.0-flash-lite, OpenRouter's free Llama 3.3
# had gone paid), and the only thing the logs said was "gave no usable
# reply" for every channel, seven times over. A dead API key, a retired
# model and a model that answered with nonsense were all indistinguishable.
# The status code was right there and we threw it away. Never again.
Reply = tuple[str | None, str]


def _describe(resp: requests.Response) -> str:
    """A one-line reason from an error response, without dumping the body."""
    detail = ""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message", ""))
            elif isinstance(err, str):
                detail = err
    except ValueError:
        detail = (resp.text or "").strip()
    detail = " ".join(detail.split())[:160]
    return f"HTTP {resp.status_code}" + (f": {detail}" if detail else "")


def _post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any],
    timeout: int, max_retries: int,
) -> tuple[dict[str, Any] | None, str]:
    delay = 1.5
    last = "no attempt made"
    for attempt in range(max(1, max_retries)):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            last = f"network error: {type(exc).__name__}"
            if attempt == max_retries - 1:
                return None, last
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            try:
                return resp.json(), ""
            except ValueError:
                return None, "HTTP 200 but the body was not JSON"

        last = _describe(resp)

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries - 1:
                return None, last
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else delay
            time.sleep(min(wait, 30.0))
            delay *= 2
            continue

        # Other 4xx (bad key, retired model, bad request shape) -- retrying
        # won't fix it. The chain moves on to the next attempt instead.
        return None, last
    return None, last


def _call_openai_compatible(
    url: str, model: str, api_key: str, user_prompt: str, timeout: int,
    max_retries: int, max_tokens: int,
) -> Reply:
    """Groq and OpenRouter both speak the OpenAI chat-completions shape."""
    data, err = _post_json(
        url,
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        },
        timeout, max_retries,
    )
    if not data:
        return None, err
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None, "unexpected response shape"
    if not (text or "").strip():
        # Reasoning models burn the token budget on hidden thinking and then
        # have nothing left for the answer. This is exactly what a too-small
        # max_tokens looks like from the outside, so say so.
        reason = str(choice.get("finish_reason") or "")
        return None, ("empty reply"
                      + (f" (finish_reason={reason}; raise classify.llm_max_tokens)"
                         if reason == "length" else " (try raising classify.llm_max_tokens)"))
    return text, ""


def _call_groq(model: str, api_key: str, user_prompt: str, timeout: int,
               max_retries: int, max_tokens: int) -> Reply:
    return _call_openai_compatible(
        GROQ_URL, model, api_key, user_prompt, timeout, max_retries, max_tokens
    )


def _call_openrouter(model: str, api_key: str, user_prompt: str, timeout: int,
                     max_retries: int, max_tokens: int) -> Reply:
    return _call_openai_compatible(
        OPENROUTER_URL, model, api_key, user_prompt, timeout, max_retries, max_tokens
    )


def _call_gemini(model: str, api_key: str, user_prompt: str, timeout: int,
                 max_retries: int, max_tokens: int) -> Reply:
    data, err = _post_json(
        GEMINI_URL.format(model=model),
        {"Content-Type": "application/json", "x-goog-api-key": api_key},
        {
            "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens},
        },
        timeout, max_retries,
    )
    if not data:
        return None, err
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"], ""
    except (KeyError, IndexError, TypeError):
        blocked = ((data.get("promptFeedback") or {}).get("blockReason")
                   or (data.get("candidates") or [{}])[0].get("finishReason"))
        return None, f"no text in reply ({blocked})" if blocked else "unexpected response shape"


# Dispatch by `kind`, not `label` -- every OpenRouter key uses the same HTTP
# function, just a different key value. selftest.py monkeypatches entries
# here to test the chain without any network access.
_CALLERS: dict[str, Callable[..., Reply]] = {
    "groq": _call_groq,
    "gemini": _call_gemini,
    "openrouter": _call_openrouter,
}


# -- response parsing -----------------------------------------------------

class PodcastInfo(NamedTuple):
    is_podcast: bool | None       # None = the model didn't say
    genre: str
    fmt: str
    host: str
    language: str


def _slug(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_").strip("_.")


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    s = str(value or "").strip().lower()
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    return None


def parse_reply(text: str) -> PodcastInfo | None:
    """Pull a validated profile out of whatever the model replied with.

    Tolerates markdown-fenced JSON and trailing prose. An invented genre or
    format slug is dropped rather than trusted -- a made-up slug would
    silently create a junk tab in the spreadsheet. A reply that parses but
    carries no usable field at all counts as a failure, so the chain moves
    on to the next provider instead of banking an empty answer.
    """
    if not text:
        return None
    # Reasoning models emit their scratchpad first. Strip it, or a stray "{"
    # inside the thinking hijacks the greedy JSON match below.
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"^\s*<think>.*", " ", text, flags=re.DOTALL | re.I)
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (ValueError, AttributeError):
        return None
    if not isinstance(obj, dict):
        return None

    genre = _slug(obj.get("genre"))
    fmt = _slug(obj.get("format"))
    is_pod = _as_bool(obj.get("is_podcast"))

    genre = genre if genre in _VALID_GENRES else ""
    fmt = fmt if fmt in _VALID_FORMATS else ""

    host = str(obj.get("host") or "").strip()[:80]
    if host.lower() in ("unknown", "n/a", "none", "not stated", "empty"):
        host = ""
    language = str(obj.get("language") or "").strip()[:32]
    if language.lower() in ("unknown", "n/a", "none"):
        language = ""

    if is_pod is None and not genre and not fmt and not host:
        return None
    return PodcastInfo(is_pod, genre, fmt, host, language)


# -- public API -----------------------------------------------------------

def analyze(
    channel: dict[str, Any],
    video_titles: list[str],
    facts: str,
    sx: "Settings",
    log: Logger = print,
) -> tuple[PodcastInfo, str] | None:
    """Walk the full chain -> (profile, label that answered), or None if
    every configured attempt failed (or nothing is configured at all).

    `facts` is a short pre-computed line of hard metadata (median runtime,
    platform links, cadence). Handing the model the numbers it cannot
    observe is what stops it calling a 90-minute interview show a "vlog".
    """
    chain = build_chain(sx)
    if not chain:
        return None

    snippet = channel.get("snippet", {}) or {}
    title = snippet.get("title", "") or ""
    description = (snippet.get("description") or "")[:800]
    titles_block = "\n".join(f"- {t}" for t in video_titles[:12]) or "(none available)"
    user_prompt = (
        f"Channel name: {title}\n"
        f"Description: {description}\n"
        f"Metadata: {facts}\n"
        f"Recent video titles:\n{titles_block}"
    )

    for kind, label, api_key, model in chain:
        if _dead(label):
            continue
        _limiter(label, _rpm_for(sx, kind)).wait()
        caller = _CALLERS[kind]
        try:
            text, err = caller(model, api_key, user_prompt, sx.llm_timeout,
                               sx.llm_max_retries, sx.llm_max_tokens)
        except Exception as exc:  # noqa: BLE001 -- one bad key must not sink the chain
            _note_failure(label, f"{type(exc).__name__}: {exc}", model, log)
            continue

        info = parse_reply(text or "")
        if info is not None:
            _revive(label)
            return info, label
        _note_failure(label, err or "replied, but with nothing usable in it", model, log)

    return None


# -- provider health ------------------------------------------------------
# A retired model id or a revoked key fails identically for every single
# channel. Logging that seven times per channel across a 30-lead run buries
# the one line that matters in ~200 lines of noise, which is exactly what
# happened in production. So: report each provider's failure ONCE with the
# real reason, then stop calling it for the rest of the run.

_FAILURES: dict[str, int] = {}
_REPORTED: set[str] = set()
DEAD_AFTER = 3          # consecutive failures before a provider is benched


def _note_failure(label: str, reason: str, model: str, log: Logger) -> None:
    _FAILURES[label] = _FAILURES.get(label, 0) + 1
    if label not in _REPORTED:
        _REPORTED.add(label)
        log(f"    [llm] {label} failing -- {reason}  (model: {model})")
    if _FAILURES[label] == DEAD_AFTER:
        log(f"    [llm] {label} failed {DEAD_AFTER}x in a row -- skipping it for the "
            f"rest of this run. Run `main.py test-llm` to see why.")


def _dead(label: str) -> bool:
    return _FAILURES.get(label, 0) >= DEAD_AFTER


def _revive(label: str) -> None:
    """A success clears the strike count -- a provider that recovers from a
    rate-limit blip must not stay benched for the whole run."""
    if _FAILURES.pop(label, 0):
        _REPORTED.discard(label)


def reset_health() -> None:
    """Forget which providers were benched. Called between runs and by tests."""
    _FAILURES.clear()
    _REPORTED.clear()


def health_report() -> str:
    """One line summarising which providers gave up this run, for the summary."""
    dead = sorted(l for l in _FAILURES if _dead(l))
    return ", ".join(dead)


def resolve(
    sx: "Settings",
    verdict: podcast.PodcastVerdict,
    channel: dict[str, Any],
    video_titles: list[str],
    facts: str = "",
    log: Logger = print,
) -> tuple[PodcastInfo, str]:
    """(profile, note-for-the-reason-column). Never raises.

    There is always an offline answer. `offline_profile()` reads the genre
    off the keyword verdict and pulls the host name and language straight
    out of the channel's own title and bio, so a lead is never left blank
    just because an API is down.

    classify.method == "keyword": returns that offline profile directly --
    an explicit, deliberate, zero-API mode.

    classify.method == "llm": walks the provider chain and merges its answer
    OVER the offline one, field by field, so a model that fills in only two
    of four fields still improves the lead and never erases a good offline
    value with a blank. If every configured attempt fails, the offline
    profile stands (note "offline"); set classify.fallback_to_keyword: false
    to get PENDING_GENRE instead and backfill later with `reclassify`.

    The model's `is_podcast: false` is honoured only when the heuristic
    verdict was not high-confidence. A channel that links its own Spotify
    show, or has "Podcast" in its name, stays a lead regardless.
    """
    heuristic = offline_profile(verdict, channel)

    if sx.classify_method != "llm":
        return heuristic, "keyword"

    result = analyze(channel, video_titles, facts, sx, log)
    if result is None:
        if getattr(sx, "llm_fallback_keyword", True):
            return heuristic, "offline"
        return heuristic._replace(genre=PENDING_GENRE), "pending"

    info, label = result
    # Merge, never overwrite with a blank: the model wins where it answered,
    # the offline read fills every gap it left.
    merged = PodcastInfo(
        is_podcast=info.is_podcast,
        genre=info.genre or heuristic.genre,
        fmt=info.fmt or heuristic.fmt,
        host=info.host or heuristic.host,
        language=info.language or heuristic.language,
    )

    if info.is_podcast is False and verdict.confidence == "high":
        # Hard evidence beats the model. Keep the lead, record the argument.
        return merged._replace(is_podcast=True), f"{label}:disputed-kept"

    return merged, label


def offline_profile(
    verdict: podcast.PodcastVerdict, channel: dict[str, Any]
) -> PodcastInfo:
    """Everything we can say about a show without calling anything.

    Genre and format come from the evidence scoring already done in
    podcast.py; host and language are read out of the channel's own title
    and description. This is the floor under every lead -- it costs nothing,
    needs no key, and cannot rate-limit.
    """
    snippet = channel.get("snippet", {}) or {}
    branding = (channel.get("brandingSettings", {}) or {}).get("channel", {}) or {}
    title = snippet.get("title", "") or ""
    description = snippet.get("description") or branding.get("description") or ""
    return PodcastInfo(
        is_podcast=True,
        genre=verdict.genre,
        fmt=verdict.fmt,
        host=podcast.guess_host(title, description),
        language=podcast.guess_language(title, description),
    )
