"""Storage backend for the in-app announcements admin.

Two modes, switched by ``ANNOUNCEMENTS_STUB_MODE``:

  * Stub (``true``, default): JSON-cache + in-memory store at
    ``.announcement_store.json`` modeled after ``ai/feature_sets.py`` and
    ``ai/classifier.py`` — survives restarts, no network. Lets the admin
    UI run end-to-end before chartmetric-api implements the matching
    endpoints documented in ``docs/chartmetric-announcement-admin-api.md``.

  * Proxy (``false``): Forwards every operation to
    ``CHARTMETRIC_ADMIN_API_BASE_URL`` with
    ``Authorization: Bearer ${CHARTMETRIC_ADMIN_API_TOKEN}``.

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
    """Read at call time so tests / runtime config flips work."""
    if not getattr(config, "CHARTMETRIC_ADMIN_API_BASE_URL", "").strip():
        return True
    val = os.environ.get("ANNOUNCEMENTS_STUB_MODE", "true").strip().lower()
    return val not in ("0", "false", "no", "off")


def get_mode_info() -> dict:
    return {
        "stub_mode": _stub_mode_enabled(),
        "base_url": getattr(config, "CHARTMETRIC_ADMIN_API_BASE_URL", "") or None,
        "token_configured": bool(getattr(config, "CHARTMETRIC_ADMIN_API_TOKEN", "")),
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
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("translations must be an object")
    out: dict[str, dict] = {}
    for lang, blob in value.items():
        if lang not in TARGET_LOCALES:
            continue
        if not isinstance(blob, dict):
            continue
        keep: dict[str, Any] = {}
        for f in fields:
            if f in blob:
                keep[f] = blob[f]
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
# Proxy mode
# ---------------------------------------------------------------------------

def _proxy_request(method: str, path: str,
                   params: dict | None = None,
                   body: Any = None,
                   files: Any = None,
                   timeout: float = 15.0):
    """Forward a request to chartmetric-api admin endpoints."""
    import requests
    base_url = getattr(config, "CHARTMETRIC_ADMIN_API_BASE_URL", "").rstrip("/")
    if not base_url:
        raise RuntimeError("CHARTMETRIC_ADMIN_API_BASE_URL not configured")
    url = f"{base_url}{path}"
    headers = {}
    token = getattr(config, "CHARTMETRIC_ADMIN_API_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None and files is None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    resp = requests.request(
        method.upper(), url,
        params=params,
        json=body if body is not None and files is None else None,
        files=files,
        headers=headers,
        timeout=timeout,
    )
    return resp


def _proxy_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {"error": resp.text or f"Unexpected status {resp.status_code}"}


# ---------------------------------------------------------------------------
# Public API (mode-dispatching)
# ---------------------------------------------------------------------------

def list_posts(status: str | None = None, category: str | None = None,
               search: str | None = None, offset: int = 0,
               limit: int = 25) -> dict:
    if _stub_mode_enabled():
        return _stub_list_posts(status, category, search, offset, limit)
    resp = _proxy_request("GET", "/admin/announcement",
                          params={"status": status or "all",
                                  "category": category or "",
                                  "search": search or "",
                                  "offset": offset, "limit": limit})
    return _proxy_json(resp)


def get_post(post_id: int) -> dict | None:
    if _stub_mode_enabled():
        return _stub_get_post(post_id)
    resp = _proxy_request("GET", f"/admin/announcement/{post_id}")
    if resp.status_code == 404:
        return None
    return _proxy_json(resp)


def create_post(payload: dict) -> dict:
    if _stub_mode_enabled():
        return _stub_create_post(payload)
    resp = _proxy_request("POST", "/admin/announcement", body=payload)
    return _proxy_json(resp)


def update_post(post_id: int, payload: dict) -> dict | None:
    if _stub_mode_enabled():
        return _stub_update_post(post_id, payload)
    resp = _proxy_request("PUT", f"/admin/announcement/{post_id}", body=payload)
    if resp.status_code == 404:
        return None
    return _proxy_json(resp)


def delete_post(post_id: int) -> bool:
    if _stub_mode_enabled():
        return _stub_delete_post(post_id)
    resp = _proxy_request("DELETE", f"/admin/announcement/{post_id}")
    return resp.status_code in (200, 204)


def list_categories() -> list[dict]:
    if _stub_mode_enabled():
        return _stub_list_categories()
    resp = _proxy_request("GET", "/admin/announcement/categories")
    return _proxy_json(resp) or []


def create_category(payload: dict) -> dict:
    if _stub_mode_enabled():
        return _stub_create_category(payload)
    resp = _proxy_request("POST", "/admin/announcement/categories", body=payload)
    return _proxy_json(resp)


def update_category(cat_id: int, payload: dict) -> dict | None:
    if _stub_mode_enabled():
        return _stub_update_category(cat_id, payload)
    resp = _proxy_request("PUT", f"/admin/announcement/categories/{cat_id}", body=payload)
    if resp.status_code == 404:
        return None
    return _proxy_json(resp)


def delete_category(cat_id: int) -> dict:
    if _stub_mode_enabled():
        return _stub_delete_category(cat_id)
    resp = _proxy_request("DELETE", f"/admin/announcement/categories/{cat_id}")
    if resp.status_code in (200, 204):
        return {"deleted": True, "id": cat_id}
    body = _proxy_json(resp)
    raise ValidationError(body.get("error") or "Delete failed",
                          body.get("code") or "delete_failed",
                          resp.status_code)


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
