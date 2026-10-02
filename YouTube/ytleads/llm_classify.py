"""Real content-type classification via free LLMs, with provider failover.

classify.py's keyword scoring still handles exclusion (animation / motion
graphics) -- that gate is fast, free, and already validated against live
data. This module only replaces the CATEGORY assigned to channels that
survive exclusion, using a model that actually reads the channel instead of
counting keyword hits.

Classification here is entirely API-dependent: it does NOT fall back to the
keyword-derived category on failure. Instead it walks an ordered chain --
Groq, Gemini, then every configured OpenRouter key in turn -- moving to the
next attempt on ANY failure (network error, retries exhausted, unparseable
reply). Only once every configured attempt has failed does a channel get
PENDING_CATEGORY instead of a real one; `pipeline.reclassify()` picks up
every pending lead and retries the whole chain again once keys have room.

(classify.method: "keyword" is still a valid, explicit, zero-API mode -- see
resolve_category -- it is just no longer the silent fallback when the LLM
chain fails.)
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, Callable

import requests

from . import classify

if TYPE_CHECKING:
    from .pipeline import Settings

Logger = Callable[[str], None]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

PENDING_CATEGORY = "pending_classification"

_VALID_CATEGORIES = frozenset(classify.all_categories())

_SYSTEM_PROMPT = (
    "You classify YouTube channels for a lead-generation tool. Read the "
    "channel's name, description and recent video titles, then decide what "
    "the channel ACTUALLY uploads -- not just words that happen to appear in "
    "its bio.\n\n"
    "Reply with ONLY a JSON object, nothing else -- no markdown fences, no "
    "explanation:\n"
    '{"category": "<one slug from the list below>"}\n\n'
    "Valid slugs:\n" + ", ".join(classify.all_categories()) + "\n\n"
    'Use "other" only if genuinely nothing on the list fits.'
)

_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


# ── rate limiting ───────────────────────────────────────────────────────
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


# ── provider chain ──────────────────────────────────────────────────────
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


# ── HTTP ─────────────────────────────────────────────────────────────────

def _post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any],
    timeout: int, max_retries: int,
) -> dict[str, Any] | None:
    delay = 1.5
    for attempt in range(max(1, max_retries)):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException:
            if attempt == max_retries - 1:
                return None
            time.sleep(delay)
            delay *= 2
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return None

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_retries - 1:
                return None
            retry_after = resp.headers.get("Retry-After", "")
            wait = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else delay
            time.sleep(min(wait, 30.0))
            delay *= 2
            continue

        # Other 4xx (bad key, bad request shape) -- retrying won't fix it.
        # The chain moves on to the next attempt instead.
        return None
    return None


def _call_openai_compatible(
    url: str, model: str, api_key: str, user_prompt: str, timeout: int, max_retries: int,
) -> str | None:
    """Groq and OpenRouter both speak the OpenAI chat-completions shape."""
    data = _post_json(
        url,
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "max_tokens": 60,
        },
        timeout, max_retries,
    )
    if not data:
        return None
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def _call_groq(model: str, api_key: str, user_prompt: str, timeout: int, max_retries: int) -> str | None:
    return _call_openai_compatible(GROQ_URL, model, api_key, user_prompt, timeout, max_retries)


def _call_openrouter(model: str, api_key: str, user_prompt: str, timeout: int, max_retries: int) -> str | None:
    return _call_openai_compatible(OPENROUTER_URL, model, api_key, user_prompt, timeout, max_retries)


def _call_gemini(model: str, api_key: str, user_prompt: str, timeout: int, max_retries: int) -> str | None:
    data = _post_json(
        GEMINI_URL.format(model=model),
        {"Content-Type": "application/json", "x-goog-api-key": api_key},
        {
            "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 60},
        },
        timeout, max_retries,
    )
    if not data:
        return None
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


# Dispatch by `kind`, not `label` -- every OpenRouter key uses the same HTTP
# function, just a different key value. selftest.py monkeypatches entries
# here to test the chain without any network access.
_CALLERS: dict[str, Callable[[str, str, str, int, int], str | None]] = {
    "groq": _call_groq,
    "gemini": _call_gemini,
    "openrouter": _call_openrouter,
}


# ── response parsing ────────────────────────────────────────────────────

def extract_category_from_text(
    text: str, valid: frozenset[str] | set[str] = _VALID_CATEGORIES
) -> str | None:
    """Pull a validated category slug out of whatever the model replied with.

    Tolerates markdown-fenced JSON and models that ignore the "JSON only"
    instruction and just say the bare word. Anything not in the known
    category list is rejected -- an invented slug would silently break the
    sheet's tab-per-category layout.
    """
    if not text:
        return None

    candidate = ""
    m = _JSON_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(0))
            candidate = str(obj.get("category", "")).strip()
        except (ValueError, AttributeError):
            candidate = ""

    if not candidate:
        stripped = text.strip().strip("`").strip('"').strip("'").strip()
        candidate = stripped.split()[0] if stripped else ""

    candidate = candidate.lower().replace(" ", "_").replace("-", "_").strip("_.")
    return candidate if candidate in valid else None


# ── public API ───────────────────────────────────────────────────────────

def classify_category(
    channel: dict[str, Any], video_titles: list[str], sx: "Settings", log: Logger = print,
) -> tuple[str, str] | None:
    """Walk the full chain -> (category, label that answered), or None if
    every configured attempt failed (or nothing is configured at all)."""
    chain = build_chain(sx)
    if not chain:
        return None

    snippet = channel.get("snippet", {}) or {}
    title = snippet.get("title", "") or ""
    description = (snippet.get("description") or "")[:600]
    titles_block = "\n".join(f"- {t}" for t in video_titles[:12]) or "(none available)"
    user_prompt = (
        f"Channel name: {title}\nDescription: {description}\n"
        f"Recent video titles:\n{titles_block}"
    )

    for kind, label, api_key, model in chain:
        _limiter(label, _rpm_for(sx, kind)).wait()
        caller = _CALLERS[kind]
        try:
            text = caller(model, api_key, user_prompt, sx.llm_timeout, sx.llm_max_retries)
        except Exception as exc:  # noqa: BLE001 -- one bad key must not sink the whole chain
            log(f"    [llm] {label} error on '{title[:40]}': {exc} -- trying next")
            continue

        category = extract_category_from_text(text or "")
        if category is not None:
            return category, label
        log(f"    [llm] {label} gave no usable reply for '{title[:40]}' -- trying next")

    log(f"    [llm] all {len(chain)} attempt(s) failed for '{title[:40]}' -- marking pending")
    return None


def resolve_category(
    sx: "Settings", keyword_category: str, channel: dict[str, Any],
    video_titles: list[str], log: Logger = print,
) -> tuple[str, str]:
    """(category, note-for-the-reason-column). Never raises.

    classify.method == "keyword": returns the keyword-derived category
    directly -- an explicit, deliberate, zero-API mode, not a fallback path.

    classify.method == "llm": walks the full provider chain. If every
    configured attempt fails (or none is configured), returns
    PENDING_CATEGORY rather than quietly reusing the keyword guess -- the
    lead is still saved in full; `python main.py reclassify` retries every
    pending lead once keys have room again.
    """
    if sx.classify_method != "llm":
        return keyword_category, "keyword"

    result = classify_category(channel, video_titles, sx, log)
    if result is None:
        return PENDING_CATEGORY, "pending"
    return result
