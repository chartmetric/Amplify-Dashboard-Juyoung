import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger("amplify.classification_overrides")

_OVERRIDES_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", ".override_history.json")
_history_lock = threading.Lock()

CHANNEL_RULES_BY_SCORE = {
    5: ["twitter", "email_newsletter", "email_standalone", "inapp", "linkedin", "notion_monthly", "article_hmc"],
    4: ["twitter", "email_newsletter", "email_standalone", "inapp", "notion_monthly"],
    3: ["twitter", "email_standalone", "inapp", "notion_monthly"],
    2: ["notion_monthly"],
    1: [],
}


def _load_history_from_disk() -> list:
    try:
        if os.path.exists(_OVERRIDES_HISTORY_FILE):
            with open(_OVERRIDES_HISTORY_FILE, "r") as f:
                data = json.load(f)
            logger.info(f"[override] Loaded {len(data)} override history entries from disk")
            return data
    except Exception as e:
        logger.warning(f"[override] Failed to load override history from disk: {e}")
    return []


def _save_history_to_disk():
    with _history_lock:
        try:
            tmp = _OVERRIDES_HISTORY_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(CLASSIFICATION_OVERRIDES, f, separators=(",", ":"))
            os.replace(tmp, _OVERRIDES_HISTORY_FILE)
        except Exception as e:
            logger.warning(f"[override] Failed to save override history to disk: {e}")


CLASSIFICATION_OVERRIDES = _load_history_from_disk()


def save_override(feature_id, feature_title, original_classification, override_classification, reason=""):
    recommended_channels = CHANNEL_RULES_BY_SCORE.get(
        override_classification.get("importance_score", 1), []
    )
    override_classification["recommended_channels"] = recommended_channels

    entry = {
        "feature_id": feature_id,
        "feature_title": feature_title,
        "original_classification": original_classification,
        "override_classification": override_classification,
        "reason": reason or "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    CLASSIFICATION_OVERRIDES.insert(0, entry)
    _save_history_to_disk()
    logger.info(f"[override] Saved override for '{feature_title}': {original_classification.get('category')}({original_classification.get('importance_score')}) -> {override_classification.get('category')}({override_classification.get('importance_score')})")
    return entry


def get_overrides(limit=None):
    if limit:
        return CLASSIFICATION_OVERRIDES[:limit]
    return list(CLASSIFICATION_OVERRIDES)


def get_override_learning_context(limit=3):
    if not CLASSIFICATION_OVERRIDES:
        return ""

    entries = CLASSIFICATION_OVERRIDES[:limit]
    lines = ["\nLEARNING FROM PAST CORRECTIONS (a human marketer corrected these AI classifications - learn from their judgment):"]
    for e in entries:
        orig = e["original_classification"]
        ovr = e["override_classification"]
        lines.append("---")
        lines.append(f"Feature: {e['feature_title']}")
        orig_cats = orig.get('categories', [orig.get('category', '?')])
        ovr_cats = ovr.get('categories', [ovr.get('category', '?')])
        lines.append(f"AI classified as: {', '.join(str(c) for c in orig_cats)}, importance {orig.get('importance_score', '?')}")
        lines.append(f"Marketer corrected to: {', '.join(str(c) for c in ovr_cats)}, importance {ovr.get('importance_score', '?')}")
        lines.append(f"Reason: {e.get('reason') or 'No reason given'}")
    lines.append("---")
    return "\n".join(lines)
