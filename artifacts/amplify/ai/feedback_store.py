import logging
from datetime import datetime, timezone

logger = logging.getLogger("amplify.feedback_store")

FEEDBACK_HISTORY = {}


def _load_from_db():
    try:
        from ai.db import load_feedback, is_available
        if not is_available():
            return
        data = load_feedback()
        FEEDBACK_HISTORY.clear()
        FEEDBACK_HISTORY.update(data)
        if data:
            total = sum(len(v) for v in data.values())
            logger.info(f"[feedback_store] Loaded {total} feedback records from database")
    except Exception as e:
        logger.error(f"[feedback_store] Failed to load from db: {e}")


def save_feedback(channel, feature_title, original_draft, approved_draft, feedback_note=None):
    if channel not in FEEDBACK_HISTORY:
        FEEDBACK_HISTORY[channel] = []

    record = {
        "feature_title": feature_title,
        "original_draft": original_draft,
        "approved_draft": approved_draft,
        "channel": channel,
        "feedback_note": feedback_note or "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    FEEDBACK_HISTORY[channel].append(record)
    try:
        from ai.db import save_feedback_record
        save_feedback_record(channel, record)
    except Exception as e:
        logger.error(f"[feedback_store] Failed to persist feedback record for channel {channel}: {e}")
    return record


def get_feedback_history(channel, limit=5):
    records = FEEDBACK_HISTORY.get(channel, [])
    return list(reversed(records[-limit:]))


def get_all_feedback():
    return FEEDBACK_HISTORY


def clear_feedback(channel=None):
    if channel:
        FEEDBACK_HISTORY.pop(channel, None)
    else:
        FEEDBACK_HISTORY.clear()
    try:
        from ai.db import delete_feedback
        delete_feedback(channel)
    except Exception as e:
        logger.error(f"[feedback_store] Failed to clear feedback from db: {e}")


_load_from_db()
