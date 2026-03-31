import json
import logging

from ai.claude_client import generate_content

logger = logging.getLogger("amplify.classifier")

CLASSIFICATION_SYSTEM_PROMPT = """You are a product marketing classifier for Chartmetric, a music data analytics platform used by artists, managers, labels, publishers, and playlist curators.

Given a feature or update from our development team, classify it for marketing purposes.

CLASSIFICATION RULES:
- "new_feature": Entirely new capability that didn't exist before (e.g., new page, new tool, new data source)
- "improvement": Enhancement to an existing feature (better performance, new options, UX changes, data accuracy fixes)
- "bug_fix": Fixing something that was broken or behaving incorrectly
- "infrastructure": Backend/DevOps changes with no direct user impact (DB migrations, refactors, CI/CD, internal tooling)
- "mobile": Mobile app specific changes
- "deprecation": Removing or replacing a feature

IMPORTANCE SCORING (1-5):
5 = Major new feature or significant improvement that many users across multiple personas will notice and benefit from. Always worth full marketing push. Examples: new analytics tool, new data source integration, major UI overhaul.
4 = Notable improvement or new feature for a specific audience segment. Worth marketing to that segment. Examples: new chart type for labels, improved playlist analytics.
3 = Moderate improvement. Worth mentioning in newsletter and internal updates. Examples: UX polish, performance improvement users will notice, minor new option.
2 = Minor tweak, small bug fix, or internal improvement with marginal user impact. Internal channels only. Examples: tooltip fix, minor data correction.
1 = Pure infrastructure, internal tooling, refactoring, or trivial fix. Internal documentation only. Examples: CI/CD changes, code refactor, dependency update.

RULES FOR is_user_facing:
- If the change affects what users see, interact with, or get value from -> true
- If it's purely backend, DevOps, internal tooling, refactoring -> false
- If a backend change improves performance/accuracy users will notice -> true
- If a backend change fixes data calculation users rely on -> true

RULES FOR recommended_channels:
- importance_score 5: all external channels (twitter, email_newsletter, linkedin, inapp, article_hmc) + notion_monthly
- importance_score 4: 2-3 most relevant external channels + notion_monthly
- importance_score 3: 1-2 channels (email_newsletter likely) + notion_monthly
- importance_score 2: notion_monthly only
- importance_score 1: notion_monthly only
- Always include notion_monthly (it's the internal record of everything)
- For user-facing features: always include inapp if score >= 4
- For data/analytics features: always include article_hmc if score >= 4

RULES FOR target_audience -- pick the most relevant subset of:
["artists", "managers", "labels", "publishers", "curators", "all"]
- If the feature benefits everyone broadly, use ["all"]
- If it's specific (e.g., playlist analytics), pick the relevant personas

Respond with ONLY a valid JSON object. No markdown code blocks, no backticks, no explanation text."""

CLASSIFICATION_USER_PROMPT = """Classify this feature/update:

Feature ID: {feature_id}
Title: {title}
Description: {description}
Release Status: {release_status}
Urgency Score: {urgency_score}

Return a JSON object with these fields:
- feature_id (string)
- title (string)
- category (string: new_feature|improvement|bug_fix|infrastructure|mobile|deprecation)
- importance_score (integer 1-5)
- importance_score_reason (string: 1-2 sentence explanation of why you assigned that importance score)
- is_user_facing (boolean)
- target_audience (array of strings)
- marketing_summary (string: 1-sentence plain-English summary of user impact)
- recommended_channels (array of strings)
- skip_reason (string or null: if importance_score <= 1, explain why this should be skipped for marketing)"""


def classify_feature(feature_data: dict) -> dict:
    feature_id = feature_data.get("id", "")
    title = feature_data.get("title", "")
    description = feature_data.get("description", "")
    release_status = "Released" if feature_data.get("release_status") else "In Progress"
    urgency_score = feature_data.get("urgency_score", "N/A")

    user_prompt = CLASSIFICATION_USER_PROMPT.format(
        feature_id=feature_id,
        title=title,
        description=description,
        release_status=release_status,
        urgency_score=urgency_score,
    )

    result = generate_content(CLASSIFICATION_SYSTEM_PROMPT, user_prompt, max_tokens=512)

    if not result["success"]:
        return {
            "feature_id": feature_id,
            "title": title,
            "category": "unknown",
            "importance_score": 0,
            "importance_score_reason": "",
            "is_user_facing": False,
            "target_audience": [],
            "marketing_summary": "",
            "recommended_channels": [],
            "skip_reason": f"Classification failed: {result.get('error', 'unknown error')}",
        }

    try:
        classification = json.loads(result["content"])
        classification["feature_id"] = feature_id
        classification["title"] = title
        return classification
    except json.JSONDecodeError:
        logger.error(f"Failed to parse classification JSON for {feature_id}: {result['content'][:200]}")
        return {
            "feature_id": feature_id,
            "title": title,
            "category": "unknown",
            "importance_score": 0,
            "importance_score_reason": "",
            "is_user_facing": False,
            "target_audience": [],
            "marketing_summary": "",
            "recommended_channels": [],
            "skip_reason": "Classification returned invalid JSON",
        }


def classify_features_batch(features: list[dict]) -> list[dict]:
    classified = []
    for feature in features:
        classification = classify_feature(feature)
        classified.append({
            **feature,
            "classification": classification,
        })

    classified.sort(key=lambda f: f["classification"].get("importance_score", 0), reverse=True)
    return classified
