"""Run orchestration.

Order matters -- each stage is cheaper than the one it protects:

    discover (100u/page)
      -> drop everything already in the DB            (0 units)
      -> batch channels.list, 50 per unit             (1u / 50)
      -> subscriber-range gate                        (0 units)
      -> uploads playlist, cadence gate               (1u each)
      -> exclusion + classification                   (0 units)
      -> contact extraction (+ optional About page)   (0 units)
      -> persist, then push to Sheets

Rejects are written to the DB too. That is the whole "never revisit" mechanism:
a channel that failed the sub range today does not cost a single unit tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import classify as classifier
from . import contacts as contactlib
from . import discovery, llm_classify, sheets
from .config import Config
from .filters import check_cadence, check_subscribers
from .store import QUALIFIED, Store
from .youtube_api import QuotaExhausted, YouTubeClient

Logger = Callable[[str], None]


@dataclass
class Stats:
    discovered: int = 0
    already_known: int = 0
    fetched: int = 0
    rejected_subs: int = 0
    rejected_few_videos: int = 0
    rejected_inactive: int = 0
    excluded_animation: int = 0
    excluded_motion_graphics: int = 0
    rejected_no_contact: int = 0
    rejected_duplicate_contact: int = 0
    qualified: int = 0
    with_email: int = 0
    pending_classification: int = 0
    quota_used: int = 0
    sheet_rows: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _mark(store: Store, cid: str, status: str, reason: str, via: str,
          extra: dict[str, Any] | None = None) -> None:
    row: dict[str, Any] = {
        "channel_id": cid, "status": status, "reason": reason, "discovered_via": via,
    }
    if extra:
        row.update(extra)
    store.upsert(row)


def _parse_provider_order(raw: Any) -> list[str]:
    """Accepts a YAML list (the normal case) or a comma-separated string
    (in case someone edits config.yaml by hand and gets the syntax wrong)."""
    if isinstance(raw, str):
        return [p.strip().lower() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p).strip().lower() for p in raw if str(p).strip()]
    return ["groq", "gemini", "openrouter"]


@dataclass(frozen=True)
class Settings:
    """Resolved run settings, built once from config + CLI overrides.

    These used to be threaded through as a dozen positional arguments, which
    silently broke the moment one call site gained a parameter and the other
    did not. One object, passed by name.
    """
    min_subs: int
    max_subs: int
    reject_hidden: bool
    min_videos: int
    max_days: int
    max_gap: int
    sample: int
    min_sampled: int
    require_contact: bool
    dedupe_email: bool
    use_about: bool
    about_delay: float
    about_timeout: int
    min_score: float
    exclusion_threshold: float
    exclude_animation: bool
    exclude_motion_graphics: bool
    classify_method: str
    llm_provider_order: list[str]
    groq_api_key: str
    gemini_api_key: str
    openrouter_api_keys: list[str]
    groq_model: str
    gemini_model: str
    openrouter_model: str
    llm_rpm_groq: float
    llm_rpm_gemini: float
    llm_rpm_openrouter: float
    llm_timeout: int
    llm_max_retries: int

    @classmethod
    def from_config(cls, cfg: Config, about_pages: bool | None = None) -> "Settings":
        return cls(
            min_subs=int(cfg.get("filters.min_subscribers", 1000)),
            max_subs=int(cfg.get("filters.max_subscribers", 1_000_000)),
            reject_hidden=bool(cfg.get("filters.reject_hidden_subscribers", True)),
            min_videos=int(cfg.get("filters.min_videos", 5)),
            max_days=int(cfg.get("filters.max_days_since_last_upload", 21)),
            max_gap=int(cfg.get("filters.max_median_gap_days", 21)),
            sample=int(cfg.get("filters.uploads_sample_size", 15)),
            min_sampled=int(cfg.get("filters.min_uploads_sampled", 4)),
            require_contact=bool(cfg.get("filters.require_contact", True)),
            dedupe_email=bool(cfg.get("filters.dedupe_by_email", True)),
            use_about=(about_pages if about_pages is not None
                       else bool(cfg.get("about_page.enabled", True))),
            about_delay=float(cfg.get("about_page.delay_seconds", 1.5)),
            about_timeout=int(cfg.get("about_page.timeout", 15)),
            min_score=float(cfg.get("classify.min_score", 2.0)),
            exclusion_threshold=float(cfg.get("exclude.threshold", 4.0)),
            exclude_animation=bool(cfg.get("exclude.animation", True)),
            exclude_motion_graphics=bool(cfg.get("exclude.motion_graphics", True)),
            classify_method=str(cfg.get("classify.method", "keyword")).lower(),
            llm_provider_order=_parse_provider_order(
                cfg.get("classify.llm_provider_order", ["groq", "gemini", "openrouter"])
            ),
            groq_api_key=cfg.groq_api_key,
            gemini_api_key=cfg.gemini_api_key,
            openrouter_api_keys=cfg.openrouter_api_keys,
            groq_model=str(cfg.get("classify.llm_model_groq", "llama-3.1-8b-instant")),
            gemini_model=str(cfg.get("classify.llm_model_gemini", "gemini-2.0-flash-lite")),
            openrouter_model=str(cfg.get(
                "classify.llm_model_openrouter", "meta-llama/llama-3.3-70b-instruct:free"
            )),
            llm_rpm_groq=float(cfg.get("classify.llm_rpm_groq", 28)),
            llm_rpm_gemini=float(cfg.get("classify.llm_rpm_gemini", 14)),
            llm_rpm_openrouter=float(cfg.get("classify.llm_rpm_openrouter", 18)),
            llm_timeout=int(cfg.get("classify.llm_timeout", 20)),
            llm_max_retries=int(cfg.get("classify.llm_max_retries", 2)),
        )


def run(
    cfg: Config,
    store: Store,
    client: YouTubeClient,
    seeds: list[str],
    max_channels: int | None = None,
    log: Logger = print,
    about_pages: bool | None = None,
    pages_per_seed: int | None = None,
) -> Stats:
    st = Stats()
    sx = Settings.from_config(cfg, about_pages)
    pages = pages_per_seed if pages_per_seed is not None else int(
        cfg.get("discovery.search_pages_per_seed", 1)
    )

    if sx.classify_method == "llm":
        chain = llm_classify.build_chain(sx)
        if chain:
            labels = ", ".join(label for _, label, _, _ in chain)
            log(f"  classification: llm, {len(chain)} attempt(s) in order [{labels}]; "
                f"unclassifiable channels are marked pending, not keyword-guessed")
        else:
            log("  [!] classify.method is 'llm' but no API key is configured "
                "(GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEYS) -- every "
                "channel will be marked pending until you add one.")
    else:
        log("  classification: keyword only")

    # ── stage 1: discovery ────────────────────────────────────────────────
    log("\n[1/4] Discovering candidates")
    candidates: dict[str, str] = {}
    for cid, via in discovery.from_seeds(
        client, store, seeds,
        pages_per_seed=pages,
        published_within_days=int(cfg.get("discovery.video_published_within_days", 21)),
        order=str(cfg.get("discovery.search_order", "relevance")),
        region_code=str(cfg.get("discovery.region_code", "") or ""),
        relevance_language=str(cfg.get("discovery.relevance_language", "") or ""),
        log=log,
    ):
        candidates.setdefault(cid, via)
        if max_channels and len(candidates) >= max_channels:
            log(f"    reached --max-channels {max_channels}")
            break

    st.discovered = len(candidates)
    log(f"    {st.discovered} new candidates (quota used {client.used})")
    if not candidates:
        log("    Nothing new. Either the seeds are exhausted for today or every "
            "result is already in the DB.")
        st.quota_used = client.used
        return st

    qualified_ids = _evaluate(sx, store, client, candidates, st, log)

    # ── stage 3: snowball off the qualified leads ─────────────────────────
    if cfg.get("discovery.snowball", True) and qualified_ids and client.can_afford(50):
        log("\n[3/4] Snowballing from featured channels")
        extra: dict[str, str] = {}
        for cid, via in discovery.snowball(
            client, store, qualified_ids,
            max_per_channel=int(cfg.get("discovery.snowball_max_per_channel", 25)),
            log=log,
        ):
            extra.setdefault(cid, via)
            if max_channels and len(extra) >= max_channels:
                break
        if extra:
            log(f"    {len(extra)} new candidates from featured channels")
            st.discovered += len(extra)
            _evaluate(sx, store, client, extra, st, log, stage="3b")
        else:
            log("    no new channels surfaced")
    else:
        log("\n[3/4] Snowball skipped")

    st.quota_used = client.used
    return st


def _evaluate(
    sx: Settings, store: Store, client: YouTubeClient,
    candidates: dict[str, str], st: Stats, log: Logger,
    stage: str = "2",
) -> list[str]:
    """Fetch, filter, classify and persist a batch. Returns qualified ids."""
    log(f"\n[{stage}/4] Evaluating {len(candidates)} channels")

    try:
        items = client.channels(list(candidates.keys()))
    except QuotaExhausted as exc:
        log(f"[!] {exc}")
        return []

    st.fetched += len(items)
    qualified_ids: list[str] = []

    for idx, ch in enumerate(items, 1):
        cid = ch.get("id", "")
        if not cid:
            continue
        via = candidates.get(cid, "unknown")
        snippet = ch.get("snippet", {}) or {}
        stats_block = ch.get("statistics", {}) or {}
        branding = (ch.get("brandingSettings", {}) or {}).get("channel", {}) or {}
        title = snippet.get("title", "") or ""
        handle = (snippet.get("customUrl", "") or "").lstrip("@")
        description = snippet.get("description") or branding.get("description") or ""

        base: dict[str, Any] = {
            "title": title,
            "handle": handle,
            "url": f"https://www.youtube.com/channel/{cid}",
            "country": snippet.get("country", ""),
            "created_at": snippet.get("publishedAt", ""),
            "view_count": _int(stats_block.get("viewCount")),
            "video_count": _int(stats_block.get("videoCount")),
            "description": description[:1500],
        }

        # -- subscriber range ---------------------------------------------
        subs = check_subscribers(stats_block, sx.min_subs, sx.max_subs, sx.reject_hidden)
        base["subscribers"] = subs.subscribers
        if not subs.ok:
            st.rejected_subs += 1
            _mark(store, cid, "rejected_subs", subs.reason, via, base)
            continue

        if base["video_count"] < sx.min_videos:
            st.rejected_few_videos += 1
            _mark(store, cid, "rejected_few_videos",
                  f"{base['video_count']} videos < {sx.min_videos}", via, base)
            continue

        # -- upload cadence ------------------------------------------------
        uploads_pl = ((ch.get("contentDetails", {}) or {})
                      .get("relatedPlaylists", {}) or {}).get("uploads", "")
        if not uploads_pl:
            st.rejected_inactive += 1
            _mark(store, cid, "rejected_inactive", "no uploads playlist", via, base)
            continue

        if not client.can_afford(1):
            log("[!] Quota exhausted mid-evaluation; stopping cleanly.")
            store.commit()
            break

        try:
            upload_items = client.uploads(uploads_pl, limit=sx.sample)
        except QuotaExhausted as exc:
            log(f"[!] {exc}")
            store.commit()
            break

        cad = check_cadence(upload_items, sx.max_days, sx.max_gap, sx.min_sampled)
        base.update({
            "last_upload": cad.last_upload,
            "days_since_upload": cad.days_since_upload,
            "median_gap_days": cad.median_gap_days,
            "uploads_sampled": cad.sampled,
        })
        if not cad.ok:
            st.rejected_inactive += 1
            _mark(store, cid, "rejected_inactive", cad.reason, via, base)
            continue

        # -- exclusions (keyword-based; fast, free, already validated) -----
        verdict = classifier.classify(
            ch,
            video_titles=cad.video_titles,
            min_score=sx.min_score,
            exclusion_threshold=sx.exclusion_threshold,
            exclude_animation=sx.exclude_animation,
            exclude_motion_graphics=sx.exclude_motion_graphics,
        )
        base["category"] = verdict.category
        base["category_score"] = verdict.score

        if verdict.excluded:
            status = f"excluded_{verdict.exclusion_kind}"
            setattr(st, status, getattr(st, status, 0) + 1)
            _mark(store, cid, status, verdict.detail, via, base)
            continue

        # -- real category: LLM reads the channel. No keyword fallback --
        # a total chain failure marks the lead pending instead ------------
        category, method_note = llm_classify.resolve_category(
            sx, verdict.category, ch, cad.video_titles, log=log
        )
        base["category"] = category
        if method_note == "pending":
            st.pending_classification += 1
        classify_detail = f"{verdict.detail} [{method_note}]"

        # -- contacts ------------------------------------------------------
        about_links: list[str] = []
        if sx.use_about:
            about_links = contactlib.fetch_about_links(
                cid, timeout=sx.about_timeout, delay=sx.about_delay
            )
        contact = contactlib.build_contacts(description, about_links)
        base.update({k: contact.get(k, "") for k in (
            "email", "instagram", "facebook", "twitter", "tiktok",
            "linkedin", "discord", "telegram", "website", "other_links",
        )})

        if sx.require_contact and not contact.has_any():
            st.rejected_no_contact += 1
            _mark(store, cid, "rejected_no_contact", "no email or socials found", via, base)
            continue

        if sx.dedupe_email and contact.get("email"):
            owner = store.email_owner(contact["email"], exclude=cid)
            if owner:
                st.rejected_duplicate_contact += 1
                _mark(store, cid, "rejected_duplicate_contact",
                      f"email already used by {owner}", via, base)
                continue

        st.qualified += 1
        if contact.get("email"):
            st.with_email += 1
        qualified_ids.append(cid)
        _mark(store, cid, QUALIFIED, classify_detail, via, base)

        log(f"    [+] {title[:44]:<44} {subs.subscribers:>9,} subs  "
            f"{category:<20} {contact.get('email') or '(socials only)'}")

        if idx % 25 == 0:
            store.commit()

    store.commit()
    return qualified_ids


def export(
    cfg: Config, store: Store, log: Logger = print, resync_all: bool = False
) -> dict[str, int]:
    """Push unsynced (or all) qualified leads into Google Sheets."""
    leads = store.all_leads() if resync_all else store.unsynced_leads()
    if not leads:
        log("Nothing to export.")
        return {}

    sid = cfg.spreadsheet_id
    if not sid:
        log("[!] SPREADSHEET_ID is not set in .env -- skipping Sheets export.")
        return {}
    creds = cfg.service_account_file
    if not creds.exists():
        log(f"[!] Service account file not found: {creds} -- skipping Sheets export.")
        return {}

    log(f"Exporting {len(leads)} leads to Google Sheets...")
    writer = sheets.SheetsWriter(str(creds), sid)
    now = datetime.now()
    written = sheets.push(
        writer, leads,
        master_tab=str(cfg.get("sheets.master_tab", "Youtube Leads")),
        tab_per_category=bool(cfg.get("sheets.tab_per_category", True)),
        date_key=now.strftime("%B %d, %Y"),
        date_label=now.strftime("%B %d, %Y  -  %I:%M %p"),
    )
    store.mark_synced([l["channel_id"] for l in leads])

    for name, n in sorted(written.items(), key=lambda kv: -kv[1]):
        log(f"    {name:<28} +{n}")
    if not written:
        log("    all rows were already present in the sheet")
    return written


def reclassify(cfg: Config, store: Store, log: Logger = print) -> int:
    """Re-run LLM classification against already-qualified leads.

    Uses each lead's stored title + description only -- video titles are not
    persisted per-lead, so this is a smaller signal than a fresh run gets,
    but still real content understanding instead of a keyword guess. Zero
    YouTube quota spent; this never touches the API.

    Sheets export is append-only (see sheets.py): if a lead was already
    pushed under its old category, this does not move or delete that row.
    Re-running `export` after this adds a corrected row under the new tab
    but leaves the stale one in the old tab -- clean that up by hand.
    """
    sx = Settings.from_config(cfg)
    if sx.classify_method != "llm":
        log("[!] classify.method is not 'llm' in config.yaml -- nothing to backfill against.")
        return 0

    chain = llm_classify.build_chain(sx)
    if not chain:
        log("[!] No API key configured (GROQ_API_KEY / GEMINI_API_KEY / "
            "OPENROUTER_API_KEYS) -- nothing to backfill against.")
        return 0

    leads = store.all_leads()
    if not leads:
        log("No qualified leads in the DB.")
        return 0

    labels = ", ".join(label for _, label, _, _ in chain)
    pending_before = sum(
        1 for l in leads if (l["category"] or "") == llm_classify.PENDING_CATEGORY
    )
    log(f"Reclassifying {len(leads)} leads, {len(chain)} attempt(s) in order [{labels}]"
        + (f" -- {pending_before} currently pending" if pending_before else "") + "...")

    changed = 0
    still_pending = 0
    for i, lead in enumerate(leads, 1):
        channel_stub = {
            "snippet": {"title": lead["title"] or "", "description": lead["description"] or ""}
        }
        old_category = lead["category"] or "other"
        category, method_note = llm_classify.resolve_category(
            sx, old_category, channel_stub, [], log=log
        )
        if category != old_category:
            changed += 1
            reason = f"{lead['reason'] or ''} [reclassified:{method_note}]".strip()
            store.update_category(lead["channel_id"], category, reason)
            log(f"    {(lead['title'] or '')[:40]:<40} {old_category:<24} -> {category}")
        if category == llm_classify.PENDING_CATEGORY:
            still_pending += 1
        if i % 25 == 0:
            store.commit()
            log(f"    ...{i}/{len(leads)} checked, {changed} changed so far")

    store.commit()
    log(f"\n{changed}/{len(leads)} lead(s) got a different category.")
    if still_pending:
        log(f"{still_pending} lead(s) are still pending -- every configured key is still "
            f"failing or rate-limited for them; try again later or add another key.")
    if changed:
        log("Sheets export only appends new rows -- if any of these were already "
            "synced, `export` will add a corrected row under the new tab but "
            "will not remove the stale one from the old tab.")
    return changed


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
