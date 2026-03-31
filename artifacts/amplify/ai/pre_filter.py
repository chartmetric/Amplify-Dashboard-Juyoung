import re
import logging

logger = logging.getLogger("amplify.pre_filter")

SCORE_1_KEYWORDS = [
    "fix", "bug", "hotfix", "typo", "lint", "refactor",
    "ci/cd", "pipeline", "dependency", "upgrade package", "revert",
]

SCORE_2_KEYWORDS = [
    "be only", "backend only", "internal only", "devops",
    "cleanup", "rename", "minor", "tweak",
]

SCORE_4_KEYWORDS = [
    "new", "launch", "release", "redesign", "overhaul",
]

SCORE_5_KEYWORDS = [
    "new feature", "new tool", "major", "v2",
]


def _text_contains(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    for kw in keywords:
        if kw in text_lower:
            return True
    return False


def pre_filter_feature(feature_data: dict) -> dict:
    title = feature_data.get("title", "")
    description = feature_data.get("description", "")
    combined = f"{title} {description}"
    total_reactions = feature_data.get("total_reactions") or 0
    feature_id = feature_data.get("id", "")

    if _text_contains(combined, SCORE_1_KEYWORDS):
        return {
            "feature_id": feature_id,
            "title": title,
            "pre_filter_score": 1,
            "pre_filter_reason": "Matched low-value keywords (bug fix, refactor, etc.)",
            "skip_classification": True,
        }

    has_high_keywords = _text_contains(combined, SCORE_4_KEYWORDS + SCORE_5_KEYWORDS)

    if _text_contains(combined, SCORE_2_KEYWORDS):
        return {
            "feature_id": feature_id,
            "title": title,
            "pre_filter_score": 2,
            "pre_filter_reason": "Matched internal/minor keywords (backend only, cleanup, etc.)",
            "skip_classification": True,
        }

    if total_reactions == 0 and not has_high_keywords:
        if not _text_contains(combined, SCORE_4_KEYWORDS) and not _text_contains(combined, SCORE_5_KEYWORDS):
            pass

    if _text_contains(combined, SCORE_5_KEYWORDS) or total_reactions >= 10:
        reason_parts = []
        if _text_contains(combined, SCORE_5_KEYWORDS):
            reason_parts.append("matched high-value keywords (new feature, major, etc.)")
        if total_reactions >= 10:
            reason_parts.append(f"high team reactions ({total_reactions})")
        return {
            "feature_id": feature_id,
            "title": title,
            "pre_filter_score": 5,
            "pre_filter_reason": "High value: " + " and ".join(reason_parts),
            "skip_classification": False,
        }

    if _text_contains(combined, SCORE_4_KEYWORDS) or total_reactions >= 5:
        reason_parts = []
        if _text_contains(combined, SCORE_4_KEYWORDS):
            reason_parts.append("matched notable keywords (new, launch, redesign, etc.)")
        if total_reactions >= 5:
            reason_parts.append(f"notable team reactions ({total_reactions})")
        return {
            "feature_id": feature_id,
            "title": title,
            "pre_filter_score": 4,
            "pre_filter_reason": "Notable: " + " and ".join(reason_parts),
            "skip_classification": False,
        }

    return {
        "feature_id": feature_id,
        "title": title,
        "pre_filter_score": 3,
        "pre_filter_reason": "Default score, no strong keyword signals",
        "skip_classification": False,
    }


def pre_filter_batch(features: list[dict]) -> dict:
    to_classify = []
    skipped = []

    for feature in features:
        result = pre_filter_feature(feature)

        if result["skip_classification"]:
            feature_with_classification = {
                **feature,
                "classification": {
                    "importance_score": result["pre_filter_score"],
                    "importance_score_reason": result["pre_filter_reason"],
                    "category": "bug_fix" if result["pre_filter_score"] == 1 else "infrastructure",
                    "recommended_channels": [],
                    "marketing_summary": "",
                    "target_audience": "",
                    "pre_filtered": True,
                },
            }
            skipped.append(feature_with_classification)
            logger.info(f"  [pre-filter] SKIP score={result['pre_filter_score']}: {result['title'][:60]}")
        else:
            to_classify.append(feature)
            logger.info(f"  [pre-filter] CLASSIFY score={result['pre_filter_score']}: {result['title'][:60]}")

    logger.info(f"[pre-filter] {len(to_classify)} to classify, {len(skipped)} skipped")
    return {
        "to_classify": to_classify,
        "skipped": skipped,
    }
