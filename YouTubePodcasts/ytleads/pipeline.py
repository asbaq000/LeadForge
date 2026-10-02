"""Run orchestration.

Order matters -- each stage is cheaper than the one it protects:

    discover (100u/page)
      -> drop everything already in the DB              (0 units)
      -> batch channels.list, 50 per unit               (1u / 50)
      -> subscriber-range gate                          (0 units)
      -> uploads playlist, cadence gate                 (1u each)
      -> videos.list: runtimes + per-episode views      (1u each)
      -> PODCAST GATE, first pass on description alone  (0 units)
      -> About page (only if it could change the verdict, or the
         channel already passed and we want its links)  (0 units, plain HTTP)
      -> PODCAST GATE, final pass with every link       (0 units)
      -> LLM profile: genre, format, host, language     (0 YouTube units)
      -> contact extraction + dedupe                    (0 units)
      -> persist, then push to CSV + Sheets

The podcast gate deliberately sits AFTER runtime and About-page enrichment,
because the two strongest podcast signals -- a link to a Spotify/Apple show
and a long median runtime -- are only visible once those have run. Cheap
obvious non-podcasts are still cut before the HTTP hit: a channel whose
first-pass score is far below the bar never gets an About fetch.

Rejects are written to the DB too. That is the whole "never revisit"
mechanism: a channel that failed the podcast gate today does not cost a
single unit tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from . import contacts as contactlib
from . import discovery, llm_classify, podcast, sheets
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
    rejected_not_podcast: int = 0
    rejected_no_contact: int = 0
    rejected_duplicate_contact: int = 0
    qualified: int = 0
    with_email: int = 0
    with_platform: int = 0
    with_booking: int = 0
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
    # -- podcast gate --
    podcast_min_score: float
    podcast_borderline_score: float
    require_platform_or_name: bool
    honour_llm_veto: bool
    genre_min_score: float
    episode_stats: bool
    min_median_minutes: float
    # -- llm --
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
    llm_max_tokens: int
    llm_fallback_keyword: bool

    @classmethod
    def from_config(cls, cfg: Config, about_pages: bool | None = None) -> "Settings":
        min_score = float(cfg.get("podcast.min_score", 5.0))
        return cls(
            min_subs=int(cfg.get("filters.min_subscribers", 500)),
            max_subs=int(cfg.get("filters.max_subscribers", 500_000)),
            reject_hidden=bool(cfg.get("filters.reject_hidden_subscribers", True)),
            min_videos=int(cfg.get("filters.min_videos", 5)),
            max_days=int(cfg.get("filters.max_days_since_last_upload", 45)),
            max_gap=int(cfg.get("filters.max_median_gap_days", 30)),
            sample=int(cfg.get("filters.uploads_sample_size", 20)),
            min_sampled=int(cfg.get("filters.min_uploads_sampled", 4)),
            require_contact=bool(cfg.get("filters.require_contact", True)),
            dedupe_email=bool(cfg.get("filters.dedupe_by_email", True)),
            use_about=(about_pages if about_pages is not None
                       else bool(cfg.get("about_page.enabled", True))),
            about_delay=float(cfg.get("about_page.delay_seconds", 1.5)),
            about_timeout=int(cfg.get("about_page.timeout", 15)),
            podcast_min_score=min_score,
            podcast_borderline_score=float(
                cfg.get("podcast.borderline_score", max(0.0, min_score - 3.5))
            ),
            require_platform_or_name=bool(
                cfg.get("podcast.require_platform_or_name", False)
            ),
            honour_llm_veto=bool(cfg.get("podcast.honour_llm_veto", True)),
            genre_min_score=float(cfg.get("podcast.genre_min_score", 2.0)),
            episode_stats=bool(cfg.get("podcast.episode_stats", True)),
            min_median_minutes=float(cfg.get("podcast.min_median_minutes", 0.0)),
            classify_method=str(cfg.get("classify.method", "llm")).lower(),
            llm_provider_order=_parse_provider_order(
                cfg.get("classify.llm_provider_order", ["groq", "gemini", "openrouter"])
            ),
            groq_api_key=cfg.groq_api_key,
            gemini_api_key=cfg.gemini_api_key,
            openrouter_api_keys=cfg.openrouter_api_keys,
            groq_model=str(cfg.get("classify.llm_model_groq", "openai/gpt-oss-20b")),
            gemini_model=str(cfg.get("classify.llm_model_gemini", "gemini-flash-lite-latest")),
            openrouter_model=str(cfg.get(
                "classify.llm_model_openrouter", "nvidia/nemotron-nano-9b-v2:free"
            )),
            llm_rpm_groq=float(cfg.get("classify.llm_rpm_groq", 28)),
            llm_rpm_gemini=float(cfg.get("classify.llm_rpm_gemini", 14)),
            llm_rpm_openrouter=float(cfg.get("classify.llm_rpm_openrouter", 18)),
            llm_timeout=int(cfg.get("classify.llm_timeout", 20)),
            llm_max_retries=int(cfg.get("classify.llm_max_retries", 2)),
            llm_max_tokens=int(cfg.get("classify.llm_max_tokens", 900)),
            llm_fallback_keyword=bool(cfg.get("classify.fallback_to_keyword", True)),
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
    stop_at: int | None = None,
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
            log(f"  enrichment: llm, {len(chain)} attempt(s) in order [{labels}]; "
                f"unprofiled leads are marked pending, not keyword-guessed")
        else:
            log("  [!] classify.method is 'llm' but no API key is configured "
                "(GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEYS) -- genre "
                "and host name will be keyword-only until you add one.")
    else:
        log("  enrichment: keyword only (no host names, weaker genres)")

    # -- stage 1: discovery ------------------------------------------------
    log("\n[1/4] Discovering candidates")
    candidates: dict[str, str] = {}
    for cid, via in discovery.from_seeds(
        client, store, seeds,
        pages_per_seed=pages,
        published_within_days=int(cfg.get("discovery.video_published_within_days", 45)),
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

    qualified_ids = _evaluate(sx, store, client, candidates, st, log, stop_at=stop_at)

    # -- stage 3: snowball off the qualified leads -------------------------
    # Podcasters feature other podcasters -- guests' own shows, network
    # siblings, friends' shows. This is by far the cheapest source of
    # on-target candidates once a run has found its first few.
    if stop_at is not None and st.qualified >= stop_at:
        log(f"\n[3/4] Snowball skipped -- this run already has its {stop_at} leads")
    elif cfg.get("discovery.snowball", True) and qualified_ids and client.can_afford(50):
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
            _evaluate(sx, store, client, extra, st, log, stage="3b", stop_at=stop_at)
        else:
            log("    no new channels surfaced")
    else:
        log("\n[3/4] Snowball skipped")

    st.quota_used = client.used
    return st


def _facts_line(
    shape: podcast.EpisodeShape, verdict: podcast.PodcastVerdict,
    per_month: float | None, subs: int,
) -> str:
    """The hard numbers the model cannot observe, handed to it as one line.

    Without this a free 8B model happily calls a 90-minute weekly interview
    show a "vlog"; with it, the runtime does the arguing.
    """
    bits = [f"{subs:,} subscribers"]
    if shape.median_minutes:
        bits.append(f"median upload {shape.median_minutes:.0f} min")
    if shape.longest_minutes:
        bits.append(f"longest {shape.longest_minutes:.0f} min")
    if shape.shorts_ratio:
        bits.append(f"{shape.shorts_ratio:.0%} of uploads are shorts")
    if per_month:
        bits.append(f"~{per_month:.1f} uploads/month")
    if verdict.platforms:
        bits.append("links to " + ", ".join(sorted(verdict.platforms)))
    return "; ".join(bits)


def _evaluate(
    sx: Settings, store: Store, client: YouTubeClient,
    candidates: dict[str, str], st: Stats, log: Logger,
    stage: str = "2", stop_at: int | None = None,
) -> list[str]:
    """Fetch, filter, profile and persist a batch. Returns qualified ids.

    `stop_at` caps how many leads this pass may produce. Without it a single
    search page hands back 50 candidates and a run asking for 30 comes back
    with 80 -- quota and LLM calls spent on leads you did not ask for.

    Candidates still queued when the cap is hit are simply never evaluated,
    so they are never written to the DB, stay "unknown", and get picked up
    by the NEXT run instead of being burned. That is what makes each run's
    output a clean, non-overlapping batch.
    """
    log(f"\n[{stage}/4] Evaluating {len(candidates)} channels")

    try:
        items = client.channels(list(candidates.keys()))
    except QuotaExhausted as exc:
        log(f"[!] {exc}")
        return []

    st.fetched += len(items)
    qualified_ids: list[str] = []

    for idx, ch in enumerate(items, 1):
        if stop_at is not None and st.qualified >= stop_at:
            log(f"    target reached: {st.qualified} lead(s) this run -- "
                f"leaving the remaining candidates for the next one")
            break
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

        if not client.can_afford(2):
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
            "episodes_per_month": cad.episodes_per_month,
            "uploads_sampled": cad.sampled,
        })
        if not cad.ok:
            st.rejected_inactive += 1
            _mark(store, cid, "rejected_inactive", cad.reason, via, base)
            continue

        # -- episode shape: runtime + reach (1 unit per channel) -----------
        shape = podcast.EpisodeShape(None, None, 0.0, None, 0)
        if sx.episode_stats and cad.video_ids and client.can_afford(1):
            try:
                shape = podcast.episode_shape(client.videos(cad.video_ids[:50]))
            except QuotaExhausted as exc:
                log(f"[!] {exc}")
                store.commit()
                break
        base.update({
            "median_episode_minutes": shape.median_minutes,
            "longest_episode_minutes": shape.longest_minutes,
            "shorts_ratio": shape.shorts_ratio,
            "avg_episode_views": shape.avg_views,
        })

        # -- podcast gate, first pass (description links only) -------------
        verdict = podcast.evaluate(
            ch,
            video_titles=cad.video_titles,
            links=contactlib.all_urls(description),
            median_minutes=shape.median_minutes,
            shorts_ratio=shape.shorts_ratio,
            min_score=sx.podcast_min_score,
            genre_min_score=sx.genre_min_score,
        )

        # -- About page: the second half of the evidence -------------------
        # Fetched when the channel already looks like a podcast (we want its
        # links for the lead) or when it is close enough that an About-only
        # Spotify link would flip the verdict. Hopeless candidates never
        # cost an HTTP round trip.
        about_links: list[str] = []
        borderline = verdict.score >= sx.podcast_borderline_score
        if sx.use_about and (verdict.is_podcast or borderline):
            about_links = contactlib.fetch_about_links(
                cid, timeout=sx.about_timeout, delay=sx.about_delay
            )

        contact = contactlib.build_contacts(description, about_links)

        if about_links:
            verdict = podcast.evaluate(
                ch,
                video_titles=cad.video_titles,
                links=list(contact.get("links") or []),
                median_minutes=shape.median_minutes,
                shorts_ratio=shape.shorts_ratio,
                min_score=sx.podcast_min_score,
                genre_min_score=sx.genre_min_score,
            )

        base.update({
            "podcast_score": verdict.score,
            "podcast_confidence": verdict.confidence,
            "podcast_signals": ", ".join(verdict.signals)[:400],
            "podcast_format": verdict.fmt,
            "genre": verdict.genre,
            "genre_score": verdict.genre_score,
        })

        if not verdict.is_podcast:
            st.rejected_not_podcast += 1
            _mark(store, cid, "rejected_not_podcast",
                  f"not a podcast: {verdict.detail}", via, base)
            continue

        if sx.require_platform_or_name and verdict.confidence == "low":
            st.rejected_not_podcast += 1
            _mark(store, cid, "rejected_not_podcast",
                  f"strict mode, no name/platform proof: {verdict.detail}", via, base)
            continue

        if sx.min_median_minutes and (shape.median_minutes or 0) < sx.min_median_minutes \
                and verdict.fmt != "clips":
            st.rejected_not_podcast += 1
            _mark(store, cid, "rejected_not_podcast",
                  f"median upload {shape.median_minutes}min < "
                  f"{sx.min_median_minutes}min", via, base)
            continue

        # -- LLM profile: genre, format, host, language (0 YouTube units) --
        facts = _facts_line(shape, verdict, cad.episodes_per_month, subs.subscribers)
        info, note = llm_classify.resolve(
            sx, verdict, ch, cad.video_titles, facts=facts, log=log
        )
        if note == "pending":
            st.pending_classification += 1

        if sx.honour_llm_veto and info.is_podcast is False:
            st.rejected_not_podcast += 1
            _mark(store, cid, "rejected_not_podcast",
                  f"llm({note}) says not a podcast; {verdict.detail}", via, base)
            continue

        base.update({
            "genre": info.genre or verdict.genre,
            "podcast_format": info.fmt or verdict.fmt,
            "host_name": info.host,
            "language": info.language,
        })

        # -- contacts ------------------------------------------------------
        base.update({k: contact.get(k, "") for k in (
            "email", "instagram", "facebook", "twitter", "tiktok",
            "linkedin", "discord", "telegram", "website", "other_links",
            "spotify", "apple_podcasts", "other_platform", "rss_feed",
            "booking_link", "membership_link",
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
        if contact.get("spotify") or contact.get("apple_podcasts") \
                or contact.get("other_platform"):
            st.with_platform += 1
        if contact.get("booking_link"):
            st.with_booking += 1
        qualified_ids.append(cid)
        _mark(store, cid, QUALIFIED, f"{verdict.detail} [{note}]", via, base)

        who = info.host or "(host unknown)"
        log(f"    [+] {title[:38]:<38} {subs.subscribers:>8,}  "
            f"{(base['genre'] or '')[:18]:<18} {base['podcast_format'][:10]:<10} "
            f"{who[:20]:<20} {contact.get('email') or '(socials only)'}")

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
        master_tab=str(cfg.get("sheets.master_tab", "Podcaster Leads")),
        tab_per_genre=bool(cfg.get("sheets.tab_per_genre", False)),
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
    """Re-run LLM profiling against already-qualified leads.

    Uses each lead's stored title + description and the podcast metrics
    already in the DB -- recent episode titles are not persisted per-lead,
    so this is a smaller signal than a fresh run gets, but still real
    understanding rather than a keyword guess. Zero YouTube quota spent;
    this never touches the YouTube API.

    The model's is_podcast verdict is NOT acted on here -- these leads
    already cleared the gate on evidence this pass cannot see (About-page
    platform links, episode numbering). Genre, format, host and language are
    the only fields it can change.

    Sheets export is append-only (see sheets.py): if a lead was already
    pushed under its old genre and tab_per_genre is on, this does not move
    or delete that row.
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
        1 for l in leads if (l["genre"] or "") == podcast.PENDING_GENRE
    )
    log(f"Re-profiling {len(leads)} leads, {len(chain)} attempt(s) in order [{labels}]"
        + (f" -- {pending_before} currently pending" if pending_before else "") + "...")

    changed = 0
    still_pending = 0
    for i, lead in enumerate(leads, 1):
        channel_stub = {
            "snippet": {"title": lead["title"] or "", "description": lead["description"] or ""}
        }
        old_genre = lead["genre"] or "other"
        stub_verdict = podcast.PodcastVerdict(
            is_podcast=True, score=float(lead["podcast_score"] or 0.0),
            confidence=lead["podcast_confidence"] or "medium",
            fmt=lead["podcast_format"] or "other", genre=old_genre,
            genre_score=float(lead["genre_score"] or 0.0), platforms={},
            signals=[], detail="stored",
        )
        facts = _stored_facts(lead)
        info, note = llm_classify.resolve(
            sx, stub_verdict, channel_stub, [], facts=facts, log=log
        )
        genre = info.genre or old_genre
        if genre != old_genre or (info.host and not lead["host_name"]):
            changed += 1
            reason = f"{lead['reason'] or ''} [reprofiled:{note}]".strip()
            store.update_profile(
                lead["channel_id"], genre, info.fmt, info.host, info.language, reason
            )
            log(f"    {(lead['title'] or '')[:36]:<36} {old_genre:<24} -> {genre}"
                + (f"  host: {info.host}" if info.host else ""))
        if genre == podcast.PENDING_GENRE:
            still_pending += 1
        if i % 25 == 0:
            store.commit()
            log(f"    ...{i}/{len(leads)} checked, {changed} changed so far")

    store.commit()
    log(f"\n{changed}/{len(leads)} lead(s) updated.")
    if still_pending:
        log(f"{still_pending} lead(s) are still pending -- every configured key is still "
            f"failing or rate-limited for them; try again later or add another key.")
    return changed


def _stored_facts(lead: Any) -> str:
    """Rebuild the metadata line from what the DB already knows, so a
    reclassify pass argues from the same numbers a live run did."""
    bits: list[str] = []
    try:
        if lead["subscribers"]:
            bits.append(f"{int(lead['subscribers']):,} subscribers")
        if lead["median_episode_minutes"]:
            bits.append(f"median upload {float(lead['median_episode_minutes']):.0f} min")
        if lead["episodes_per_month"]:
            bits.append(f"~{float(lead['episodes_per_month']):.1f} uploads/month")
        platforms = [
            name for name, col in (
                ("Spotify", "spotify"), ("Apple Podcasts", "apple_podcasts"),
                ("another podcast platform", "other_platform"),
            ) if lead[col]
        ]
        if platforms:
            bits.append("links to " + ", ".join(platforms))
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    return "; ".join(bits)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
