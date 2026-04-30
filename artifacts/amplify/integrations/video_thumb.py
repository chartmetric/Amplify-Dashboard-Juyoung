import hashlib
import io
import logging
import os
import re

logger = logging.getLogger(__name__)

_CACHE_DIR = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", ".publish_videos", "_external_cache")
)


def _ensure_cache_dir():
    os.makedirs(_CACHE_DIR, exist_ok=True)


def composite_play_button(image_bytes: bytes) -> bytes:
    from PIL import Image, ImageDraw

    src = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = src.size

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    radius = int(min(w, h) * 0.11)
    radius = max(28, min(radius, 80))
    cx, cy = w // 2, h // 2

    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill=(0, 0, 0, 180),
    )

    tri_size = int(radius * 1.0)
    offset = int(radius * 0.12)
    triangle = [
        (cx - tri_size // 2 + offset, cy - tri_size // 2),
        (cx - tri_size // 2 + offset, cy + tri_size // 2),
        (cx + tri_size // 2 + offset, cy),
    ]
    draw.polygon(triangle, fill=(255, 255, 255, 255))

    composed = Image.alpha_composite(src.convert("RGBA"), overlay).convert("RGB")
    out = io.BytesIO()
    composed.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


def composite_play_button_file(thumb_path: str) -> bool:
    try:
        with open(thumb_path, "rb") as f:
            data = f.read()
        out = composite_play_button(data)
        with open(thumb_path, "wb") as f:
            f.write(out)
        return True
    except Exception as e:
        logger.warning(f"[video_thumb] composite failed for {thumb_path}: {e}")
        return False


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def _s3_external_thumb_key(key: str) -> str:
    return f"external-thumbs/{key}.jpg"


def get_cached_external_thumb(url: str) -> str:
    """Fetch external thumbnail URL, composite play button, cache locally.

    Returns the cache key (filename stem) on success, or empty string on failure.
    Also uploads the composited JPEG to S3 (kind=external-thumbs) when the
    durable backend is enabled so the serve route can 302-redirect later.
    """
    _ensure_cache_dir()
    key = _cache_key(url)
    path = os.path.join(_CACHE_DIR, key + ".jpg")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return key

    try:
        import requests
        resp = requests.get(url, timeout=10, stream=True)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "image" not in ctype.lower():
            logger.warning(f"[video_thumb] external thumb not image: {url} ({ctype})")
            return ""
        raw = resp.content
        if not raw:
            return ""
        out = composite_play_button(raw)
        with open(path, "wb") as f:
            f.write(out)
        # Best-effort S3 upload; serving falls back to local on failure.
        try:
            from integrations import attachment_store as _astore
            _astore.put(
                kind="external-thumbs",
                key_hint=_s3_external_thumb_key(key),
                raw_bytes=out,
                content_type="image/jpeg",
            )
        except Exception as _e:
            logger.warning(f"[attachments] kind=external-thumbs S3 upload skipped: {_e}")
        return key
    except Exception as e:
        logger.warning(f"[video_thumb] external thumb fetch/composite failed for {url}: {e}")
        return ""


def get_cached_external_thumb_path(key: str) -> str:
    if not key or not re.match(r"^[a-f0-9]{1,64}$", key):
        return ""
    path = os.path.join(_CACHE_DIR, key + ".jpg")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    return ""


def get_external_thumb_s3_key(key: str) -> str:
    """Return the S3 object key for a cached external thumb, or ''.

    Callers should pass the result through ``attachment_store.s3_serve_url``
    to get a working serve URL. We deliberately do not return a URL
    here — the bucket is private, so a public-form URL would 403 and
    break thumbs in the email.
    """
    if not key or not re.match(r"^[a-f0-9]{1,64}$", key):
        return ""
    try:
        from integrations import attachment_store as _astore
        if not _astore.s3_enabled():
            return ""
        return _s3_external_thumb_key(key) or ""
    except Exception:
        return ""


def get_external_thumb_s3_url(key: str) -> str:
    """Deprecated: returns a public S3 URL that 403s on private buckets.

    Kept as a compatibility shim for any out-of-tree caller. New code
    should call :func:`get_external_thumb_s3_key` and pass the key
    through :func:`attachment_store.s3_serve_url`.
    """
    s3_key = get_external_thumb_s3_key(key)
    if not s3_key:
        return ""
    try:
        from integrations import attachment_store as _astore
        return _astore.s3_public_url(s3_key) or ""
    except Exception:
        return ""
