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


def get_cached_external_thumb(url: str) -> str:
    """Fetch external thumbnail URL, composite play button, cache locally.

    Returns the cache key (filename stem) on success, or empty string on failure.
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
