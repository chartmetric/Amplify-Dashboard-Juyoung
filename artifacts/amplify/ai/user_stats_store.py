"""User activity statistics storage.

Persists per-user stats in a JSON file:  <app_dir>/.user_stats.json

Schema:
{
  "user@example.com": {
    "minutes_in_app": 42,
    "drafts_saved": 5,
    "sends": [
      {"channel": "email", "feature_count": 3, "ts": "2024-01-01T00:00:00"},
      ...
    ]
  }
}
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger("amplify.user_stats")

_STATS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".user_stats.json")
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(_STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("user_stats_store: load error: %s", e)
        return {}


def _save(data: dict) -> None:
    try:
        with open(_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("user_stats_store: save error: %s", e)


def _ensure_user(data: dict, user_key: str) -> dict:
    if user_key not in data:
        data[user_key] = {"minutes_in_app": 0, "drafts_saved": 0, "sends": []}
    return data[user_key]


def record_heartbeat(user_email: str, minutes: int = 1) -> None:
    """Add `minutes` of active time for the user."""
    if not user_email:
        return
    with _lock:
        data = _load()
        entry = _ensure_user(data, user_email)
        entry["minutes_in_app"] = entry.get("minutes_in_app", 0) + minutes
        _save(data)


def record_draft_saved(user_email: str) -> None:
    """Increment the draft-saved counter for the user."""
    if not user_email:
        return
    with _lock:
        data = _load()
        entry = _ensure_user(data, user_email)
        entry["drafts_saved"] = entry.get("drafts_saved", 0) + 1
        _save(data)


def record_artifact_sent(user_email: str, channel: str, feature_count: int = 1) -> None:
    """Record a successful artifact send/publish for the user."""
    if not user_email:
        return
    with _lock:
        data = _load()
        entry = _ensure_user(data, user_email)
        sends = entry.setdefault("sends", [])
        sends.append({
            "channel": channel,
            "feature_count": max(1, int(feature_count)),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        _save(data)


def get_stats(user_email: str) -> dict:
    """Return aggregated stats for the user.

    Returns:
        {
          "minutes_in_app": int,
          "drafts_saved": int,
          "artifacts_sent": int,
          "time_saved_minutes": int,
        }
    """
    if not user_email:
        return {"minutes_in_app": 0, "drafts_saved": 0, "artifacts_sent": 0, "time_saved_minutes": 0}

    with _lock:
        data = _load()

    entry = data.get(user_email, {})
    sends = entry.get("sends", [])
    time_saved = sum(s.get("feature_count", 1) * 5 for s in sends)

    return {
        "minutes_in_app": entry.get("minutes_in_app", 0),
        "drafts_saved": entry.get("drafts_saved", 0),
        "artifacts_sent": len(sends),
        "time_saved_minutes": time_saved,
    }
