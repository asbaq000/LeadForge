"""
Fetch + parse a single public Instagram profile.

How it works without a login
----------------------------
Instagram's own web client calls

    GET /api/v1/users/web_profile_info/?username=<name>

with an `x-ig-app-id` header. That endpoint is what the public profile page
itself uses, so it needs no account, no cookie and no API key — just the
header. We hit the same endpoint and reshape its (very verbose) response
into the flat schema documented in the README.

Everything network-related lives in this module; utils_format.py stays pure.
"""

import json
import os
import time

import requests

from http_client import Blocked, InstagramClient
from utils_format import dig, normalize_username, safe_int, safe_str

PROFILE_ENDPOINT = "https://www.instagram.com/api/v1/users/web_profile_info/"
FEED_ENDPOINT = "https://www.instagram.com/api/v1/feed/user/{user_id}/"

# Two public web app ids, neither secret — both are hardcoded in Instagram's
# own JS bundles. They behave differently and we need both:
#
#   PRIMARY   full payload, including the recent-post edges, but currently
#             500s^H400s on a large share of business accounts with
#             "ig_business_category_subvertical has been deleted".
#   FALLBACK  survives those accounts, but returns media_count = 0 and no
#             post edges — profile fields only.
#
# So: try primary, fall back on the schema bug, and pull post timestamps
# from the feed endpoint separately (which works for both).
PRIMARY_APP_ID = "936619743392459"
FALLBACK_APP_ID = "238260118697367"

DEFAULT_HEADERS = {
    "x-ig-app-id": PRIMARY_APP_ID,
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.instagram.com/",
}

# Instagram's own account_type codes, mirrored from the private API.
ACCOUNT_TYPE_PERSONAL = 1
ACCOUNT_TYPE_BUSINESS = 2
ACCOUNT_TYPE_CREATOR = 3


class ProfileNotFound(Exception):
    """The username does not exist (or the account was removed)."""


class RateLimited(Exception):
    """Instagram threw a 429 / login wall at us."""


class ScrapeError(Exception):
    """
    The endpoint answered, but not with a profile — and not because of rate
    limiting. Instagram intermittently 400s on certain business accounts
    with a server-side schema error; retrying that is pointless.
    """


class SessionError(Exception):
    """Couldn't attach a logged-in session — missing file, missing dependency, etc."""


def _server_message(response):
    """Pull Instagram's own error text out of a failed response, if any."""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    if isinstance(payload, dict):
        return safe_str(payload.get("message"))
    return ""


def _load_login_cookies(login_username, session_file=None):
    """
    Read the cookie jar out of a session file created by instaloader's own
    CLI, without going through instaloader's Instaloader/context classes —
    just enough to get requests-compatible cookies back.

    instaloader saves sessions as `pickle.dump(dict_from_cookiejar(cookies))`
    (see InstaloaderContext.save_session); this reads that same format.
    Never touches a password — the file already exists or this fails.
    """
    import pickle

    try:
        from instaloader.instaloader import get_default_session_filename
    except ImportError as exc:
        raise SessionError(
            "instaloader is not installed:  pip install instaloader"
        ) from exc

    path = session_file or get_default_session_filename(login_username)
    if not os.path.exists(path):
        raise SessionError(
            f"no saved session for {login_username!r} at {path!r}. "
            f"Create one first (this only asks for your password once, "
            f"interactively, and never through this tool):\n"
            f"    instaloader --login={login_username}"
        )

    try:
        with open(path, "rb") as f:
            cookie_dict = pickle.load(f)
    except Exception as exc:  # noqa: BLE001 — surface the cause, not a raw traceback
        raise SessionError(f"could not read session file {path!r}: {exc}") from exc

    if "csrftoken" not in cookie_dict or "sessionid" not in cookie_dict:
        raise SessionError(
            f"session file {path!r} doesn't look like a logged-in session "
            f"(missing csrftoken/sessionid) — it may be stale or corrupt."
        )
    return cookie_dict


def attach_login_session(session, login_username, session_file=None):
    """
    Swap an anonymous session for a logged-in one, in place.

    Logged-in web sessions get a far higher per-account rate limit than the
    anonymous per-IP quota this scraper otherwise runs under — this is the
    fix for a "please wait a few minutes" block that isn't clearing.
    """
    cookies = _load_login_cookies(login_username, session_file)
    session.cookies.update(requests.utils.cookiejar_from_dict(cookies))
    session.headers["X-CSRFToken"] = cookies["csrftoken"]
    session.headers["X-IG-WWW-Claim"] = "0"
    return session


def build_session(settings=None, login_username=None, session_file=None,
                  state_path="data/run/ratelimit.json"):
    """
    Build the HTTP client used for every request.

    Anonymous by default, and deliberately careful about it: guest cookies,
    rotating fingerprints, jittered pacing and a cooldown that survives
    process exit (see http_client). Pass login_username to authenticate with
    a session file created by `instaloader --login=<name>` instead — this
    module never sees or handles a password, only cookies already on disk.
    """
    settings = settings or {}
    client = InstagramClient(settings, state_path=state_path)

    login_username = login_username or settings.get("login_username")
    if login_username:
        # A logged-in session is bound to an account, not a browser guest —
        # start it eagerly so the cookies are in place for request one.
        client._new_session()
        attach_login_session(client.session, login_username,
                             session_file or settings.get("session_file"))
        client.logged_in = True
        # Authenticated traffic tolerates a much higher rate than anonymous,
        # and rotating the fingerprint would throw away the session cookies.
        client.rotate_after = 10 ** 9
        client.min_delay = settings.get("min_delay", 1.0)
        client.max_delay = settings.get("max_delay", 2.0)

    return client


def fetch_profile(client, username, settings=None):
    """
    Fetch the raw `data.user` dict, transparently falling back to the second
    app id when the primary one hits Instagram's schema bug.

    Returns the user dict; it carries a `_app_id` marker so callers can tell
    which path produced it (the fallback path has no post data).
    """
    try:
        return _fetch_profile_with(client, username, PRIMARY_APP_ID, settings)
    except ScrapeError as exc:
        if "ig_business_category_subvertical" not in str(exc):
            raise
    return _fetch_profile_with(client, username, FALLBACK_APP_ID, settings)


def _fetch_profile_with(client, username, app_id, settings=None):
    """
    Hit the endpoint with one specific app id and return the raw user dict.

    Rate-limit handling lives in the client now: it detects a block, records
    a persistent cooldown and raises Blocked, which we surface as RateLimited
    so callers stop the run. Retrying a block is what deepens it, so the only
    retries here are for genuinely transient failures.
    """
    settings = settings or {}
    max_retries = settings.get("max_retries", 2)
    backoff = settings.get("retry_backoff", 3.0)

    username = normalize_username(username)
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = client.get(
                PROFILE_ENDPOINT,
                params={"username": username},
                app_id=app_id,
                referer=f"https://www.instagram.com/{username}/",
                allow_status=(404, 400),
            )
        except Blocked as exc:
            raise RateLimited(str(exc)) from exc

        if response.status_code == 404:
            raise ProfileNotFound(f"@{username} does not exist")

        if response.status_code == 400:
            # Deterministic server-side rejection — the same request will
            # fail identically forever, so fail fast instead of retrying.
            message = _server_message(response)
            raise ScrapeError(
                f"HTTP 400 from Instagram for @{username}"
                + (f": {message}" if message else "")
            )

        if response.status_code != 200:
            last_error = f"HTTP {response.status_code}"
            if attempt == max_retries:
                raise ScrapeError(f"@{username} failed: {last_error}")
            time.sleep(backoff * attempt)
            continue

        try:
            payload = response.json()
        except json.JSONDecodeError:
            # Almost always an HTML challenge page served with a 200.
            last_error = "non-JSON response (challenge page?)"
            time.sleep(backoff * attempt)
            continue

        user = dig(payload, "data", "user")
        if not user:
            # A bare {"status":"ok"} with no payload. Not a 404 — the account
            # usually exists but Instagram declined to describe it to an
            # anonymous caller. Worth distinguishing from "does not exist".
            raise ScrapeError(f"@{username} returned an empty payload (restricted?)")

        user["_app_id"] = app_id
        return user

    # Only network errors and unparseable bodies reach here — the rate-limit
    # and hard-failure paths raise on their final attempt above.
    raise ScrapeError(f"gave up on @{username} after {max_retries} attempts: {last_error}")


def _account_type(user):
    """
    web_profile_info doesn't expose the numeric account_type, so derive it
    from the professional/business flags to match the documented values.
    """
    if user.get("is_business_account"):
        return ACCOUNT_TYPE_BUSINESS
    if user.get("is_professional_account"):
        return ACCOUNT_TYPE_CREATOR
    return ACCOUNT_TYPE_PERSONAL


def _category(user):
    """
    Which key holds the category depends on the account flavour: creators
    get `category_name`, businesses get `business_category_name`, and a few
    only expose the uppercase enum. Try them in that order.
    """
    for key in ("category_name", "business_category_name", "overall_category_name"):
        value = safe_str(user.get(key)).strip()
        if value:
            return value

    enum_value = safe_str(user.get("category_enum")).strip()
    if enum_value:
        return enum_value.replace("_", " ").title()
    return ""


def _location_data(user):
    """
    Business/creator accounts may publish an address blob as a JSON *string*.
    Personal accounts have none — return the zeroed shape so every record
    has identical keys.
    """
    location = {"city_name": "", "latitude": 0, "longitude": 0}

    raw_address = user.get("business_address_json")
    if isinstance(raw_address, str) and raw_address.strip():
        try:
            address = json.loads(raw_address)
        except json.JSONDecodeError:
            address = {}
        if isinstance(address, dict):
            location["city_name"] = safe_str(address.get("city_name"))
            location["latitude"] = address.get("latitude") or 0
            location["longitude"] = address.get("longitude") or 0

    # Some responses carry coordinates at the top level instead.
    location["latitude"] = user.get("latitude") or location["latitude"]
    location["longitude"] = user.get("longitude") or location["longitude"]
    return location


def parse_profile(user):
    """Flatten the raw `data.user` blob into the README's output schema."""
    return {
        "username": safe_str(user.get("username")),
        "full_name": safe_str(user.get("full_name")),
        "biography": safe_str(user.get("biography")),
        "external_url": safe_str(user.get("external_url")),
        "category": _category(user),
        "follower_count": safe_int(dig(user, "edge_followed_by", "count")),
        "following_count": safe_int(dig(user, "edge_follow", "count")),
        "is_verified": bool(user.get("is_verified")),
        # None, not 0, when the fallback app id served this profile: that
        # payload always says 0 regardless of the truth, and reporting a
        # confident 0 would make an active account look abandoned.
        "media_count": (
            None
            if user.get("_app_id") == FALLBACK_APP_ID
            else safe_int(dig(user, "edge_owner_to_timeline_media", "count"))
        ),
        "profile_pic_url_hd": safe_str(
            user.get("profile_pic_url_hd") or user.get("profile_pic_url")
        ),
        "account_type": _account_type(user),
        "location_data": _location_data(user),
    }


def related_usernames(user):
    """
    The handles Instagram itself suggests as similar to this one.

    This is the only keyword-free discovery channel that works logged out —
    hashtag and search endpoints both answer `login_required`.
    """
    edges = dig(user, "edge_related_profiles", "edges", default=[]) or []
    names = []
    for edge in edges:
        name = safe_str(dig(edge, "node", "username"))
        if name:
            names.append(name)
    return names


def embedded_post_timestamps(user):
    """
    Post timestamps already present in the profile response, newest first.

    The primary app id ships the 12 most recent posts inside the profile
    payload, so reading them here saves a whole request per account — which
    matters a lot given how tight the anonymous quota is. The fallback app id
    ships none, and returns []; those accounts need fetch_post_timestamps.
    """
    edges = dig(user, "edge_owner_to_timeline_media", "edges", default=[]) or []
    timestamps = []
    for edge in edges:
        taken_at = dig(edge, "node", "taken_at_timestamp")
        if isinstance(taken_at, int):
            timestamps.append(taken_at)
    return sorted(timestamps, reverse=True)


def fetch_post_timestamps(client, user_id, settings=None, count=24):
    """
    Return the epoch timestamps of an account's most recent posts.

    Uses /api/v1/feed/user/<id>/, which — unlike the profile endpoint's post
    edges — answers for accounts served by either app id. Pinned posts come
    back first regardless of age, so the caller must sort rather than trust
    the order.

    Returns [] for private accounts and anything else that declines.
    """
    settings = settings or {}

    try:
        response = client.get(
            FEED_ENDPOINT.format(user_id=user_id),
            params={"count": count},
            app_id=PRIMARY_APP_ID,
            allow_status=(400, 403, 404),
        )
    except Blocked as exc:
        # Let a block propagate — swallowing it here would let the caller
        # keep hammering and turn a short cooldown into a long one.
        raise RateLimited(str(exc)) from exc
    except requests.RequestException:
        return []

    if response.status_code != 200:
        return []

    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return []

    timestamps = []
    for item in payload.get("items") or []:
        taken_at = item.get("taken_at")
        if isinstance(taken_at, int):
            timestamps.append(taken_at)
    return sorted(timestamps, reverse=True)


def scrape_profile(client, username, settings=None, with_posts=False):
    """
    fetch + parse in one call. Raises the same exceptions as fetch_profile.

    with_posts=True costs one extra request and adds `post_timestamps`
    (epoch seconds, newest first) plus the numeric `user_id`.
    """
    user = fetch_profile(client, username, settings)
    record = parse_profile(user)

    if with_posts:
        user_id = safe_str(user.get("id"))
        record["user_id"] = user_id
        record["is_private"] = bool(user.get("is_private"))
        record["post_timestamps"] = (
            [] if user.get("is_private") or not user_id
            else fetch_post_timestamps(client, user_id, settings)
        )

    return record
