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
5 = Major new feature or significant improvement that many users across multiple personas will notice and benefit from. Always worth full marketing push. Examples: new analytics tool, new data source integration, major UI overhaul. ONLY for categories "new_feature" or "improvement".
4 = Notable improvement or new feature for a specific audience segment. Worth marketing to that segment. Examples: new chart type for labels, improved playlist analytics. ONLY for categories "new_feature" or "improvement".
3 = Moderate improvement. Worth mentioning in newsletter and internal updates. Examples: UX polish, performance improvement users will notice, minor new option.
2 = Minor tweak, small bug fix, or internal improvement with marginal user impact. Internal channels only. Examples: tooltip fix, minor data correction.
1 = Pure infrastructure, internal tooling, refactoring, or trivial fix. Internal documentation only. Examples: CI/CD changes, code refactor, dependency update.

CATEGORY-IMPORTANCE CAPS:
- "new_feature" and "improvement" can score 1-5 (no cap)
- "bug_fix" can score at most 3 (even major bug fixes are not marketing-worthy beyond newsletter mentions)
- "infrastructure" can score at most 2 (never externally marketed)
- "deprecation" can score at most 3 (users need to know, but it's not a positive marketing moment)
- "mobile" can score 1-5 (same as new_feature/improvement, depending on impact)

RULES FOR is_user_facing:
- If the change affects what users see, interact with, or get value from -> true
- If it's purely backend, DevOps, internal tooling, refactoring -> false
- If a backend change improves performance/accuracy users will notice -> true
- If a backend change fixes data calculation users rely on -> true

CHANNEL USE CASES (understand these before recommending):
- "twitter": High frequency. Any time an important user-facing feature releases. Good for quick announcements with data hooks. Use for score >= 3 if user-facing.
- "email_newsletter": Low frequency (monthly). Holistic product update with 3-4 key features. Only the most important features make the cut. Use for score >= 4 only.
- "email_standalone": Weekly/biweekly/monthly opt-in "what's new" digest. More inclusive than newsletter. Use for score >= 3 if user-facing.
- "inapp": High frequency. Any time an important user-facing feature releases. Users see this inside the product. Use for score >= 3 if user-facing.
- "linkedin": Thought-leadership posts connecting features to industry trends. Use for score >= 4 when there's a compelling industry narrative.
- "notion_monthly": Low frequency (monthly). Internal doc listing 10-12 key features of the month. Use for score >= 3.
- "article_hmc": Low frequency. Long-form blog articles combining multiple features around a theme (marketing, content, playlist, influencer). Rarely for a single feature unless score = 5. Use for score >= 5, or flag with note "combine with related features" for score 4.

RULES FOR recommended_channels:
- importance_score 5: twitter, email_newsletter, email_standalone, inapp, linkedin, notion_monthly, article_hmc (all channels)
- importance_score 4: twitter, email_newsletter, email_standalone, inapp, notion_monthly (+ linkedin if industry-relevant, + article_hmc only if thematic)
- importance_score 3 (user-facing): twitter, email_standalone, inapp, notion_monthly
- importance_score 3 (not user-facing): notion_monthly only
- importance_score 2: notion_monthly only
- importance_score 1: (none — skip marketing entirely)
- Always include notion_monthly for score >= 2
- twitter and inapp go together — if a feature is worth tweeting, it's worth an in-app announcement
- email_newsletter is reserved for the best features (score >= 4) — it's a curated monthly digest, not a catch-all
- article_hmc is almost never for a single feature — it's for thematic bundles. Only include for score 5 standalone features

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


def classify_features_batch(features: list[dict], max_workers: int = 8) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    classified = [None] * len(features)

    def classify_at_index(idx, feature):
        classification = classify_feature(feature)
        return idx, {**feature, "classification": classification}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(classify_at_index, i, f): i
            for i, f in enumerate(features)
        }
        for future in as_completed(futures):
            idx, result = future.result()
            classified[idx] = result

    classified.sort(key=lambda f: f["classification"].get("importance_score", 0), reverse=True)
    return classified


_manual_overrides = {}


def set_manual_override(feature_id: str, override: dict):
    _manual_overrides[feature_id] = override


def get_manual_overrides():
    return dict(_manual_overrides)


def remove_manual_override(feature_id: str):
    return _manual_overrides.pop(feature_id, None)


def apply_manual_overrides(classified_features: list[dict]) -> list[dict]:
    for feature in classified_features:
        fid = feature.get("id", "")
        if fid in _manual_overrides:
            override = _manual_overrides[fid]
            if "classification" not in feature:
                feature["classification"] = {}
            feature["classification"].update(override)
            feature["classification"]["manual_override"] = True
    classified_features.sort(
        key=lambda f: f.get("classification", {}).get("importance_score", 0),
        reverse=True,
    )
    return classified_features
