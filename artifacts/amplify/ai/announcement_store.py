"""Storage backend for the in-app announcements admin.

Two modes, switched automatically by environment configuration:

  * **Stub** — picked when EITHER ``CHARTMETRIC_ADMIN_API_BASE_URL`` or
    ``CHARTMETRIC_ADMIN_API_TOKEN`` is missing. JSON-cache + in-memory
    store at ``.announcement_store.json`` — survives restarts, no
    network. Lets the admin UI run end-to-end before chartmetric-api
    is reachable.

  * **Proxy / live** — picked when BOTH ``CHARTMETRIC_ADMIN_API_BASE_URL``
    and ``CHARTMETRIC_ADMIN_API_TOKEN`` are set. The local JSON store
    remains the *working copy* (it tracks Amplify-only metadata such
    as ``display_format``, ``scheduled_publish_at``, ``source_feature_*``,
    plus the local-id ↔ Chartmetric-id mapping) and every create /
    update / delete is also pushed to the live Chartmetric REST API
    via ``integrations.chartmetric_announcement_client``.

Live mode can be forced *off* (kill switch) by setting
``ANNOUNCEMENTS_STUB_MODE`` to a truthy value (``1`` / ``true`` /
``yes`` / ``on``); this is intended for incident response so an
operator can pin Amplify to its local working copy without unsetting
the Chartmetric env vars.

Both modes return the same JSON shape (see spec §6).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import config

logger = logging.getLogger("amplify.announcement_store")

_STORE_FILE = os.path.join(os.path.dirname(__file__), "..", ".announcement_store.json")
_lock = threading.Lock()

VALID_DISPLAY_FORMATS = ("banner", "popup", "inline")
VALID_STATUSES = ("draft", "publish_now", "schedule")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
TARGET_LOCALES = ("de", "es", "fr", "ja", "ko", "pt")

DEFAULT_CATEGORIES = [
    {"name": "New Feature", "color": "#00C9A7"},
    {"name": "Improvement", "color": "#3b82f6"},
    {"name": "Heads up",   "color": "#f97316"},
]


# ---------------------------------------------------------------------------
# Mode helpers
# ---------------------------------------------------------------------------

def _stub_mode_enabled() -> bool:
    """Decide whether Amplify is in stub mode for this call.

    Live mode is enabled automatically when EITHER:
      * The new cookie-based auth is fully configured:
        ``CM_API_BASE_URL`` + ``CM_SERVICE_ACCOUNT_EMAIL``
        + ``CM_SERVICE_ACCOUNT_PASSWORD`` are all set, OR
      * The legacy bearer-token pair is set:
        ``CHARTMETRIC_ADMIN_API_BASE_URL`` + ``CHARTMETRIC_ADMIN_API_TOKEN``.

    ``ANNOUNCEMENTS_STUB_MODE`` is a kill switch — set it to
    ``1`` / ``true`` / ``yes`` / ``on`` to pin Amplify to the local
    working copy even when the env vars are wired (incident response).
    """
    # New cookie-based auth (CM_API_BASE_URL already aliased into
    # CHARTMETRIC_ADMIN_API_BASE_URL by config.py, so check the CM_ vars
    # independently to gate on service-account credentials being present).
    cm_base = (getattr(config, "CM_API_BASE_URL", "") or "").strip()
    cm_email = (getattr(config, "CM_SERVICE_ACCOUNT_EMAIL", "") or "").strip()
    cm_password = (getattr(config, "CM_SERVICE_ACCOUNT_PASSWORD", "") or "").strip()
    cookie_auth_ready = bool(cm_base and cm_email and cm_password)

    # Legacy bearer-token auth.
    old_base = (getattr(config, "CHARTMETRIC_ADMIN_API_BASE_URL", "") or "").strip()
    old_token = (getattr(config, "CHARTMETRIC_ADMIN_API_TOKEN", "") or "").strip()
    bearer_auth_ready = bool(old_base and old_token)

    if not (cookie_auth_ready or bearer_auth_ready):
        return True
    raw = os.environ.get("ANNOUNCEMENTS_STUB_MODE")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_mode_info() -> dict:
    cm_base = (getattr(config, "CM_API_BASE_URL", "") or "").strip()
    old_base = (getattr(config, "CHARTMETRIC_ADMIN_API_BASE_URL", "") or "").strip()
    return {
        "stub_mode": _stub_mode_enabled(),
        "base_url": cm_base or old_base or None,
        "auth_method": (
            "cookie" if (
                cm_base
                and (getattr(config, "CM_SERVICE_ACCOUNT_EMAIL", "") or "").strip()
                and (getattr(config, "CM_SERVICE_ACCOUNT_PASSWORD", "") or "").strip()
            ) else "bearer"
        ),
        "token_configured": bool(getattr(config, "CHARTMETRIC_ADMIN_API_TOKEN", "")),
        "service_account_configured": bool(
            (getattr(config, "CM_SERVICE_ACCOUNT_EMAIL", "") or "").strip()
            and (getattr(config, "CM_SERVICE_ACCOUNT_PASSWORD", "") or "").strip()
        ),
        "media_upload_url": getattr(config, "CHARTMETRIC_MEDIA_UPLOAD_URL", "") or None,
        "store_path": os.path.abspath(_STORE_FILE),
    }


# ---------------------------------------------------------------------------
# Stub-store persistence
# ---------------------------------------------------------------------------

def _empty_store() -> dict:
    return {
        "next_post_id": 1,
        "next_category_id": 1,
        "posts": {},        # id (str) -> post dict
        "categories": {},   # id (str) -> category dict
    }


def _load() -> dict:
    try:
        if os.path.exists(_STORE_FILE):
            with open(_STORE_FILE, "r") as f:
                data = json.load(f)
            data.setdefault("next_post_id", 1)
            data.setdefault("next_category_id", 1)
            data.setdefault("posts", {})
            data.setdefault("categories", {})
            return data
    except Exception as e:
        logger.warning("[announcement_store] load failed: %s — starting fresh", e)
    return _empty_store()


def _save(data: dict) -> None:
    try:
        tmp = _STORE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, _STORE_FILE)
    except Exception as e:
        logger.warning("[announcement_store] save failed: %s", e)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_seed_categories(data: dict) -> bool:
    """Seed a few starter categories on the very first run so the UI isn't
    empty. Returns True if anything changed."""
    if data.get("categories"):
        return False
    changed = False
    for cat in DEFAULT_CATEGORIES:
        cid = data["next_category_id"]
        data["next_category_id"] = cid + 1
        data["categories"][str(cid)] = {
            "id": cid,
            "name": cat["name"],
            "color": cat["color"],
            "translations": {},
        }
        changed = True
    return changed


def _resolve_status(status: str, scheduled_at: str | None) -> dict:
    now_iso = _now_iso()
    if status == "publish_now":
        return {"is_published": True,
                "published_at": now_iso,
                "scheduled_publish_at": None}
    if status == "schedule":
        return {"is_published": False,
                "published_at": None,
                "scheduled_publish_at": scheduled_at}
    return {"is_published": False,
            "published_at": None,
            "scheduled_publish_at": None}


def _derived_status(post: dict) -> str:
    if post.get("is_published"):
        return "published"
    if post.get("scheduled_publish_at"):
        return "scheduled"
    return "draft"


def _hydrate_post(post: dict, categories_by_id: dict) -> dict:
    out = dict(post)
    cats = []
    for cid in post.get("category_ids") or []:
        c = categories_by_id.get(str(cid))
        if c:
            cats.append({
                "id": c["id"],
                "name": c["name"],
                "color": c["color"],
                "translations": c.get("translations") or {},
            })
    out["categories"] = cats
    out["status"] = _derived_status(post)
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    """Raised when a post / category payload fails validation."""

    def __init__(self, message: str, code: str = "validation_error",
                 status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _validate_translations(value: Any, fields: tuple[str, ...]) -> dict:
    """Validate the JSONB translations payload sent to Chartmetric.

    English (``en``) is *never* persisted in ``translations`` — it lives
    in the top-level ``title`` / ``content`` / ``name`` columns. We reject
    it explicitly so the marketer notices the mistake.

    Per-locale entries must be objects with **every** required ``field``
    present and well-typed (``str`` for everything except ``content``,
    which must be a non-empty Slate.js block array). Partial blobs —
    e.g. a post locale carrying only ``title`` without ``content`` —
    are rejected so the wire payload always matches the JSONB shape
    the Chartmetric reader expects.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("translations must be an object")
    if "en" in value:
        raise ValidationError(
            "translations must not contain the 'en' key — English is stored in the "
            "top-level title/content/name columns")
    out: dict[str, dict] = {}
    for lang, blob in value.items():
        if lang not in TARGET_LOCALES:
            # silently ignore unknown locales rather than hard-failing
            continue
        if not isinstance(blob, dict):
            raise ValidationError(
                f"translations.{lang} must be an object with keys {list(fields)}")
        missing = [f for f in fields if f not in blob]
        if missing:
            raise ValidationError(
                f"translations.{lang} is missing required field(s): "
                f"{', '.join(missing)}")
        keep: dict[str, Any] = {}
        for f in fields:
            v = blob[f]
            if f == "content":
                if not isinstance(v, list) or not v:
                    raise ValidationError(
                        f"translations.{lang}.content must be a non-empty "
                        "Slate.js block array")
            else:
                if not isinstance(v, str) or not v.strip():
                    raise ValidationError(
                        f"translations.{lang}.{f} must be a non-empty string")
            keep[f] = v
        out[lang] = keep
    return out


def _validate_post_input(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError("Body must be a JSON object")
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValidationError("title is required")
    if len(title) > 300:
        raise ValidationError("title must be <= 300 chars")
    content = payload.get("content")
    if not isinstance(content, list) or not content:
        raise ValidationError("content must be a non-empty Slate.js block array")
    category_ids_raw = payload.get("category_ids") or []
    if not isinstance(category_ids_raw, list):
        raise ValidationError("category_ids must be an array")
    try:
        category_ids = [int(c) for c in category_ids_raw]
    except (TypeError, ValueError):
        raise ValidationError("category_ids must be integers")
    image_url = payload.get("image_url") or None
    if image_url is not None and not isinstance(image_url, str):
        raise ValidationError("image_url must be a string or null")
    display_format = payload.get("display_format") or "banner"
    if display_format not in VALID_DISPLAY_FORMATS:
        raise ValidationError(f"display_format must be one of {VALID_DISPLAY_FORMATS}")
    status = payload.get("status") or "draft"
    if status not in VALID_STATUSES:
        raise ValidationError(f"status must be one of {VALID_STATUSES}")
    scheduled_at = payload.get("scheduled_publish_at") or None
    if status == "schedule":
        if not scheduled_at or not isinstance(scheduled_at, str):
            raise ValidationError("scheduled_publish_at is required when status=schedule")
    else:
        scheduled_at = None
    return {
        "title": title,
        "content": content,
        "translations": _validate_translations(payload.get("translations"),
                                                ("title", "content")),
        "category_ids": category_ids,
        "image_url": image_url,
        "display_format": display_format,
        "is_pinned": bool(payload.get("is_pinned")),
        "is_boosted": bool(payload.get("is_boosted")),
        "status": status,
        "scheduled_publish_at": scheduled_at,
        "source_feature_id": payload.get("source_feature_id") or None,
        "source_feature_set_id": payload.get("source_feature_set_id") or None,
    }


def _validate_category_input(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError("Body must be a JSON object")
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValidationError("name is required")
    if len(name) > 80:
        raise ValidationError("name must be <= 80 chars")
    color = payload.get("color") or ""
    if not HEX_COLOR_RE.match(color):
        raise ValidationError("color must be a 7-char hex string like #00C9A7")
    return {
        "name": name,
        "color": color,
        "translations": _validate_translations(payload.get("translations"),
                                                ("name",)),
    }


# ---------------------------------------------------------------------------
# Stub-mode CRUD
# ---------------------------------------------------------------------------

def _stub_list_posts(status: str | None, category: str | None,
                     search: str | None, offset: int, limit: int) -> dict:
    with _lock:
        data = _load()
        if _ensure_seed_categories(data):
            _save(data)
        cats_by_id = data["categories"]
        # Build name->id resolver for the optional category filter.
        cat_name_to_id = {c["name"].lower(): str(c["id"])
                          for c in cats_by_id.values()}
        items = [_hydrate_post(p, cats_by_id) for p in data["posts"].values()]
    if status and status != "all":
        items = [p for p in items if p["status"] == status]
    if category:
        cid = cat_name_to_id.get(category.strip().lower())
        if cid:
            items = [p for p in items
                     if any(str(c["id"]) == cid for c in p["categories"])]
        else:
            items = []
    if search:
        s = search.strip().lower()
        items = [p for p in items if s in (p.get("title") or "").lower()]
    items.sort(
        key=lambda p: (
            0 if p.get("is_pinned") else 1,
            -(_iso_to_epoch(p.get("modified_at"))),
        )
    )
    total = len(items)
    return {"items": items[offset: offset + limit], "total": total}


def _iso_to_epoch(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _stub_get_post(post_id: int) -> dict | None:
    with _lock:
        data = _load()
        post = data["posts"].get(str(post_id))
        if not post:
            return None
        return _hydrate_post(post, data["categories"])


def _stub_create_post(payload: dict) -> dict:
    cleaned = _validate_post_input(payload)
    with _lock:
        data = _load()
        _ensure_seed_categories(data)
        for cid in cleaned["category_ids"]:
            if str(cid) not in data["categories"]:
                raise ValidationError(f"Unknown category id: {cid}", "not_found", 404)
        pid = data["next_post_id"]
        data["next_post_id"] = pid + 1
        now = _now_iso()
        post = {
            "id": pid,
            "title": cleaned["title"],
            "content": cleaned["content"],
            "translations": cleaned["translations"],
            "category_ids": cleaned["category_ids"],
            "image_url": cleaned["image_url"],
            "display_format": cleaned["display_format"],
            "is_pinned": cleaned["is_pinned"],
            "is_boosted": cleaned["is_boosted"],
            "created_at": now,
            "modified_at": now,
            "source_feature_id": cleaned["source_feature_id"],
            "source_feature_set_id": cleaned["source_feature_set_id"],
        }
        post.update(_resolve_status(cleaned["status"], cleaned["scheduled_publish_at"]))
        data["posts"][str(pid)] = post
        _save(data)
        return _hydrate_post(post, data["categories"])


def _stub_update_post(post_id: int, payload: dict) -> dict | None:
    cleaned = _validate_post_input(payload)
    with _lock:
        data = _load()
        existing = data["posts"].get(str(post_id))
        if not existing:
            return None
        for cid in cleaned["category_ids"]:
            if str(cid) not in data["categories"]:
                raise ValidationError(f"Unknown category id: {cid}", "not_found", 404)
        existing["title"] = cleaned["title"]
        existing["content"] = cleaned["content"]
        existing["translations"] = cleaned["translations"]
        existing["category_ids"] = cleaned["category_ids"]
        existing["image_url"] = cleaned["image_url"]
        existing["display_format"] = cleaned["display_format"]
        existing["is_pinned"] = cleaned["is_pinned"]
        existing["is_boosted"] = cleaned["is_boosted"]
        existing["source_feature_id"] = cleaned["source_feature_id"]
        existing["source_feature_set_id"] = cleaned["source_feature_set_id"]
        existing["modified_at"] = _now_iso()
        # Status transition: if publishing now AND was unpublished, set published_at.
        prev_published = existing.get("is_published", False)
        new_state = _resolve_status(cleaned["status"], cleaned["scheduled_publish_at"])
        if new_state["is_published"] and not prev_published:
            new_state["published_at"] = _now_iso()
        elif new_state["is_published"] and prev_published and existing.get("published_at"):
            new_state["published_at"] = existing["published_at"]
        existing.update(new_state)
        _save(data)
        return _hydrate_post(existing, data["categories"])


def _stub_delete_post(post_id: int) -> bool:
    with _lock:
        data = _load()
        if str(post_id) not in data["posts"]:
            return False
        data["posts"].pop(str(post_id))
        _save(data)
        return True


def _stub_list_categories() -> list[dict]:
    with _lock:
        data = _load()
        if _ensure_seed_categories(data):
            _save(data)
        # Compute posts_count
        usage: dict[str, int] = {}
        for p in data["posts"].values():
            for cid in p.get("category_ids") or []:
                usage[str(cid)] = usage.get(str(cid), 0) + 1
        out = []
        for c in data["categories"].values():
            out.append({**c,
                        "translations": c.get("translations") or {},
                        "posts_count": usage.get(str(c["id"]), 0)})
        out.sort(key=lambda c: c["name"].lower())
        return out


def _stub_create_category(payload: dict) -> dict:
    cleaned = _validate_category_input(payload)
    with _lock:
        data = _load()
        for c in data["categories"].values():
            if c["name"].strip().lower() == cleaned["name"].lower():
                raise ValidationError("Category name already exists",
                                      "category_name_taken", 409)
        cid = data["next_category_id"]
        data["next_category_id"] = cid + 1
        cat = {"id": cid, **cleaned}
        data["categories"][str(cid)] = cat
        _save(data)
        return {**cat, "posts_count": 0}


def _stub_update_category(cat_id: int, payload: dict) -> dict | None:
    cleaned = _validate_category_input(payload)
    with _lock:
        data = _load()
        existing = data["categories"].get(str(cat_id))
        if not existing:
            return None
        for c in data["categories"].values():
            if (c["id"] != cat_id
                    and c["name"].strip().lower() == cleaned["name"].lower()):
                raise ValidationError("Category name already exists",
                                      "category_name_taken", 409)
        existing.update(cleaned)
        _save(data)
        usage = sum(1 for p in data["posts"].values()
                    if cat_id in (p.get("category_ids") or []))
        return {**existing, "posts_count": usage}


def _stub_delete_category(cat_id: int) -> dict:
    with _lock:
        data = _load()
        if str(cat_id) not in data["categories"]:
            return {"deleted": False, "id": cat_id, "missing": True}
        usage = sum(1 for p in data["posts"].values()
                    if cat_id in (p.get("category_ids") or []))
        if usage > 0:
            raise ValidationError(
                f"Category in use by {usage} post(s)",
                "category_in_use", 409,
            )
        data["categories"].pop(str(cat_id))
        _save(data)
        return {"deleted": True, "id": cat_id}


# ---------------------------------------------------------------------------
# Live-mode push helpers
# ---------------------------------------------------------------------------
#
# In live (non-stub) mode the local JSON store is still the working copy —
# it owns the local id space, all Amplify-only metadata
# (display_format, scheduled_publish_at, source_feature_id,
# source_feature_set_id) and the local-id ↔ Chartmetric-id mapping.
# Each public CRUD entry-point performs the local mutation first, then
# pushes the *cleaned* payload to Chartmetric via the dedicated client.
# The remote id is persisted back onto the local record so subsequent
# updates / deletes target the right Chartmetric row even after the
# local id space diverges.

# Amplify-only fields that MUST NOT be sent to Chartmetric — the live
# tables (announcement_post / announcement_category) don't have these
# columns. They live only in the Amplify working copy.
_AMPLIFY_ONLY_POST_FIELDS = (
    "display_format",
    "scheduled_publish_at",
    "source_feature_id",
    "source_feature_set_id",
)


def _live_mode_enabled() -> bool:
    return not _stub_mode_enabled()


def _shape_post_payload(local_post: dict, remote_category_ids: list[int]) -> dict:
    """Build the JSON body for POST/PUT /admin/announcement.

    Strips every Amplify-only field and substitutes the local
    ``category_ids`` with their Chartmetric counterparts.
    """
    payload = {
        "title": local_post.get("title") or "",
        "content": local_post.get("content") or [],
        "translations": local_post.get("translations") or {},
        "image_url": local_post.get("image_url"),
        "is_pinned": bool(local_post.get("is_pinned")),
        "is_boosted": bool(local_post.get("is_boosted")),
        "is_published": bool(local_post.get("is_published")),
        "published_at": local_post.get("published_at"),
        "category_ids": list(remote_category_ids),
    }
    # Defensive: never leak any Amplify-only field.
    for k in _AMPLIFY_ONLY_POST_FIELDS:
        payload.pop(k, None)
    return payload


def _shape_category_payload(local_cat: dict) -> dict:
    return {
        "name": local_cat.get("name") or "",
        "color": local_cat.get("color") or "",
        "translations": local_cat.get("translations") or {},
    }


def _resolve_remote_category_ids(local_cat_ids: list[int],
                                  data: dict,
                                  cache: dict | None = None) -> list[int]:
    """Map local category ids to their Chartmetric ids.

    For any local category that doesn't yet have a ``chartmetric_id``,
    we resolve-or-create on Chartmetric by *name* (case-insensitive)
    and stage the mapping on the in-memory category record. The caller
    is responsible for the final ``_save(data)`` — we deliberately do
    NOT persist mid-flight so a downstream Chartmetric failure leaves
    the on-disk store untouched. ``cache`` (passed once for the entire
    save operation) ensures all categories are resolved with a single
    ``GET /admin/announcement/categories`` round-trip.
    """
    from integrations import chartmetric_announcement_client as cmc
    if cache is None:
        cache = {}
    out: list[int] = []
    for cid in local_cat_ids or []:
        local = data["categories"].get(str(cid))
        if not local:
            continue
        remote_id = local.get("chartmetric_id")
        if remote_id:
            out.append(int(remote_id))
            continue
        # Need to find or create on Chartmetric.
        remote = cmc.resolve_or_create_category(
            local["name"], local["color"],
            translations=local.get("translations") or {},
            cache=cache,
        )
        rid = remote.get("id")
        if rid is None:
            raise ValidationError(
                f"Chartmetric returned no id for category {local['name']!r}",
                "chartmetric_error", 502)
        # Stage mapping on the in-memory record. Persisted by caller's
        # _save(data) once the post push succeeds.
        local["chartmetric_id"] = int(rid)
        out.append(int(rid))
    return out


def _coerce_remote_id(payload: dict | None, *, kind: str) -> int:
    """Pull ``id`` out of a Chartmetric success body and coerce to int.

    Raises a shaped :class:`ValidationError` (502 chartmetric_error)
    when the body is missing or contains a non-numeric id, so callers
    never bubble a generic ``TypeError`` / ``ValueError`` to the user.
    """
    raw = (payload or {}).get("id")
    if raw is None:
        raise ValidationError(
            f"Chartmetric {kind} response missing 'id' field",
            "chartmetric_error", 502)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError(
            f"Chartmetric {kind} response has non-numeric id: {raw!r}",
            "chartmetric_error", 502)


def _push_post_to_chartmetric(local_post: dict, data: dict) -> None:
    """Push a local post (create-or-update) to Chartmetric and update
    the local record with the returned remote id.

    Caller MUST hold ``_lock`` and pass the live ``data`` dict — we
    persist the remote-id mapping inline.
    """
    from integrations import chartmetric_announcement_client as cmc
    cat_cache: dict = {}
    remote_cat_ids = _resolve_remote_category_ids(
        local_post.get("category_ids") or [], data, cache=cat_cache)
    body = _shape_post_payload(local_post, remote_cat_ids)
    remote_id = local_post.get("chartmetric_id")
    try:
        if remote_id:
            updated = cmc.update_post(remote_id, body)
            if updated is None:
                # Remote row vanished — re-create.
                created = cmc.create_post(body)
                local_post["chartmetric_id"] = _coerce_remote_id(
                    created, kind="post")
            else:
                # Remote may rewrite the id — keep using it.
                if updated.get("id") is not None:
                    local_post["chartmetric_id"] = _coerce_remote_id(
                        updated, kind="post")
        else:
            created = cmc.create_post(body)
            local_post["chartmetric_id"] = _coerce_remote_id(
                created, kind="post")
        # Replace link-table rows so existing ones not in the new set
        # are removed.
        cmc.replace_post_categories(
            local_post["chartmetric_id"], remote_cat_ids)
    except cmc.ChartmetricClientError as e:
        raise ValidationError(
            f"Chartmetric push failed: {e.short()}",
            "chartmetric_error", 502) from e


def _delete_post_on_chartmetric(remote_id: int | None) -> None:
    if not remote_id:
        return
    from integrations import chartmetric_announcement_client as cmc
    try:
        cmc.delete_post(remote_id)
    except cmc.ChartmetricClientError as e:
        raise ValidationError(
            f"Chartmetric delete failed: {e.short()}",
            "chartmetric_error", 502) from e


def _push_category_to_chartmetric(local_cat: dict) -> None:
    from integrations import chartmetric_announcement_client as cmc
    body = _shape_category_payload(local_cat)
    remote_id = local_cat.get("chartmetric_id")
    try:
        if remote_id:
            updated = cmc.update_category(remote_id, body)
            if updated is None:
                created = cmc.create_category(body)
                local_cat["chartmetric_id"] = _coerce_remote_id(
                    created, kind="category")
            elif updated.get("id") is not None:
                local_cat["chartmetric_id"] = _coerce_remote_id(
                    updated, kind="category")
        else:
            # Resolve-or-create avoids the 409 if the name already exists
            # on Chartmetric (e.g. seeded by another environment).
            cache: dict = {}
            remote = cmc.resolve_or_create_category(
                local_cat["name"], local_cat["color"],
                translations=local_cat.get("translations") or {},
                cache=cache,
            )
            local_cat["chartmetric_id"] = _coerce_remote_id(
                remote, kind="category")
    except cmc.ChartmetricClientError as e:
        raise ValidationError(
            f"Chartmetric push failed: {e.short()}",
            "chartmetric_error", 502) from e


def _delete_category_on_chartmetric(remote_id: int | None) -> None:
    if not remote_id:
        return
    from integrations import chartmetric_announcement_client as cmc
    try:
        cmc.delete_category(remote_id)
    except cmc.ChartmetricClientError as e:
        raise ValidationError(
            f"Chartmetric delete failed: {e.short()}",
            "chartmetric_error", 502) from e


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
#
# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------
# In stub mode all three reads come from the local working copy.
# In live mode list_posts / get_post proxy through the Chartmetric API so
# the admin panel shows canonical production data.  list_categories stays
# local-only because it drives the local category validation and seeding
# logic; a live fetch there would bypass _ensure_seed_categories and break
# the category-id mapping used by create / update.
# Every live read falls back to the local working copy on any client error
# so an upstream outage never breaks the admin UI entirely.

def list_posts(status: str | None = None, category: str | None = None,
               search: str | None = None, offset: int = 0,
               limit: int = 25) -> dict:
    if _stub_mode_enabled():
        return _stub_list_posts(status, category, search, offset, limit)
    from integrations import chartmetric_announcement_client as cmc
    try:
        return cmc.list_posts(status=status, category=category,
                               search=search, offset=offset, limit=limit)
    except cmc.ChartmetricClientError as exc:
        logger.warning(
            "[announcement_store] list_posts live fetch failed (%s); "
            "falling back to local store", exc.short())
        return _stub_list_posts(status, category, search, offset, limit)


def get_post(post_id: int) -> dict | None:
    if _stub_mode_enabled():
        return _stub_get_post(post_id)
    # In live mode, look up the chartmetric_id stored in the local record,
    # then fetch the canonical row from the production API.  Amplify-only
    # fields (display_format / scheduled_publish_at / source_feature_*)
    # are merged back from the local record so the editor still has them.
    with _lock:
        data = _load()
        local = data["posts"].get(str(post_id))
    if not local:
        return None
    cm_id = local.get("chartmetric_id")
    if not cm_id:
        # Post has never been pushed — serve from the local working copy.
        return _hydrate_post(local, data["categories"])
    from integrations import chartmetric_announcement_client as cmc
    try:
        remote = cmc.get_post(cm_id)
        if remote is None:
            return None
        # Re-attach Amplify-only metadata so the editor form is complete.
        for field in _AMPLIFY_ONLY_POST_FIELDS:
            remote.setdefault(field, local.get(field))
        return remote
    except cmc.ChartmetricClientError as exc:
        logger.warning(
            "[announcement_store] get_post live fetch failed (%s); "
            "falling back to local store", exc.short())
        return _hydrate_post(local, data["categories"])


def list_categories() -> list[dict]:
    # Always served from the local working copy — the local store is the
    # authoritative source for the local-id ↔ chartmetric-id mapping used
    # by category validation in create / update.
    return _stub_list_categories()


def create_post(payload: dict) -> dict:
    if _stub_mode_enabled():
        return _stub_create_post(payload)
    # Live mode — local mutation first, then push.
    cleaned = _validate_post_input(payload)
    with _lock:
        data = _load()
        _ensure_seed_categories(data)
        for cid in cleaned["category_ids"]:
            if str(cid) not in data["categories"]:
                raise ValidationError(f"Unknown category id: {cid}",
                                      "not_found", 404)
        pid = data["next_post_id"]
        data["next_post_id"] = pid + 1
        now = _now_iso()
        post = {
            "id": pid,
            "title": cleaned["title"],
            "content": cleaned["content"],
            "translations": cleaned["translations"],
            "category_ids": cleaned["category_ids"],
            "image_url": cleaned["image_url"],
            "display_format": cleaned["display_format"],
            "is_pinned": cleaned["is_pinned"],
            "is_boosted": cleaned["is_boosted"],
            "created_at": now,
            "modified_at": now,
            "source_feature_id": cleaned["source_feature_id"],
            "source_feature_set_id": cleaned["source_feature_set_id"],
            "chartmetric_id": None,
        }
        post.update(_resolve_status(cleaned["status"],
                                     cleaned["scheduled_publish_at"]))
        data["posts"][str(pid)] = post
        _push_post_to_chartmetric(post, data)
        _save(data)
        return _hydrate_post(post, data["categories"])


def update_post(post_id: int, payload: dict) -> dict | None:
    if _stub_mode_enabled():
        return _stub_update_post(post_id, payload)
    cleaned = _validate_post_input(payload)
    with _lock:
        data = _load()
        existing = data["posts"].get(str(post_id))
        if not existing:
            return None
        for cid in cleaned["category_ids"]:
            if str(cid) not in data["categories"]:
                raise ValidationError(f"Unknown category id: {cid}",
                                      "not_found", 404)
        existing["title"] = cleaned["title"]
        existing["content"] = cleaned["content"]
        existing["translations"] = cleaned["translations"]
        existing["category_ids"] = cleaned["category_ids"]
        existing["image_url"] = cleaned["image_url"]
        existing["display_format"] = cleaned["display_format"]
        existing["is_pinned"] = cleaned["is_pinned"]
        existing["is_boosted"] = cleaned["is_boosted"]
        existing["source_feature_id"] = cleaned["source_feature_id"]
        existing["source_feature_set_id"] = cleaned["source_feature_set_id"]
        existing["modified_at"] = _now_iso()
        prev_published = existing.get("is_published", False)
        new_state = _resolve_status(cleaned["status"],
                                     cleaned["scheduled_publish_at"])
        if new_state["is_published"] and not prev_published:
            new_state["published_at"] = _now_iso()
        elif new_state["is_published"] and prev_published and existing.get("published_at"):
            new_state["published_at"] = existing["published_at"]
        existing.update(new_state)
        _push_post_to_chartmetric(existing, data)
        _save(data)
        return _hydrate_post(existing, data["categories"])


def delete_post(post_id: int) -> bool:
    if _stub_mode_enabled():
        return _stub_delete_post(post_id)
    with _lock:
        data = _load()
        existing = data["posts"].get(str(post_id))
        if not existing:
            return False
        _delete_post_on_chartmetric(existing.get("chartmetric_id"))
        data["posts"].pop(str(post_id))
        _save(data)
        return True


def set_post_boost(post_id: int, is_boosted: bool) -> dict | None:
    """Immediately flip ``is_boosted`` on an existing post.

    This entry point is *only* meant for posts that are already
    published AND already synced to Chartmetric — that's the case
    where the marketer wants the boost change to go live without
    re-saving the entire form. For drafts and scheduled posts the
    boost toggle is staged inside the editor form and persisted as
    part of the next Save / Publish. Calling this for a draft /
    scheduled / unsynced post is a programmer error and raises
    ``ValidationError`` so the UI can surface a clear message.

    Returns the updated (hydrated) post dict, or ``None`` if the post
    doesn't exist locally.
    """
    with _lock:
        data = _load()
        existing = data["posts"].get(str(post_id))
        if not existing:
            return None
        if not existing.get("is_published"):
            raise ValidationError(
                "Boost can only be toggled live on published posts; "
                "save the boost setting from the editor for drafts or "
                "scheduled posts.",
                "boost_not_allowed", 409)
        if _live_mode_enabled() and not existing.get("chartmetric_id"):
            raise ValidationError(
                "Post is not synced to Chartmetric yet; save it first "
                "before toggling boost.",
                "boost_not_allowed", 409)
        existing["is_boosted"] = bool(is_boosted)
        existing["modified_at"] = _now_iso()
        if _live_mode_enabled() and existing.get("chartmetric_id"):
            from integrations import chartmetric_announcement_client as cmc
            try:
                cmc.patch_post_boost(existing["chartmetric_id"],
                                      bool(is_boosted))
            except cmc.ChartmetricClientError as e:
                raise ValidationError(
                    f"Chartmetric boost toggle failed: {e.short()}",
                    "chartmetric_error", 502) from e
        _save(data)
        return _hydrate_post(existing, data["categories"])


def create_category(payload: dict) -> dict:
    if _stub_mode_enabled():
        return _stub_create_category(payload)
    cleaned = _validate_category_input(payload)
    with _lock:
        data = _load()
        for c in data["categories"].values():
            if c["name"].strip().lower() == cleaned["name"].lower():
                raise ValidationError("Category name already exists",
                                      "category_name_taken", 409)
        cid = data["next_category_id"]
        data["next_category_id"] = cid + 1
        cat = {"id": cid, "chartmetric_id": None, **cleaned}
        data["categories"][str(cid)] = cat
        _push_category_to_chartmetric(cat)
        _save(data)
        return {**cat, "posts_count": 0}


def update_category(cat_id: int, payload: dict) -> dict | None:
    if _stub_mode_enabled():
        return _stub_update_category(cat_id, payload)
    cleaned = _validate_category_input(payload)
    with _lock:
        data = _load()
        existing = data["categories"].get(str(cat_id))
        if not existing:
            return None
        for c in data["categories"].values():
            if (c["id"] != cat_id
                    and c["name"].strip().lower() == cleaned["name"].lower()):
                raise ValidationError("Category name already exists",
                                      "category_name_taken", 409)
        existing.update(cleaned)
        _push_category_to_chartmetric(existing)
        _save(data)
        usage = sum(1 for p in data["posts"].values()
                    if cat_id in (p.get("category_ids") or []))
        return {**existing, "posts_count": usage}


def delete_category(cat_id: int) -> dict:
    if _stub_mode_enabled():
        return _stub_delete_category(cat_id)
    with _lock:
        data = _load()
        existing = data["categories"].get(str(cat_id))
        if not existing:
            return {"deleted": False, "id": cat_id, "missing": True}
        usage = sum(1 for p in data["posts"].values()
                    if cat_id in (p.get("category_ids") or []))
        if usage > 0:
            raise ValidationError(
                f"Category in use by {usage} post(s)",
                "category_in_use", 409,
            )
        _delete_category_on_chartmetric(existing.get("chartmetric_id"))
        data["categories"].pop(str(cat_id))
        _save(data)
        return {"deleted": True, "id": cat_id}


# ---------------------------------------------------------------------------
# Backward-compat helpers (the old inapp_client.publish_announcement path)
# ---------------------------------------------------------------------------

def publish_announcement_quick(title: str, body: str,
                               feature_id: str | None = None,
                               category: str | None = None) -> dict:
    """Used by the existing /api/publish/inapp endpoint.

    Creates a single-paragraph published announcement so the legacy
    ``Publish to In-App`` button on feature cards keeps working.
    """
    from ai.announcement_serializer import html_to_slate
    body_blocks = html_to_slate(body or "")
    cat_ids: list[int] = []
    if category:
        cats = list_categories()
        match = next((c for c in cats
                      if (c.get("name") or "").lower() == category.lower()), None)
        if match:
            cat_ids = [match["id"]]
    payload = {
        "title": title or "(untitled)",
        "content": body_blocks,
        "translations": {},
        "category_ids": cat_ids,
        "status": "publish_now",
        "source_feature_id": feature_id,
    }
    try:
        post = create_post(payload)
    except ValidationError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.exception("[announcement_store] quick publish failed: %s", e)
        return {"success": False, "error": str(e)}
    return {"success": True, "announcement": post,
            "id": post.get("id")}


# ---------------------------------------------------------------------------
# Connection probe (used by the admin "Test Chartmetric connection" button)
# ---------------------------------------------------------------------------

def ping_chartmetric() -> dict:
    """Probe live Chartmetric admin API and return diagnostics.

    Always returns a dict — never raises — so the route handler can
    serialize the result directly.
    """
    if _stub_mode_enabled():
        return {
            "ok": False,
            "stub_mode": True,
            "base_url": getattr(config, "CHARTMETRIC_ADMIN_API_BASE_URL", "") or None,
            "token_configured": bool(getattr(config, "CHARTMETRIC_ADMIN_API_TOKEN", "")),
            "error": "Stub mode is enabled — set both "
                     "CHARTMETRIC_ADMIN_API_BASE_URL and "
                     "CHARTMETRIC_ADMIN_API_TOKEN to go live "
                     "(and unset the ANNOUNCEMENTS_STUB_MODE kill switch "
                     "if it's set).",
        }
    from integrations import chartmetric_announcement_client as cmc
    info = cmc.ping()
    info["stub_mode"] = False
    return info
