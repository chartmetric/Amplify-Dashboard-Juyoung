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


MAX_IMAGE_SIZE = 25 * 1024 * 1024


def save_image(feature_id, channel, data_url, filename, file_size, is_gif=False):
    _ensure_dirs()

    if len(data_url) > MAX_IMAGE_SIZE:
        logger.warning(f"[publish_store] Image too large for {feature_id} ({len(data_url)} bytes data-url), rejecting")
        raise ValueError(f"Image data exceeds maximum size ({MAX_IMAGE_SIZE // (1024*1024)}MB)")

    img_path = _image_path_feature(feature_id)
    meta_path = _meta_path_feature(feature_id)
    tmp_img = img_path + ".tmp"
    tmp_meta = meta_path + ".tmp"

    with open(tmp_img, "w") as f:
        f.write(data_url)
    os.replace(tmp_img, img_path)

    meta = {"name": str(filename)[:200], "size": int(file_size) if file_size else 0, "is_gif": bool(is_gif)}
    with open(tmp_meta, "w") as f:
        json.dump(meta, f)
    os.replace(tmp_meta, meta_path)

    logger.info(f"[publish_store] Saved feature-level image for {feature_id} ({filename}, dataUrl={len(data_url)} bytes, declared={file_size}, is_gif={bool(is_gif)})")
    return True


def _load_image_files(img_path, meta_path):
    if not os.path.exists(img_path) or not os.path.exists(meta_path):
        return None
    try:
        with open(img_path, "r") as f:
            data_url = f.read()
        with open(meta_path, "r") as f:
            meta = json.load(f)
        return {
            "dataUrl": data_url,
            "name": meta.get("name", "image.png"),
            "size": meta.get("size", 0),
            "is_gif": bool(meta.get("is_gif", False)),
        }
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


def _probe_video_streams(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,codec_name:format=format_name",
             "-of", "json", path],
            capture_output=True, timeout=30, text=True,
        )
        if result.returncode != 0:
            return None, None, ""
        info = json.loads(result.stdout or "{}")
        streams = info.get("streams", []) or []
        vcodec = next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None)
        acodec = next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None)
        fmt = ((info.get("format") or {}).get("format_name") or "")
        return vcodec, acodec, fmt
    except Exception as e:
        logger.warning(f"[publish_store] ffprobe failed for {path}: {e}")
        return None, None, ""


def _normalize_video_to_mp4(src_path, dst_path):
    """Produce a Gmail-friendly MP4 (H.264/AAC, +faststart) at dst_path.

    If the source is already H.264 + AAC in MP4, stream-copies (remux). Otherwise
    transcodes. Returns True on success, False on any failure.
    """
    vcodec, acodec, fmt = _probe_video_streams(src_path)
    fmt_parts = {p.strip() for p in (fmt or "").split(",") if p.strip()}
    is_mp4_container = bool(fmt_parts & {"mp4", "m4a", "3gp", "3g2", "mj2"})
    can_remux = (vcodec == "h264") and (acodec in (None, "aac")) and is_mp4_container

    if can_remux:
        cmd = [
            "ffmpeg", "-y", "-i", src_path,
            "-c", "copy",
            "-movflags", "+faststart",
            "-f", "mp4",
            dst_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", src_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            "-f", "mp4",
            dst_path,
        ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
            logger.info(
                f"[publish_store] Normalized video to MP4 ({'remux' if can_remux else 'transcode'}): "
                f"{os.path.getsize(dst_path)} bytes"
            )
            return True
        err_tail = (result.stderr or b"")[-500:].decode("utf-8", errors="replace")
        logger.warning(
            f"[publish_store] ffmpeg normalize failed (rc={result.returncode}, remux={can_remux}): {err_tail}"
        )
        if os.path.exists(dst_path):
            try:
                os.remove(dst_path)
            except Exception:
                pass
        return False
    except Exception as e:
        logger.warning(f"[publish_store] ffmpeg normalize exception: {e}")
        if os.path.exists(dst_path):
            try:
                os.remove(dst_path)
            except Exception:
                pass
        return False


def save_video_url(feature_id, url, filename, thumb_url=None):
    """Register an external (URL-only) video against feature_id.

    Persists a meta.json describing the remote video so it survives reload and
    is included in list_feature_videos / _build_video_map. No bytes are stored
    on disk; the email pipeline keeps using the external URL/thumbnail.
    """
    _ensure_dirs()
    if not url or not isinstance(url, str):
        raise ValueError("URL is required")
    url = url.strip()
    if not re.match(r'^https?://', url, re.IGNORECASE):
        raise ValueError("URL must start with http:// or https://")
    if len(url) > 2048:
        raise ValueError("URL too long")
    thumb_url = (thumb_url or "").strip()
    if thumb_url and not re.match(r'^https?://', thumb_url, re.IGNORECASE):
        thumb_url = ""
    if thumb_url and len(thumb_url) > 2048:
        thumb_url = ""

    video_id = str(uuid.uuid4())
    vdir = _video_dir(video_id)
    os.makedirs(vdir, exist_ok=True)
    meta = {
        "video_id": video_id,
        "feature_id": feature_id,
        "filename": str(filename or url)[:200],
        "ext": "",
        "size": 0,
        "has_thumb": bool(thumb_url),
        "is_url": True,
        "external_url": url,
        "external_thumb_url": thumb_url,
    }
    with open(os.path.join(vdir, "meta.json"), "w") as f:
        json.dump(meta, f)
    logger.info(f"[publish_store] Saved video URL {video_id} for feature {feature_id} ({url})")
    return video_id


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

    src_ext = os.path.splitext(filename)[1].lower() or ".mp4"
    if src_ext not in (".mp4", ".mov", ".webm", ".avi", ".mkv"):
        src_ext = ".mp4"
    src_path = os.path.join(vdir, "source" + src_ext)
    thumb_path = os.path.join(vdir, "thumb.jpg")

    with open(src_path, "wb") as f:
        f.write(raw)

    normalized_path = os.path.join(vdir, "video.mp4")
    if _normalize_video_to_mp4(src_path, normalized_path):
        ext = ".mp4"
        video_path = normalized_path
        try:
            if os.path.abspath(src_path) != os.path.abspath(video_path):
                os.remove(src_path)
        except Exception:
            pass
    else:
        ext = src_ext
        video_path = os.path.join(vdir, "video" + ext)
        # os.replace is atomic on the same filesystem; vdir was just created so
        # src and dest share it. If this somehow fails, propagate so meta and
        # get_video_path stay consistent rather than silently pointing at a
        # nonexistent file.
        os.replace(src_path, video_path)
        if os.path.exists(normalized_path):
            try:
                os.remove(normalized_path)
            except Exception:
                pass

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

    try:
        stored_size = os.path.getsize(video_path)
    except Exception:
        stored_size = len(raw)
    meta = {
        "video_id": video_id,
        "feature_id": feature_id,
        "filename": str(filename)[:200],
        "ext": ext,
        "size": stored_size,
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


def delete_video(feature_id, video_id):
    """Permanently delete a video (file, thumbnail, metadata) belonging to feature_id.

    Raises ValueError if the video is missing or does not belong to the given feature.
    Returns True on success.
    """
    import shutil
    vdir = _video_dir(video_id)
    meta_path = os.path.join(vdir, "meta.json")
    if not os.path.exists(meta_path):
        raise ValueError("Video not found")
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except Exception as e:
        raise ValueError(f"Could not read video metadata: {e}")
    owner = meta.get("feature_id")
    if owner != feature_id:
        raise ValueError("Video does not belong to this feature")
    shutil.rmtree(vdir)
    logger.info(f"[publish_store] Deleted video {video_id} for feature {feature_id}")
    return True


def cleanup_orphan_videos(known_feature_ids, dry_run=False):
    """One-shot maintenance routine that prunes stray videos under VIDEOS_DIR.

    Removes:
    - Video directories with missing/unreadable meta.json (irrecoverable).
    - Videos whose owning feature_id is not in `known_feature_ids`.
    - Exact duplicates per feature (same filename + size); keeps the newest
      directory by mtime and removes the older copies.

    Safe to re-run: if there is nothing to clean up it is a no-op. When
    `dry_run=True`, no files are touched and the report still describes what
    would have been removed.

    `known_feature_ids` should be an iterable of all currently-known feature
    ids. If it is None, the orphan-by-feature check is skipped (only the
    duplicate / unreadable cleanup runs).

    Returns a report dict:
        {
          "scanned": N,
          "removed_orphan": [{"video_id", "feature_id", "filename"}, ...],
          "removed_duplicate": [{"video_id", "feature_id", "filename", "size", "kept": kept_id}, ...],
          "removed_unreadable": [{"video_id", "reason"}, ...],
          "dry_run": bool,
        }
    """
    import shutil

    _ensure_dirs()
    report = {
        "scanned": 0,
        "removed_orphan": [],
        "removed_duplicate": [],
        "removed_unreadable": [],
        "dry_run": bool(dry_run),
    }

    if not os.path.isdir(VIDEOS_DIR):
        return report

    known = set(known_feature_ids) if known_feature_ids is not None else None

    def _rm(vdir):
        if dry_run:
            return
        try:
            shutil.rmtree(vdir)
        except Exception as e:
            logger.warning(f"[publish_store] cleanup_orphan_videos: failed to remove {vdir}: {e}")

    entries = []
    for entry in os.listdir(VIDEOS_DIR):
        vdir = os.path.join(VIDEOS_DIR, entry)
        if not os.path.isdir(vdir):
            continue
        report["scanned"] += 1
        meta_path = os.path.join(vdir, "meta.json")
        if not os.path.exists(meta_path):
            report["removed_unreadable"].append({"video_id": entry, "reason": "missing meta.json"})
            logger.info(f"[publish_store] cleanup: removing {entry} (missing meta.json){' [dry-run]' if dry_run else ''}")
            _rm(vdir)
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception as e:
            report["removed_unreadable"].append({"video_id": entry, "reason": f"unreadable meta: {e}"})
            logger.info(f"[publish_store] cleanup: removing {entry} (unreadable meta: {e}){' [dry-run]' if dry_run else ''}")
            _rm(vdir)
            continue

        try:
            mtime = os.path.getmtime(vdir)
        except Exception:
            mtime = 0.0
        entries.append((entry, vdir, meta, mtime))

    surviving = []
    for entry, vdir, meta, mtime in entries:
        fid = meta.get("feature_id")
        if known is not None and fid not in known:
            report["removed_orphan"].append({
                "video_id": entry,
                "feature_id": fid,
                "filename": meta.get("filename", ""),
            })
            logger.info(f"[publish_store] cleanup: removing orphan {entry} (feature_id={fid!r} not found){' [dry-run]' if dry_run else ''}")
            _rm(vdir)
            continue
        surviving.append((entry, vdir, meta, mtime))

    by_key = {}
    for entry, vdir, meta, mtime in surviving:
        fid = meta.get("feature_id")
        key = (fid, meta.get("filename", ""), int(meta.get("size") or 0))
        by_key.setdefault(key, []).append((entry, vdir, meta, mtime))

    for key, items in by_key.items():
        if len(items) <= 1:
            continue
        items.sort(key=lambda x: x[3], reverse=True)
        keeper = items[0]
        for entry, vdir, meta, mtime in items[1:]:
            report["removed_duplicate"].append({
                "video_id": entry,
                "feature_id": meta.get("feature_id"),
                "filename": meta.get("filename", ""),
                "size": meta.get("size", 0),
                "kept": keeper[0],
            })
            logger.info(
                f"[publish_store] cleanup: removing duplicate {entry} "
                f"(feature_id={meta.get('feature_id')!r} filename={meta.get('filename','')!r} "
                f"size={meta.get('size')}) — keeping {keeper[0]}{' [dry-run]' if dry_run else ''}"
            )
            _rm(vdir)

    logger.info(
        f"[publish_store] cleanup_orphan_videos done: scanned={report['scanned']} "
        f"orphan={len(report['removed_orphan'])} duplicate={len(report['removed_duplicate'])} "
        f"unreadable={len(report['removed_unreadable'])} dry_run={dry_run}"
    )
    return report


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
