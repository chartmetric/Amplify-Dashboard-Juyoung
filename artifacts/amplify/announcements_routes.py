"""Flask blueprint for the in-app announcements admin (Task #91).

Routes (all live under the dashboard host):

  GET    /announcements                       -> render the admin SPA
  GET    /api/admin/announcements             -> list posts (filters)
  POST   /api/admin/announcements             -> create post
  GET    /api/admin/announcements/<id>        -> get post
  PUT    /api/admin/announcements/<id>        -> update post
  DELETE /api/admin/announcements/<id>        -> delete post
  GET    /api/admin/announcement-categories   -> list categories
  POST   /api/admin/announcement-categories   -> create category
  PUT    /api/admin/announcement-categories/<id> -> update category
  DELETE /api/admin/announcement-categories/<id> -> delete category
  POST   /api/admin/announcements/translate   -> Claude auto-translate post
  POST   /api/admin/announcement-categories/translate -> auto-translate category
  POST   /api/admin/announcements/upload      -> upload image/video
  GET    /api/admin/announcement-mode         -> {stub_mode, base_url, ...}
  GET    /api/admin/announcement-uploads/<filename> -> serve uploaded media

In stub mode, uploads land under ``./.announcement_uploads`` and are
served back from the same host. In proxy mode, uploads are forwarded
to ``CHARTMETRIC_MEDIA_UPLOAD_URL`` and that URL's response payload is
returned to the client unchanged.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import secrets
import time

from flask import (Blueprint, abort, jsonify, render_template, request,
                   send_from_directory)

import config
from ai import announcement_store, announcement_translator
from ai.announcement_store import ValidationError

logger = logging.getLogger("amplify.announcements_routes")

bp = Blueprint("announcements_admin", __name__)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), ".announcement_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
ALLOWED_VIDEO_EXTS = {".mp4", ".webm", ".mov"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _validation_response(e: ValidationError):
    return jsonify({"success": False, "code": e.code, "error": str(e)}), e.status_code


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@bp.route("/announcements")
def announcements_page():
    return render_template("announcements.html")


# ---------------------------------------------------------------------------
# Mode info (used by the UI to show a "Stub mode" banner when applicable).
# ---------------------------------------------------------------------------

@bp.route("/api/admin/announcement-mode", methods=["GET"])
def announcement_mode():
    info = announcement_store.get_mode_info()
    return jsonify({"success": True, **info}), 200


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

@bp.route("/api/admin/announcements", methods=["GET"])
def list_posts_endpoint():
    status = request.args.get("status") or None
    category = request.args.get("category") or None
    search = request.args.get("search") or None
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
        limit = min(max(int(request.args.get("limit", 25)), 1), 200)
    except ValueError:
        return jsonify({"success": False, "error": "offset/limit must be ints"}), 400
    try:
        result = announcement_store.list_posts(status=status, category=category,
                                                search=search, offset=offset, limit=limit)
    except ValidationError as e:
        return _validation_response(e)
    except Exception as e:
        logger.exception("[announcements] list failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, **result}), 200


@bp.route("/api/admin/announcements", methods=["POST"])
def create_post_endpoint():
    payload = request.get_json(silent=True) or {}
    try:
        post = announcement_store.create_post(payload)
    except ValidationError as e:
        return _validation_response(e)
    except Exception as e:
        logger.exception("[announcements] create failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "post": post}), 201


@bp.route("/api/admin/announcements/<int:post_id>", methods=["GET"])
def get_post_endpoint(post_id: int):
    post = announcement_store.get_post(post_id)
    if post is None:
        return jsonify({"success": False, "error": "Not found"}), 404
    return jsonify({"success": True, "post": post}), 200


@bp.route("/api/admin/announcements/<int:post_id>", methods=["PUT"])
def update_post_endpoint(post_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        post = announcement_store.update_post(post_id, payload)
    except ValidationError as e:
        return _validation_response(e)
    except Exception as e:
        logger.exception("[announcements] update %s failed: %s", post_id, e)
        return jsonify({"success": False, "error": str(e)}), 500
    if post is None:
        return jsonify({"success": False, "error": "Not found"}), 404
    return jsonify({"success": True, "post": post}), 200


@bp.route("/api/admin/announcements/<int:post_id>", methods=["DELETE"])
def delete_post_endpoint(post_id: int):
    ok = announcement_store.delete_post(post_id)
    if not ok:
        return jsonify({"success": False, "error": "Not found"}), 404
    return jsonify({"success": True, "id": post_id}), 200


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@bp.route("/api/admin/announcement-categories", methods=["GET"])
def list_categories_endpoint():
    cats = announcement_store.list_categories()
    return jsonify({"success": True, "categories": cats}), 200


@bp.route("/api/admin/announcement-categories", methods=["POST"])
def create_category_endpoint():
    payload = request.get_json(silent=True) or {}
    try:
        cat = announcement_store.create_category(payload)
    except ValidationError as e:
        return _validation_response(e)
    except Exception as e:
        logger.exception("[announcements] create category failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "category": cat}), 201


@bp.route("/api/admin/announcement-categories/<int:cat_id>", methods=["PUT"])
def update_category_endpoint(cat_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        cat = announcement_store.update_category(cat_id, payload)
    except ValidationError as e:
        return _validation_response(e)
    except Exception as e:
        logger.exception("[announcements] update category failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    if cat is None:
        return jsonify({"success": False, "error": "Not found"}), 404
    return jsonify({"success": True, "category": cat}), 200


@bp.route("/api/admin/announcement-categories/<int:cat_id>", methods=["DELETE"])
def delete_category_endpoint(cat_id: int):
    try:
        result = announcement_store.delete_category(cat_id)
    except ValidationError as e:
        return _validation_response(e)
    if not result.get("deleted"):
        return jsonify({"success": False, "error": "Not found"}), 404
    return jsonify({"success": True, **result}), 200


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------

@bp.route("/api/admin/announcements/translate", methods=["POST"])
def translate_post_endpoint():
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    content = payload.get("content") or []
    if not title:
        return jsonify({"success": False, "error": "title is required"}), 400
    if not isinstance(content, list):
        return jsonify({"success": False, "error": "content must be a Slate.js block array"}), 400
    t0 = time.time()
    translations = announcement_translator.translate_post(title=title, content_blocks=content)
    dt = (time.time() - t0) * 1000
    logger.info("[announcements] translate_post dt=%.0fms langs=%s", dt, list(translations.keys()))
    return jsonify({
        "success": True,
        "translations": translations,
        "claude_configured": bool(config.ANTHROPIC_API_KEY),
        "elapsed_ms": int(dt),
    }), 200


@bp.route("/api/admin/announcement-categories/translate", methods=["POST"])
def translate_category_endpoint():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name is required"}), 400
    translations = announcement_translator.translate_category(name)
    return jsonify({
        "success": True,
        "translations": translations,
        "claude_configured": bool(config.ANTHROPIC_API_KEY),
    }), 200


# ---------------------------------------------------------------------------
# Media upload
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    name = (name or "upload").strip().replace(" ", "_")
    name = _SAFE_NAME_RE.sub("", name) or "upload"
    return name[-80:]


@bp.route("/api/admin/announcements/upload", methods=["POST"])
def upload_media_endpoint():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "file part is required"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"success": False, "error": "empty filename"}), 400
    name = _safe_name(f.filename)
    ext = os.path.splitext(name)[1].lower()
    kind = "image" if ext in ALLOWED_IMAGE_EXTS else (
        "video" if ext in ALLOWED_VIDEO_EXTS else "")
    if not kind:
        return jsonify({"success": False, "error": f"Unsupported file type {ext}"}), 415
    raw = f.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        return jsonify({"success": False,
                        "error": f"File too large ({len(raw)} > {MAX_UPLOAD_BYTES})"}), 413

    # Proxy mode: forward to chartmetric-api media uploader.
    if (not announcement_store._stub_mode_enabled()
            and getattr(config, "CHARTMETRIC_MEDIA_UPLOAD_URL", "")):
        try:
            import requests
            headers = {}
            token = getattr(config, "CHARTMETRIC_ADMIN_API_TOKEN", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            resp = requests.post(
                config.CHARTMETRIC_MEDIA_UPLOAD_URL,
                files={"file": (name, raw, f.mimetype or mimetypes.guess_type(name)[0]
                                or "application/octet-stream")},
                headers=headers,
                timeout=60,
            )
            try:
                body = resp.json()
            except Exception:
                body = {"error": resp.text or f"Status {resp.status_code}"}
            if resp.status_code >= 400:
                return jsonify({"success": False, "error": body.get("error") or "Upload failed",
                                "status": resp.status_code}), resp.status_code
            url = body.get("url") or body.get("location")
            if not url:
                return jsonify({"success": False,
                                "error": "Upstream upload did not return a URL",
                                "upstream": body}), 502
            return jsonify({"success": True, "url": url, "kind": kind,
                            "filename": name, "size": len(raw),
                            "upstream": body}), 200
        except Exception as e:
            logger.exception("[announcements] proxy upload failed: %s", e)
            return jsonify({"success": False, "error": str(e)}), 502

    # Stub mode: persist locally and return a public URL the dashboard host
    # can serve back from.
    token = secrets.token_urlsafe(8).replace("-", "").replace("_", "")
    stored_name = f"{int(time.time())}_{token}_{name}"
    target_path = os.path.join(UPLOAD_DIR, stored_name)
    try:
        with open(target_path, "wb") as out:
            out.write(raw)
    except Exception as e:
        logger.exception("[announcements] write %s failed: %s", target_path, e)
        return jsonify({"success": False, "error": str(e)}), 500
    url = f"/api/admin/announcement-uploads/{stored_name}"
    return jsonify({"success": True, "url": url, "kind": kind,
                    "filename": name, "size": len(raw),
                    "stored_as": stored_name}), 201


@bp.route("/api/admin/announcement-uploads/<path:filename>", methods=["GET"])
def serve_upload(filename: str):
    safe = os.path.basename(filename)
    full = os.path.join(UPLOAD_DIR, safe)
    if not os.path.isfile(full):
        return abort(404)
    return send_from_directory(UPLOAD_DIR, safe, conditional=True)


# ---------------------------------------------------------------------------
# Pre-fill from Feature / FeatureSet
# ---------------------------------------------------------------------------

@bp.route("/api/admin/announcements/prefill-from-feature/<feature_id>", methods=["GET"])
def prefill_from_feature(feature_id: str):
    """Look up a feature (across asana/slack/manual sources) and return a
    composer-ready ``{title, content_html}`` payload so the marketer can
    start a new post pre-populated with the feature's title and bulleted
    summary."""
    try:
        from app import _resolve_feature_for_id  # type: ignore
    except Exception:
        _resolve_feature_for_id = None  # type: ignore

    feature = None
    if _resolve_feature_for_id is not None:
        try:
            feature = _resolve_feature_for_id(feature_id)
        except Exception as e:
            logger.warning("[announcements] feature resolver failed: %s", e)

    if feature is None:
        # Fallback: walk classification cache JSON.
        try:
            cache_path = os.path.join(os.path.dirname(__file__),
                                      ".classification_cache.json")
            if os.path.exists(cache_path):
                import json
                with open(cache_path, "r") as f:
                    cache = json.load(f) or {}
                feature = (cache.get(feature_id)
                           or cache.get("classifications", {}).get(feature_id))
        except Exception as e:
            logger.warning("[announcements] cache fallback failed: %s", e)

    if not feature:
        return jsonify({"success": False, "error": "Feature not found",
                        "feature_id": feature_id}), 404

    title = feature.get("title") or feature.get("name") or feature_id
    description = (feature.get("description")
                   or feature.get("summary")
                   or feature.get("body") or "")
    bullets = feature.get("bullets") or feature.get("highlights") or []
    parts = []
    if description:
        parts.append(f"<p>{_escape(description)}</p>")
    if bullets:
        parts.append("<ul>" + "".join(f"<li>{_escape(str(b))}</li>"
                                        for b in bullets) + "</ul>")
    if not parts:
        parts.append("<p></p>")
    return jsonify({
        "success": True,
        "title": title,
        "content_html": "".join(parts),
        "source_feature_id": feature_id,
    }), 200


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register(app) -> None:
    """Attach the blueprint to a Flask app and log registration."""
    app.register_blueprint(bp)
    logger.info("[announcements] admin blueprint registered (stub_mode=%s)",
                announcement_store.get_mode_info().get("stub_mode"))
