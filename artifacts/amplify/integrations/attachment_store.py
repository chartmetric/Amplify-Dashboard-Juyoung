"""Shared attachment storage seam (Task #99).

This module is the single place the rest of the codebase asks
"persist these bytes for me and tell me where to fetch them later". It
replaces the hosted-images-only seam that used to live inside
``sendgrid_client.py`` and lifts it to cover every attachment kind:

  * feature images / GIFs    (kind=``feature-images``)
  * normalized videos        (kind=``videos``)
  * generated video thumbs   (kind=``video-thumbs``)
  * cached external thumbs   (kind=``external-thumbs``)
  * email-hosted images      (kind=``hosted-emails``)
  * announcement uploads     (kind=``announcements``)

The active backend is selected by ``AMPLIFY_IMAGE_STORAGE_BACKEND``
(historic name kept for compatibility) and currently supports:
  * ``local`` (default) — caller falls back to the existing
    disk + Postgres path it already uses.
  * ``s3`` — uploads to AWS S3 using these four secrets:
        S3_Bucket_name, S3_Region, S3_Access_Key, S3_Secret_Access_Key

The seam is *additive*: even when ``s3`` is selected, callers should
still write to the local backend so a single S3 outage never loses
content. ``put`` returns ``{"backend": "s3"|"local", "url": ..., "key": ...}``
or ``{"backend": "local", "url": None, "key": None}`` when S3 is off /
failed; the caller then knows whether to record an S3 key alongside the
row.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

S3_SECRET_NAMES = (
    "S3_Bucket_name",
    "S3_Region",
    "S3_Access_Key",
    "S3_Secret_Access_Key",
)

KIND_PREFIX = {
    "feature-images": "feature-images",
    "videos": "videos",
    "video-thumbs": "videos",  # thumbnails sit next to their videos
    "external-thumbs": "external-thumbs",
    "hosted-emails": "hosted-emails",
    "announcements": "announcements",
}

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
    "mkv": "video/x-matroska",
}

_SAFE_KEY_RE = re.compile(r"[^a-zA-Z0-9._\-/]+")


def get_backend_name() -> str:
    """Return the active backend name (lower-cased, defaulting to ``local``)."""
    return (os.environ.get("AMPLIFY_IMAGE_STORAGE_BACKEND") or "local").strip().lower()


def secrets_present() -> dict:
    """Return ``{secret_name: bool}`` indicating which S3 secrets are set."""
    out = {}
    for name in S3_SECRET_NAMES:
        out[name] = bool((os.environ.get(name) or "").strip())
    return out


def s3_enabled() -> bool:
    """True only when the backend is ``s3`` AND every required secret is set."""
    if get_backend_name() != "s3":
        return False
    present = secrets_present()
    return all(present.values())


# ---------------------------------------------------------------------------
# Recent-uploads ring buffer (used by the admin status endpoint).
# ---------------------------------------------------------------------------
_RECENT: "deque[dict]" = deque(maxlen=50)
_RECENT_LOCK = threading.Lock()


def _record_recent(entry: dict) -> None:
    entry = dict(entry)
    entry.setdefault("ts", time.time())
    with _RECENT_LOCK:
        _RECENT.appendleft(entry)


def recent_uploads(limit: int = 25) -> list:
    with _RECENT_LOCK:
        items = list(_RECENT)
    if limit and limit > 0:
        items = items[: int(limit)]
    return items


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _safe_key_segment(s: str, fallback: str = "") -> str:
    s = (s or "").strip().lstrip("/").rstrip("/")
    s = _SAFE_KEY_RE.sub("-", s)
    s = s.strip("-/.") or fallback
    return s[:200]


def _build_key(kind: str, key_hint: str, ext: str) -> str:
    """Compose a stable, collision-free S3 object key for ``kind``.

    The hint is sanitized and combined with a uuid suffix so retries /
    duplicate uploads for the same logical row don't collide. Callers
    that want a deterministic key (e.g. ``videos/<id>/thumb.jpg``) can
    pass that exact path as ``key_hint`` and we'll keep it.
    """
    prefix = KIND_PREFIX.get(kind, kind or "misc")
    safe_ext = (ext or "").lower().lstrip(".") or "bin"
    if not safe_ext.replace("-", "").isalnum():
        safe_ext = "bin"

    hint = (key_hint or "").strip().lstrip("/")
    if hint and hint.startswith(prefix + "/"):
        return hint
    if hint:
        # Treat hint as a "stem" — append a uuid to avoid collisions
        # unless it already looks like a full filename.
        if hint.endswith("." + safe_ext):
            stem = hint[: -(len(safe_ext) + 1)]
        else:
            stem = hint
        stem = _safe_key_segment(stem, fallback="item")
        suffix = uuid.uuid4().hex[:12]
        return f"{prefix}/{stem}-{suffix}.{safe_ext}"
    return f"{prefix}/{uuid.uuid4().hex}.{safe_ext}"


def _content_type_from(content_type: Optional[str], key: str) -> str:
    if content_type:
        return content_type
    ext = os.path.splitext(key)[1].lstrip(".").lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _s3_client():
    """Return a configured boto3 S3 client or ``None`` when unavailable."""
    region = (os.environ.get("S3_Region") or "").strip()
    access_key = (os.environ.get("S3_Access_Key") or "").strip()
    secret_key = (os.environ.get("S3_Secret_Access_Key") or "").strip()
    try:
        import boto3  # type: ignore
    except Exception as e:
        logger.error(f"[attachments] boto3 not available: {e}")
        return None
    try:
        return boto3.client(
            "s3",
            region_name=region or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
        )
    except Exception as e:
        logger.error(f"[attachments] boto3 client creation failed: {e}")
        return None


def s3_public_url(key: str) -> Optional[str]:
    """Return the virtual-hosted-style public URL for ``key`` or ``None``."""
    bucket = (os.environ.get("S3_Bucket_name") or "").strip()
    region = (os.environ.get("S3_Region") or "").strip()
    if not bucket or not region or not key:
        return None
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def s3_presigned_url(key: str, ttl_seconds: int = 3600) -> Optional[str]:
    """Return a short-lived presigned GET URL for a private object."""
    bucket = (os.environ.get("S3_Bucket_name") or "").strip()
    if not bucket or not key:
        return None
    client = _s3_client()
    if client is None:
        return None
    try:
        return client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=int(ttl_seconds),
        )
    except Exception as e:
        logger.warning(f"[attachments] presign failed for key={key!r}: {e}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def put(
    kind: str,
    key_hint: str,
    raw_bytes: bytes,
    content_type: Optional[str] = None,
) -> dict:
    """Persist ``raw_bytes`` to the active backend and return where it went.

    Always returns a dict with the following shape:

        {"backend": "s3"|"local", "url": str|None, "key": str|None,
         "error": str|None, "kind": kind, "bytes": int}

    When the backend is ``local`` (or S3 fails), ``url`` and ``key`` are
    ``None`` — the caller is expected to fall back to its existing
    on-disk + Postgres path.
    """
    n = len(raw_bytes or b"")
    base = {
        "backend": "local",
        "url": None,
        "key": None,
        "error": None,
        "kind": kind,
        "bytes": n,
    }
    if not raw_bytes:
        base["error"] = "empty_bytes"
        return base

    if get_backend_name() != "s3":
        return base

    bucket = (os.environ.get("S3_Bucket_name") or "").strip()
    missing = [n_ for n_, v in secrets_present().items() if not v]
    if missing:
        msg = f"missing secrets {missing}"
        logger.error(f"[attachments] kind={kind} backend=s3 {msg}")
        base["error"] = msg
        _record_recent({**base, "ok": False})
        return base

    ext = os.path.splitext(key_hint or "")[1].lstrip(".").lower()
    if not ext:
        guess = (content_type or "").split("/")[-1].split(";")[0].strip().lower()
        if guess and guess.replace("+", "").isalnum():
            ext = guess
    key = _build_key(kind, key_hint, ext)
    ctype = _content_type_from(content_type, key)

    client = _s3_client()
    if client is None:
        base["error"] = "boto3_unavailable"
        _record_recent({**base, "ok": False})
        return base

    try:
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
        try:
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=raw_bytes,
                ContentType=ctype,
                CacheControl="public, max-age=31536000, immutable",
            )
        except (BotoCoreError, ClientError) as e:
            err = repr(e)
            logger.error(
                f"[attachments] kind={kind} backend=s3 bytes={n} "
                f"error={err} key={key!r} bucket={bucket!r}"
            )
            base["error"] = err
            _record_recent({**base, "ok": False, "key": key})
            return base
    except Exception as e:
        err = repr(e)
        logger.error(f"[attachments] kind={kind} backend=s3 bytes={n} error={err}")
        base["error"] = err
        _record_recent({**base, "ok": False, "key": key})
        return base

    url = s3_public_url(key)
    logger.info(
        f"[attachments] kind={kind} backend=s3 bytes={n} key={key} "
        f"ctype={ctype}"
    )
    out = {
        "backend": "s3",
        "url": url,
        "key": key,
        "error": None,
        "kind": kind,
        "bytes": n,
    }
    _record_recent({**out, "ok": True})
    return out


def delete(key: str) -> bool:
    """Best-effort delete of a stored S3 object. Returns True on success."""
    if not key or get_backend_name() != "s3":
        return False
    bucket = (os.environ.get("S3_Bucket_name") or "").strip()
    if not bucket:
        return False
    client = _s3_client()
    if client is None:
        return False
    try:
        client.delete_object(Bucket=bucket, Key=key)
        logger.info(f"[attachments] kind=delete backend=s3 key={key}")
        return True
    except Exception as e:
        logger.warning(f"[attachments] delete failed for key={key!r}: {e}")
        return False


def get_url(kind: str, key: str, presign_ttl: int = 3600) -> Optional[str]:
    """Return the URL to serve an S3-stored object.

    Tries the public URL form first; if presigning would be needed, the
    caller should pass ``presign_ttl > 0``. The serve route always
    returns the public URL — for private buckets, swap to
    :func:`s3_presigned_url` instead.
    """
    if not key:
        return None
    return s3_public_url(key)


# Default TTL for serve-time presigned URLs. One hour is plenty: email
# clients (Gmail in particular) proxy and cache the image at the first
# fetch, so this only needs to outlive the single open that triggers a
# server hit. For non-proxying clients (Outlook, Apple Mail), every
# open re-hits our Flask route and mints a fresh URL.
_SERVE_PRESIGN_TTL_SECONDS = 3600


def s3_serve_url(key: str) -> Optional[str]:
    """Return the URL the serve routes should 302-redirect to for ``key``.

    Today this returns a short-lived presigned URL because the bucket
    is private (recipients can't read the virtual-hosted public URL —
    S3 returns 403). When/if the bucket is configured for public read,
    this can be flipped back to :func:`s3_public_url` so email clients
    cache more aggressively. All serve routes funnel through this one
    helper so that change is a single-line edit.
    """
    if not key:
        return None
    return s3_presigned_url(key, ttl_seconds=_SERVE_PRESIGN_TTL_SECONDS)
