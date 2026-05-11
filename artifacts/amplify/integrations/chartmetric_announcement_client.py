"""REST client for the Chartmetric in-app announcement admin API.

This module owns *all* HTTP traffic to the Chartmetric admin endpoints
documented in ``docs/chartmetric-announcement-admin-api.md`` so the rest
of the Amplify codebase doesn't have to know anything about wire shape,
auth, or error decoding.

Auth (two modes, auto-detected):

  1. **Cookie / service-account** — used when ALL THREE of
     ``CM_API_BASE_URL``, ``CM_SERVICE_ACCOUNT_EMAIL``, and
     ``CM_SERVICE_ACCOUNT_PASSWORD`` are set (Update Hub proxy pattern).
     The client POSTs to ``/login`` once, caches the ``Set-Cookie`` token
     in memory, and forwards it as ``Cookie: <token>`` on every request.
     On 401/403 the token is cleared and re-fetched automatically.

  2. **Bearer token (legacy)** — used when ``CHARTMETRIC_ADMIN_API_TOKEN``
     is set but the service-account credentials are absent. Sends
     ``Authorization: Bearer <token>`` on every request; no auto-refresh.

When neither auth path is configured the client raises
``ChartmetricClientError`` so the caller can fall back to stub mode.

Base URL resolution: ``CM_API_BASE_URL`` takes precedence over the legacy
``CHARTMETRIC_ADMIN_API_BASE_URL`` (``config.py`` already aliases them).
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Iterable

import config

logger = logging.getLogger("amplify.chartmetric_announcement_client")

# Path constants — adjust here if Chartmetric finalises a different prefix.
ADMIN_PREFIX = "/admin/announcement"
POSTS_PATH = ADMIN_PREFIX                  # POST/GET list, /<id> for detail
CATEGORIES_PATH = f"{ADMIN_PREFIX}/categories"
POST_CATEGORIES_LINK_PATH = "{post_id}/categories"   # PUT replaces link rows
POST_BOOST_PATH = "{post_id}/boost"        # PATCH is_boosted only

DEFAULT_TIMEOUT = 15.0

# ---------------------------------------------------------------------------
# Service-account token cache (cookie-based auth)
# ---------------------------------------------------------------------------

_cached_service_token: str | None = None
_token_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class ChartmetricClientError(Exception):
    """Raised when a Chartmetric admin REST call fails.

    Carries the HTTP status code (or ``0`` for transport errors) and the
    parsed response body when available so callers can surface a helpful
    error to the marketer.
    """

    def __init__(self, message: str, *, status: int = 0,
                 body: Any = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
        self.url = url

    def short(self) -> str:
        body_preview = ""
        if isinstance(self.body, dict):
            err = self.body.get("error") or self.body.get("message")
            if err:
                body_preview = f" — {err}"
        elif isinstance(self.body, str) and self.body:
            body_preview = f" — {self.body[:200]}"
        return f"HTTP {self.status}{body_preview}" if self.status else str(self)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return (getattr(config, "CHARTMETRIC_ADMIN_API_BASE_URL", "") or "").rstrip("/")


def _bearer_token() -> str:
    return getattr(config, "CHARTMETRIC_ADMIN_API_TOKEN", "") or ""


def _service_account_email() -> str:
    return (getattr(config, "CM_SERVICE_ACCOUNT_EMAIL", "") or "").strip()


def _service_account_password() -> str:
    return (getattr(config, "CM_SERVICE_ACCOUNT_PASSWORD", "") or "").strip()


def _use_cookie_auth() -> bool:
    """True when service-account cookie auth is fully configured."""
    return bool(_base_url() and _service_account_email() and _service_account_password())


def is_configured() -> bool:
    """Live wiring is considered configured when either auth method is ready."""
    if _use_cookie_auth():
        return True
    return bool(_base_url() and _bearer_token())


def _login_service_account() -> str:
    """POST /login with service-account credentials; return cookie string.

    Extracts all ``Set-Cookie`` values from the response and joins them into
    a single ``Cookie: <name>=<value>; ...`` header string, matching the
    pattern from the Update Hub proxy guide.
    """
    import requests as req_lib

    base = _base_url()
    email = _service_account_email()
    password = _service_account_password()
    if not (base and email and password):
        raise ChartmetricClientError(
            "Service-account credentials not configured "
            "(CM_API_BASE_URL / CM_SERVICE_ACCOUNT_EMAIL / CM_SERVICE_ACCOUNT_PASSWORD)",
            status=0)

    url = f"{base}/login"
    try:
        resp = req_lib.request(
            "POST", url,
            json={"email": email, "password": password},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except req_lib.RequestException as e:
        raise ChartmetricClientError(
            f"Service-account login transport error: {e!r}",
            status=0, url=url) from e

    if resp.status_code < 200 or resp.status_code >= 300:
        raise ChartmetricClientError(
            f"Service-account login failed (HTTP {resp.status_code})",
            status=resp.status_code, url=url)

    # Build Cookie header string from the response cookie jar.
    cookie_str = "; ".join(f"{c.name}={c.value}" for c in resp.cookies)
    if not cookie_str:
        raise ChartmetricClientError(
            "Service-account login succeeded but no session cookie was returned",
            status=resp.status_code, url=url)

    return cookie_str


def _get_session_token(force: bool = False) -> str:
    """Return a cached service-account cookie token, logging in if needed.

    Thread-safe via double-checked locking. ``force=True`` bypasses the
    cache entirely (used by the 401/403 auto-refresh path).
    """
    global _cached_service_token
    if not force and _cached_service_token:
        return _cached_service_token
    with _token_lock:
        if not force and _cached_service_token:
            return _cached_service_token
        token = _login_service_account()
        _cached_service_token = token
        return token


def clear_service_account_token_cache() -> None:
    """Evict the cached service-account token (for testing / admin use)."""
    global _cached_service_token
    with _token_lock:
        _cached_service_token = None


def _request(method: str, path: str, *,
             params: dict | None = None,
             body: Any = None,
             timeout: float = DEFAULT_TIMEOUT,
             allow_404: bool = False) -> tuple[int, Any]:
    """Issue an HTTP request and return ``(status_code, parsed_body)``.

    Auth is selected automatically:
    - Cookie / service-account when CM creds are configured.
    - Bearer token fallback when only CHARTMETRIC_ADMIN_API_TOKEN is set.

    Raises ``ChartmetricClientError`` on transport errors. 4xx/5xx are
    NOT raised here — callers decide whether a non-2xx is fatal.
    """
    import requests as req_lib

    base = _base_url()
    if not base:
        raise ChartmetricClientError(
            "Chartmetric base URL not configured "
            "(CM_API_BASE_URL or CHARTMETRIC_ADMIN_API_BASE_URL)",
            status=0, url=path)

    def _do(auth_header: dict) -> tuple[int, Any]:
        url = f"{base}{path}"
        headers: dict[str, str] = {"Accept": "application/json; charset=utf-8"}
        headers.update(auth_header)
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            resp = req_lib.request(
                method.upper(), url,
                params=params,
                json=body if body is not None else None,
                headers=headers,
                timeout=timeout,
            )
        except req_lib.RequestException as e:
            raise ChartmetricClientError(
                f"Transport error talking to Chartmetric: {e!r}",
                status=0, body=None, url=url) from e

        if resp.status_code == 404 and allow_404:
            return resp.status_code, None
        parsed: Any
        if resp.content:
            try:
                parsed = resp.json()
            except Exception:
                parsed = resp.text
        else:
            parsed = None
        return resp.status_code, parsed

    if _use_cookie_auth():
        tok = _get_session_token()
        code, parsed = _do({"Cookie": tok})
        if code in (401, 403):
            logger.warning(
                "Cookie session expired (HTTP %s on %s); refreshing service-account token",
                code, path)
            global _cached_service_token
            _cached_service_token = None
            tok = _get_session_token(force=True)
            code, parsed = _do({"Cookie": tok})
        return code, parsed

    tok = _bearer_token()
    if not tok:
        missing = []
        if not base:
            missing.append("CHARTMETRIC_ADMIN_API_BASE_URL")
        missing.append("CHARTMETRIC_ADMIN_API_TOKEN")
        raise ChartmetricClientError(
            "Missing required env var(s): " + ", ".join(missing),
            status=0, url=path)
    return _do({"Authorization": f"Bearer {tok}"})


def _ensure_2xx(status: int, body: Any, url: str) -> Any:
    if 200 <= status < 300:
        return body
    raise ChartmetricClientError(
        f"Chartmetric returned HTTP {status} for {url}",
        status=status, body=body, url=url)


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------

def ping() -> dict:
    """Probe the Chartmetric admin API by listing categories.

    Returns a dict with ``ok`` (bool), ``status`` (HTTP), ``base_url``,
    ``auth_method`` (``"cookie"`` or ``"bearer"``), plus either
    ``count`` (categories returned) or ``error`` (failure preview).
    """
    info: dict[str, Any] = {
        "base_url": _base_url() or None,
        "auth_method": "cookie" if _use_cookie_auth() else "bearer",
        "token_configured": bool(_bearer_token()),
        "service_account_configured": bool(_service_account_email() and _service_account_password()),
    }
    if not is_configured():
        info["ok"] = False
        info["status"] = 0
        missing = []
        if not _base_url():
            missing.append("CM_API_BASE_URL / CHARTMETRIC_ADMIN_API_BASE_URL")
        if not _use_cookie_auth() and not _bearer_token():
            missing.append("CM_SERVICE_ACCOUNT_* or CHARTMETRIC_ADMIN_API_TOKEN")
        info["error"] = "Missing required env var(s): " + ", ".join(missing)
        return info
    try:
        status, body = _request("GET", CATEGORIES_PATH, timeout=10.0)
    except ChartmetricClientError as e:
        info["ok"] = False
        info["status"] = e.status
        info["error"] = e.short()
        return info
    info["status"] = status
    info["ok"] = 200 <= status < 300
    if info["ok"]:
        if isinstance(body, list):
            info["count"] = len(body)
        elif isinstance(body, dict):
            items = body.get("items") or body.get("categories")
            info["count"] = len(items) if isinstance(items, list) else None
    else:
        info["error"] = _short_error(body)
    return info


def _short_error(body: Any) -> str:
    if isinstance(body, dict):
        msg = body.get("error") or body.get("message")
        if msg:
            return str(msg)[:200]
        try:
            import json as _json
            return _json.dumps(body)[:200]
        except Exception:
            return str(body)[:200]
    if isinstance(body, str):
        return body[:200]
    return ""


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

def list_posts(*, status: str | None = None, category: str | None = None,
               search: str | None = None, offset: int = 0,
               limit: int = 25) -> dict:
    params = {
        "status": status or "all",
        "category": category or "",
        "search": search or "",
        "offset": offset,
        "limit": limit,
    }
    code, body = _request("GET", POSTS_PATH, params=params)
    return _ensure_2xx(code, body, POSTS_PATH) or {"items": [], "total": 0}


def get_post(post_id: int | str) -> dict | None:
    path = f"{POSTS_PATH}/{post_id}"
    code, body = _request("GET", path, allow_404=True)
    if code == 404:
        return None
    return _ensure_2xx(code, body, path)


def create_post(payload: dict) -> dict:
    code, body = _request("POST", POSTS_PATH, body=payload)
    return _ensure_2xx(code, body, POSTS_PATH)


def update_post(post_id: int | str, payload: dict) -> dict | None:
    path = f"{POSTS_PATH}/{post_id}"
    code, body = _request("PUT", path, body=payload, allow_404=True)
    if code == 404:
        return None
    return _ensure_2xx(code, body, path)


def delete_post(post_id: int | str) -> bool:
    path = f"{POSTS_PATH}/{post_id}"
    code, body = _request("DELETE", path, allow_404=True)
    if code == 404:
        return False
    if 200 <= code < 300:
        return True
    raise ChartmetricClientError(
        f"Failed to delete post {post_id}",
        status=code, body=body, url=path)


def patch_post_boost(post_id: int | str, is_boosted: bool) -> dict:
    """Flip ``is_boosted`` on an existing post without touching anything else.

    Sends ``PATCH /admin/announcement/<id>/boost`` with body
    ``{"is_boosted": <bool>}``.
    """
    path = f"{POSTS_PATH}/{POST_BOOST_PATH.format(post_id=post_id)}"
    code, body = _request("PATCH", path, body={"is_boosted": bool(is_boosted)})
    return _ensure_2xx(code, body, path)


def replace_post_categories(post_id: int | str,
                             category_ids: Iterable[int]) -> dict:
    """Replace the ``l_announcement_post_category`` rows for a post.

    The endpoint is expected to honour *replace* semantics — any existing
    links not in ``category_ids`` MUST be removed. We use PUT to make
    the intent explicit.
    """
    path = f"{POSTS_PATH}/{POST_CATEGORIES_LINK_PATH.format(post_id=post_id)}"
    payload = {"category_ids": [int(c) for c in (category_ids or [])]}
    code, body = _request("PUT", path, body=payload)
    return _ensure_2xx(code, body, path) or {}


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def list_categories() -> list[dict]:
    code, body = _request("GET", CATEGORIES_PATH)
    out = _ensure_2xx(code, body, CATEGORIES_PATH)
    if isinstance(out, list):
        return out
    if isinstance(out, dict):
        items = out.get("items") or out.get("categories") or []
        return list(items)
    return []


def create_category(payload: dict) -> dict:
    code, body = _request("POST", CATEGORIES_PATH, body=payload)
    return _ensure_2xx(code, body, CATEGORIES_PATH)


def update_category(cat_id: int | str, payload: dict) -> dict | None:
    path = f"{CATEGORIES_PATH}/{cat_id}"
    code, body = _request("PUT", path, body=payload, allow_404=True)
    if code == 404:
        return None
    return _ensure_2xx(code, body, path)


def delete_category(cat_id: int | str) -> bool:
    path = f"{CATEGORIES_PATH}/{cat_id}"
    code, body = _request("DELETE", path, allow_404=True)
    if code == 404:
        return False
    if 200 <= code < 300:
        return True
    raise ChartmetricClientError(
        f"Failed to delete category {cat_id}",
        status=code, body=body, url=path)


# ---------------------------------------------------------------------------
# Convenience: resolve-or-create a category by name (case-insensitive)
# ---------------------------------------------------------------------------

_LISTED_KEY = "__listed__"


def resolve_or_create_category(name: str, color: str,
                                translations: dict | None = None,
                                *, cache: dict | None = None) -> dict:
    """Look a category up on Chartmetric by name; create it if missing.

    ``cache`` is an optional dict the caller passes across the *entire*
    save operation so a multi-category post triggers at most ONE
    ``GET /admin/announcement/categories`` call. On the first miss we
    fetch the full category list, slot every entry into the cache by
    ``name.lower()``, mark the cache as primed via ``__listed__=True``,
    and serve subsequent names from memory. Newly created categories
    are also written back to the cache.
    """
    name_key = (name or "").strip().lower()
    if not name_key:
        raise ChartmetricClientError("Cannot resolve a category with empty name")
    if cache is not None and name_key in cache:
        return cache[name_key]
    if cache is None or not cache.get(_LISTED_KEY):
        cats = list_categories()
        if cache is not None:
            cache[_LISTED_KEY] = True
            for c in cats:
                k = (c.get("name") or "").strip().lower()
                if k:
                    cache.setdefault(k, c)
        else:
            for c in cats:
                if (c.get("name") or "").strip().lower() == name_key:
                    return c
        if cache is not None and name_key in cache:
            return cache[name_key]
    cat = create_category({
        "name": name,
        "color": color,
        "translations": translations or {},
    })
    if cache is not None:
        cache[name_key] = cat
    return cat
