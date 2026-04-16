import json
import os
import re
import tempfile
import logging
import base64
import subprocess
import uuid

logger = logging.getLogger(__name__)

PUBLISH_FILE = os.path.join(os.path.dirname(__file__), "..", ".publish_state.json")
IMAGES_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".publish_images"))
VIDEOS_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".publish_videos"))

VALID_CHANNELS = {"twitter", "email_newsletter", "email_short", "email_medium", "email_long", "email_standalone", "inapp", "linkedin", "notion_monthly", "article_hmc"}
_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-.]")

MAX_VIDEO_SIZE = 50 * 1024 * 1024


def _ensure_dirs():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(VIDEOS_DIR, exist_ok=True)


def _sanitize(val):
    return _SAFE_RE.sub("_", val)


def _validate_channel(channel):
    if channel not in VALID_CHANNELS:
        raise ValueError(f"Invalid channel: {channel}")


def _load():
    try:
        with open(PUBLISH_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.realpath(PUBLISH_FILE)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, PUBLISH_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _key(feature_id, channel):
    return f"{feature_id}:{channel}"


def _safe_path(feature_id, channel):
    _validate_channel(channel)
    safe_fid = _sanitize(feature_id)
    return safe_fid, channel


def _image_path_feature(feature_id):
    safe_fid = _sanitize(feature_id)
    p = os.path.realpath(os.path.join(IMAGES_DIR, f"{safe_fid}.img"))
    if not p.startswith(IMAGES_DIR):
        raise ValueError("Invalid path")
    return p


def _meta_path_feature(feature_id):
    safe_fid = _sanitize(feature_id)
    p = os.path.realpath(os.path.join(IMAGES_DIR, f"{safe_fid}.meta.json"))
    if not p.startswith(IMAGES_DIR):
        raise ValueError("Invalid path")
    return p


def _legacy_image_path(feature_id, channel):
    safe_fid, safe_ch = _safe_path(feature_id, channel)
    p = os.path.realpath(os.path.join(IMAGES_DIR, f"{safe_fid}__{safe_ch}.img"))
    if not p.startswith(IMAGES_DIR):
        raise ValueError("Invalid path")
    return p


def _legacy_meta_path(feature_id, channel):
    safe_fid, safe_ch = _safe_path(feature_id, channel)
    p = os.path.realpath(os.path.join(IMAGES_DIR, f"{safe_fid}__{safe_ch}.meta.json"))
    if not p.startswith(IMAGES_DIR):
        raise ValueError("Invalid path")
    return p


def mark_published(feature_id, channel, tweet_url=None):
    _validate_channel(channel)
    data = _load()
    k = _key(feature_id, channel)
    if k not in data:
        data[k] = {}
    data[k]["published"] = True
    if tweet_url:
        data[k]["tweet_url"] = tweet_url
    _save(data)
    logger.info(f"[publish_store] Marked {k} as published")


def _normalize_email_channel(ch):
    if ch in ("email_short", "email_medium", "email_long"):
        return "email_standalone"
    return ch


def is_published(feature_id, channel):
    data = _load()
    entry = data.get(_key(feature_id, channel), {})
    if not entry.get("published") and channel == "email_standalone":
        for fallback in ("email_medium", "email_short", "email_long"):
            entry = data.get(_key(feature_id, fallback), {})
            if entry.get("published"):
                break
    return entry.get("published", False)


def get_publish_info(feature_id, channel):
    data = _load()
    return data.get(_key(feature_id, channel), {})


def get_all_published():
    data = _load()
    result = {}
    for k, v in data.items():
        if not v.get("published"):
            continue
        parts = k.split(":", 1)
        if len(parts) != 2:
            continue
        fid, ch = parts
        ch = _normalize_email_channel(ch)
        if fid not in result:
            result[fid] = []
        if ch not in result[fid]:
            result[fid].append(ch)
    return result


MAX_IMAGE_SIZE = 10 * 1024 * 1024


def save_image(feature_id, channel, data_url, filename, file_size):
    _ensure_dirs()

    if len(data_url) > MAX_IMAGE_SIZE:
        logger.warning(f"[publish_store] Image too large for {feature_id}, skipping")
        return

    img_path = _image_path_feature(feature_id)
    meta_path = _meta_path_feature(feature_id)

    with open(img_path, "w") as f:
        f.write(data_url)

    meta = {"name": str(filename)[:200], "size": int(file_size) if file_size else 0}
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    logger.info(f"[publish_store] Saved feature-level image for {feature_id} ({filename}, {file_size} bytes)")


def _load_image_files(img_path, meta_path):
    if not os.path.exists(img_path) or not os.path.exists(meta_path):
        return None
    try:
        with open(img_path, "r") as f:
            data_url = f.read()
        with open(meta_path, "r") as f:
            meta = json.load(f)
        return {"dataUrl": data_url, "name": meta.get("name", "image.png"), "size": meta.get("size", 0)}
    except Exception as e:
        logger.error(f"[publish_store] Error loading image files: {e}")
        return None


def get_image(feature_id, channel=None):
    img_path = _image_path_feature(feature_id)
    meta_path = _meta_path_feature(feature_id)
    result = _load_image_files(img_path, meta_path)
    if result:
        return result

    if channel:
        try:
            _validate_channel(channel)
            legacy_img = _legacy_image_path(feature_id, channel)
            legacy_meta = _legacy_meta_path(feature_id, channel)
            result = _load_image_files(legacy_img, legacy_meta)
            if result:
                logger.info(f"[publish_store] Found legacy per-channel image for {feature_id}:{channel}")
                return result
        except ValueError:
            pass

    return None


def remove_image(feature_id, channel=None):
    img_path = _image_path_feature(feature_id)
    meta_path = _meta_path_feature(feature_id)
    for p in [img_path, meta_path]:
        if os.path.exists(p):
            os.remove(p)

    channels_to_clean = [channel] if channel else list(VALID_CHANNELS)
    for ch in channels_to_clean:
        try:
            _validate_channel(ch)
            for p in [_legacy_image_path(feature_id, ch), _legacy_meta_path(feature_id, ch)]:
                if os.path.exists(p):
                    os.remove(p)
        except ValueError:
            pass

    logger.info(f"[publish_store] Removed image for {feature_id}")


def get_feature_state(feature_id, channels):
    data = _load()
    shared_img = get_image(feature_id)
    result = {}
    for ch in channels:
        try:
            _validate_channel(ch)
        except ValueError:
            continue
        k = _key(feature_id, ch)
        entry = data.get(k, {})
        ch_state = {}
        if entry.get("published"):
            ch_state["published"] = True
            if entry.get("tweet_url"):
                ch_state["tweet_url"] = entry["tweet_url"]
        img = shared_img if shared_img else get_image(feature_id, ch)
        if img:
            ch_state["image"] = img
        if ch_state:
            result[ch] = ch_state
    return result


_VIDEO_ID_RE = re.compile(r'^[a-f0-9\-]{8,36}$')
_ALLOWED_VIDEO_MIME_PREFIXES = (b'\x00\x00\x00', b'\x1a\x45\xdf\xa3', b'RIFF')


def _video_dir(video_id):
    if not _VIDEO_ID_RE.match(video_id):
        raise ValueError("Invalid video_id format")
    safe_id = _SAFE_RE.sub("_", video_id)
    vdir = os.path.realpath(os.path.join(VIDEOS_DIR, safe_id))
    if not vdir.startswith(VIDEOS_DIR):
        raise ValueError("Invalid video path")
    return vdir


def save_video(feature_id, data_url, filename):
    _ensure_dirs()

    if "," not in data_url or not data_url.startswith("data:video/"):
        raise ValueError("Invalid video data URL format. Expected data:video/...;base64,...")

    header, b64data = data_url.split(",", 1)
    try:
        raw = base64.b64decode(b64data, validate=True)
    except Exception:
        raise ValueError("Invalid base64 data")

    if len(raw) > MAX_VIDEO_SIZE:
        raise ValueError("Video file too large (max 50MB)")

    if len(raw) < 8:
        raise ValueError("File too small to be a valid video")

    if not any(raw[:4].startswith(prefix) for prefix in _ALLOWED_VIDEO_MIME_PREFIXES):
        logger.warning(f"[publish_store] Video magic bytes check: header={raw[:8].hex()}")

    video_id = str(uuid.uuid4())
    vdir = _video_dir(video_id)
    os.makedirs(vdir, exist_ok=True)

    ext = os.path.splitext(filename)[1].lower() or ".mp4"
    if ext not in (".mp4", ".mov", ".webm", ".avi", ".mkv"):
        ext = ".mp4"
    video_path = os.path.join(vdir, "video" + ext)
    thumb_path = os.path.join(vdir, "thumb.jpg")

    with open(video_path, "wb") as f:
        f.write(raw)

    thumb_ok = False
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-ss", "00:00:00", "-vframes", "1",
             "-vf", "scale=640:-2", "-q:v", "3", thumb_path],
            capture_output=True, timeout=30
        )
        thumb_ok = result.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0
    except Exception as e:
        logger.warning(f"[publish_store] ffmpeg thumbnail failed for {video_id}: {e}")

    if thumb_ok:
        try:
            from integrations.video_thumb import composite_play_button_file
            composite_play_button_file(thumb_path)
        except Exception as e:
            logger.warning(f"[publish_store] play-button composite failed for {video_id}: {e}")

    meta = {
        "video_id": video_id,
        "feature_id": feature_id,
        "filename": str(filename)[:200],
        "ext": ext,
        "size": len(raw),
        "has_thumb": thumb_ok,
    }
    with open(os.path.join(vdir, "meta.json"), "w") as f:
        json.dump(meta, f)

    logger.info(f"[publish_store] Saved video {video_id} for feature {feature_id} ({filename}, {len(raw)} bytes, thumb={thumb_ok})")
    return video_id


def get_video_path(video_id):
    vdir = _video_dir(video_id)
    meta_path = os.path.join(vdir, "meta.json")
    if not os.path.exists(meta_path):
        return None, None
    with open(meta_path) as f:
        meta = json.load(f)
    ext = meta.get("ext", ".mp4")
    video_path = os.path.join(vdir, "video" + ext)
    if not os.path.exists(video_path):
        return None, None
    return video_path, meta


def get_video_thumb_path(video_id):
    vdir = _video_dir(video_id)
    thumb_path = os.path.join(vdir, "thumb.jpg")
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        return thumb_path
    return None


def list_feature_videos(feature_id):
    _ensure_dirs()
    results = []
    if not os.path.exists(VIDEOS_DIR):
        return results
    for entry in os.listdir(VIDEOS_DIR):
        meta_path = os.path.join(VIDEOS_DIR, entry, "meta.json")
        if not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if meta.get("feature_id") == feature_id:
                results.append(meta)
        except Exception:
            pass
    return results
