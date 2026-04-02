import json
import os
import re
import tempfile
import logging

logger = logging.getLogger(__name__)

PUBLISH_FILE = os.path.join(os.path.dirname(__file__), "..", ".publish_state.json")
IMAGES_DIR = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".publish_images"))

VALID_CHANNELS = {"twitter", "email_newsletter", "email_standalone", "inapp", "linkedin", "notion_monthly", "article_hmc"}
_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-.]")


def _ensure_dirs():
    os.makedirs(IMAGES_DIR, exist_ok=True)


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


def _image_path(feature_id, channel):
    safe_fid, safe_ch = _safe_path(feature_id, channel)
    p = os.path.realpath(os.path.join(IMAGES_DIR, f"{safe_fid}__{safe_ch}.img"))
    if not p.startswith(IMAGES_DIR):
        raise ValueError("Invalid path")
    return p


def _meta_path(feature_id, channel):
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


def is_published(feature_id, channel):
    data = _load()
    entry = data.get(_key(feature_id, channel), {})
    return entry.get("published", False)


def get_publish_info(feature_id, channel):
    data = _load()
    return data.get(_key(feature_id, channel), {})


MAX_IMAGE_SIZE = 10 * 1024 * 1024


def save_image(feature_id, channel, data_url, filename, file_size):
    _validate_channel(channel)
    _ensure_dirs()

    if len(data_url) > MAX_IMAGE_SIZE:
        logger.warning(f"[publish_store] Image too large for {feature_id}:{channel}, skipping")
        return

    img_path = _image_path(feature_id, channel)
    meta_path = _meta_path(feature_id, channel)

    with open(img_path, "w") as f:
        f.write(data_url)

    meta = {"name": str(filename)[:200], "size": int(file_size) if file_size else 0}
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    logger.info(f"[publish_store] Saved image for {feature_id}:{channel} ({filename}, {file_size} bytes)")


def get_image(feature_id, channel):
    try:
        _validate_channel(channel)
    except ValueError:
        return None

    img_path = _image_path(feature_id, channel)
    meta_path = _meta_path(feature_id, channel)

    if not os.path.exists(img_path) or not os.path.exists(meta_path):
        return None

    try:
        with open(img_path, "r") as f:
            data_url = f.read()
        with open(meta_path, "r") as f:
            meta = json.load(f)
        return {"dataUrl": data_url, "name": meta.get("name", "image.png"), "size": meta.get("size", 0)}
    except Exception as e:
        logger.error(f"[publish_store] Error loading image for {feature_id}:{channel}: {e}")
        return None


def remove_image(feature_id, channel):
    _validate_channel(channel)
    img_path = _image_path(feature_id, channel)
    meta_path = _meta_path(feature_id, channel)
    for p in [img_path, meta_path]:
        if os.path.exists(p):
            os.remove(p)
    logger.info(f"[publish_store] Removed image for {feature_id}:{channel}")


def get_feature_state(feature_id, channels):
    data = _load()
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
        img = get_image(feature_id, ch)
        if img:
            ch_state["image"] = img
        if ch_state:
            result[ch] = ch_state
    return result
