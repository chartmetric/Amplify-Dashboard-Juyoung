from datetime import datetime, timezone

FEEDBACK_HISTORY = {}


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
