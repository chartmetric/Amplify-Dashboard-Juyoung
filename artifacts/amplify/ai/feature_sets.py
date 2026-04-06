import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger("amplify.feature_sets")

_SETS_FILE = os.path.join(os.path.dirname(__file__), "..", ".feature_sets.json")
_lock = threading.Lock()


def _load_from_disk() -> list:
    try:
        if os.path.exists(_SETS_FILE):
            with open(_SETS_FILE, "r") as f:
                data = json.load(f)
            logger.info(f"[feature_sets] Loaded {len(data)} saved sets from disk")
            return data
    except Exception as e:
        logger.warning(f"[feature_sets] Failed to load from disk: {e}")
    return []


def _save_to_disk():
    with _lock:
        try:
            tmp = _SETS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(_SAVED_SETS, f, separators=(",", ":"))
            os.replace(tmp, _SETS_FILE)
        except Exception as e:
            logger.warning(f"[feature_sets] Failed to save to disk: {e}")


_SAVED_SETS = _load_from_disk()


def save_set(name, channel, feature_ids):
    set_entry = {
        "id": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"),
        "name": name,
        "channel": channel,
        "feature_ids": feature_ids,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    for existing in _SAVED_SETS:
        if existing["name"] == name and existing["channel"] == channel:
            existing["feature_ids"] = feature_ids
            existing["created_at"] = set_entry["created_at"]
            _save_to_disk()
            return existing
    _SAVED_SETS.insert(0, set_entry)
    _save_to_disk()
    return set_entry


def get_sets():
    return list(_SAVED_SETS)


def delete_set(set_id):
    for i, s in enumerate(_SAVED_SETS):
        if s["id"] == set_id:
            removed = _SAVED_SETS.pop(i)
            _save_to_disk()
            return removed
    return None
