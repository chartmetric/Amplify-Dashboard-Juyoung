import logging
from datetime import datetime, timezone

logger = logging.getLogger("amplify.classification_overrides")

CLASSIFICATION_OVERRIDES = []

CHANNEL_RULES_BY_SCORE = {
    5: ["twitter", "email_newsletter", "email_standalone", "inapp", "linkedin", "notion_monthly", "article_hmc"],
    4: ["twitter", "email_newsletter", "email_standalone", "inapp", "notion_monthly"],
    3: ["twitter", "email_standalone", "inapp", "notion_monthly"],
    2: ["notion_monthly"],
    1: [],
}


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
        lines.append(f"AI classified as: {orig.get('category', '?')}, importance {orig.get('importance_score', '?')}")
        lines.append(f"Marketer corrected to: {ovr.get('category', '?')}, importance {ovr.get('importance_score', '?')}")
        lines.append(f"Reason: {e.get('reason') or 'No reason given'}")
    lines.append("---")
    return "\n".join(lines)
