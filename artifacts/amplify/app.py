import os
import sys
import signal
import logging
import html

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, jsonify, render_template, request
import config
from sources.asana_source import AsanaSource
from sources.slack_source import SlackSource
from sources.manual_source import ManualSource
from ai.classifier import classify_features_batch, classify_feature, set_manual_override, get_manual_overrides, remove_manual_override, apply_manual_overrides
from ai.generator import generate_for_channel, generate_all_channels
from ai.few_shot_examples import FEW_SHOT_EXAMPLES
from ai.feedback_store import save_feedback, get_feedback_history, get_all_feedback, clear_feedback
from datetime import datetime, timezone

app = Flask(__name__, template_folder="templates")
app.secret_key = config.SESSION_SECRET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("amplify")

SOURCE_REGISTRY = {
    "asana": AsanaSource(project_gid="1213445772342530"),
    "slack": SlackSource(channel_id="C014BMSCGS2"),
    "manual": ManualSource(),
}


@app.route("/")
def dashboard():
    """Dashboard home page.

    Category: System
    Response: HTML dashboard page.
    """
    return render_template("dashboard.html")


@app.route("/api/health")
def health():
    """Health check with API key status, channel count, and example counts.

    Category: System

    Response:
    {
        "status": "ok",
        "api_key_configured": true,
        "channels_loaded": 7,
        "examples_loaded": {"twitter": 3, ...},
        "keys": {"anthropic": true, "asana": true, "slack": true}
    }
    """
    from ai.channel_configs import CHANNEL_CONFIGS
    examples_loaded = {k: len(v) for k, v in FEW_SHOT_EXAMPLES.items()}
    return jsonify({
        "status": "ok",
        "api_key_configured": bool(config.ANTHROPIC_API_KEY),
        "channels_loaded": len(CHANNEL_CONFIGS),
        "examples_loaded": examples_loaded,
        "keys": {
            "anthropic": bool(config.ANTHROPIC_API_KEY),
            "asana": bool(config.ASANA_ACCESS_TOKEN),
            "slack": bool(config.SLACK_BOT_TOKEN),
        },
    })


@app.route("/api/sources")
def list_sources():
    """List available data sources.

    Category: Sources

    Response: ["asana", "slack", "manual"]
    """
    return jsonify(list(SOURCE_REGISTRY.keys()))


@app.route("/api/sources/asana/features")
def asana_list():
    """List all features from Asana projects.

    Category: Sources

    Response: Array of feature objects with id, title, description, date, section, custom fields.
    """
    source = SOURCE_REGISTRY["asana"]
    try:
        features = source.list_recent_features()
        return jsonify(features)
    except Exception as e:
        logger.error(f"Asana list error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/asana/features/<feature_id>")
def asana_detail(feature_id):
    """Get detailed context for a single Asana feature by task GID.

    Category: Sources

    Response: Feature context with title, description, comments, custom fields, permalink.
    """
    source = SOURCE_REGISTRY["asana"]
    try:
        ctx = source.get_feature_context(feature_id)
        return jsonify(ctx.to_dict())
    except Exception as e:
        logger.error(f"Asana detail error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/slack/features")
def slack_list():
    """List recent feature releases from Slack channel.

    Category: Sources

    Response: Array of released features with reactions and timestamps.
    """
    source = SOURCE_REGISTRY["slack"]
    try:
        features = source.list_recent_features()
        return jsonify(features)
    except Exception as e:
        logger.error(f"Slack list error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/slack/features/<feature_id>")
def slack_detail(feature_id):
    """Get detailed context for a Slack feature by message timestamp.

    Category: Sources

    Response: Feature context with title, description, reactions.
    """
    source = SOURCE_REGISTRY["slack"]
    try:
        ctx = source.get_feature_context(feature_id)
        return jsonify(ctx.to_dict())
    except Exception as e:
        logger.error(f"Slack detail error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/manual/feature", methods=["POST"])
def manual_create():
    """Create a manual feature entry for classification/generation.

    Category: Sources

    Request Body:
    {
        "title": "Feature name",
        "description": "Feature description"
    }

    Response: Feature context object.
    """
    data = request.get_json() or {}
    title = data.get("title", "")
    description = data.get("description", "")

    if not title:
        return jsonify({"error": "title is required"}), 400

    source = SOURCE_REGISTRY["manual"]
    ctx = source.get_feature_context(title=title, description=description)
    return jsonify(ctx.to_dict())


@app.route("/api/features/<source_type>")
def unified_list(source_type):
    """List features from a specific source (asana, slack, manual).

    Category: Sources

    Response: Array of feature objects from the specified source.
    """
    if source_type not in SOURCE_REGISTRY:
        return jsonify({"error": f"Unknown source: {source_type}"}), 404
    source = SOURCE_REGISTRY[source_type]
    try:
        features = source.list_recent_features()
        return jsonify(features)
    except Exception as e:
        logger.error(f"{source_type} list error: {e}")
        return jsonify({"error": str(e)}), 500


def _get_enriched_features():
    asana_source = SOURCE_REGISTRY["asana"]
    slack_source = SOURCE_REGISTRY["slack"]
    features = asana_source.list_recent_features()

    released_map = {}
    try:
        released_map = slack_source.get_released_task_ids()
    except Exception as e:
        logger.warning(f"Slack enrichment failed, continuing without: {e}")

    enriched = []
    for feature in features:
        task_id = feature.get("id", "")
        release_info = released_map.get(task_id, {})
        enriched.append({
            **feature,
            "release_status": release_info.get("released", False),
            "release_date": release_info.get("release_date"),
            "total_reactions": release_info.get("total_reactions"),
            "reactions_breakdown": release_info.get("reactions_breakdown"),
        })
    return enriched


@app.route("/api/features/enriched")
def enriched_features():
    """Fetch all features from Asana cross-referenced with Slack release data.

    Category: Sources

    Response: Array of enriched feature objects with release_status, release_date, reactions.
    """
    try:
        enriched = _get_enriched_features()
        return jsonify(enriched)
    except Exception as e:
        logger.error(f"Enriched endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/features/classify", methods=["POST"])
def classify_features_endpoint():
    """Classify a custom list of features using Claude AI.

    Category: Sources

    Request Body:
    {
        "features": [
            {"id": "...", "title": "...", "description": "..."}
        ]
    }

    Query Params: ?min_importance=N (optional filter)

    Response:
    {
        "classified_features": [
            {"title": "...", "classification": {"importance_score": 4, "category": "new_feature", ...}}
        ]
    }
    """
    data = request.get_json() or {}
    features = data.get("features", [])
    if not isinstance(features, list) or not features:
        return jsonify({"error": "No features provided. Send {\"features\": [...]} with a list of feature objects"}), 400
    if not all(isinstance(f, dict) for f in features):
        return jsonify({"error": "Each feature must be a JSON object with at least id, title, and description"}), 400

    try:
        classified = classify_features_batch(features)
    except Exception as e:
        logger.error(f"Classification error: {e}")
        return jsonify({"error": str(e)}), 500

    min_importance = request.args.get("min_importance", type=int)
    if min_importance is not None:
        classified = [
            f for f in classified
            if f.get("classification", {}).get("importance_score", 0) >= min_importance
        ]

    return jsonify({"classified_features": classified})


@app.route("/api/features/classified")
def classified_features():
    """Fetch and auto-classify all enriched features, sorted by importance.

    Category: Sources

    Query Params: ?limit=20&min_importance=N&released_only=true

    Response:
    {
        "classified_features": [...],
        "total_enriched": 356,
        "classified_count": 20,
        "filtered": 15
    }
    """
    limit = request.args.get("limit", default=20, type=int)
    released_only = request.args.get("released_only", default="false").lower() == "true"

    try:
        enriched = _get_enriched_features()
    except Exception as e:
        logger.error(f"Classified endpoint - enrichment error: {e}")
        return jsonify({"error": f"Feature enrichment failed: {e}"}), 500

    total_enriched = len(enriched)

    if released_only:
        enriched = [f for f in enriched if f.get("release_status")]

    enriched = enriched[:limit]

    try:
        classified = classify_features_batch(enriched)
    except Exception as e:
        logger.error(f"Classified endpoint - classification error: {e}")
        return jsonify({"error": f"Classification failed: {e}"}), 500

    classified = apply_manual_overrides(classified)

    total = len(classified)
    min_importance = request.args.get("min_importance", type=int)
    if min_importance is not None:
        classified = [
            f for f in classified
            if f.get("classification", {}).get("importance_score", 0) >= min_importance
        ]

    return jsonify({
        "classified_features": classified,
        "total_enriched": total_enriched,
        "classified_count": total,
        "filtered": len(classified),
        "limit_applied": limit,
        "released_only": released_only,
        "min_importance_applied": min_importance,
        "manual_overrides_applied": len(get_manual_overrides()),
    })


@app.route("/api/features/override", methods=["POST"])
def add_manual_override():
    """Set a manual override for a feature's classification.

    Category: Sources

    Request Body:
    {
        "feature_id": "123",
        "importance_score": 5,
        "category": "new_feature",
        "recommended_channels": ["twitter", "inapp"]
    }

    Response: {"status": "override_set", "feature_id": "123", "override": {...}}
    """
    data = request.get_json() or {}
    feature_id = data.get("feature_id")
    if not feature_id:
        return jsonify({"error": "feature_id is required"}), 400

    override = {}
    if "importance_score" in data:
        override["importance_score"] = int(data["importance_score"])
    if "importance_score_reason" in data:
        override["importance_score_reason"] = data["importance_score_reason"]
    if "category" in data:
        override["category"] = data["category"]
    if "recommended_channels" in data:
        override["recommended_channels"] = data["recommended_channels"]
    if "marketing_summary" in data:
        override["marketing_summary"] = data["marketing_summary"]
    if "target_audience" in data:
        override["target_audience"] = data["target_audience"]

    if not override:
        return jsonify({"error": "Provide at least one field to override (e.g. importance_score, category, recommended_channels)"}), 400

    set_manual_override(feature_id, override)
    return jsonify({"status": "override_set", "feature_id": feature_id, "override": override})


@app.route("/api/features/override/<feature_id>", methods=["DELETE"])
def delete_manual_override(feature_id):
    """Remove a manual override for a feature.

    Category: Sources

    Response: {"status": "override_removed", "feature_id": "123"}
    """
    removed = remove_manual_override(feature_id)
    if removed is None:
        return jsonify({"error": f"No override found for {feature_id}"}), 404
    return jsonify({"status": "override_removed", "feature_id": feature_id})


@app.route("/api/features/overrides")
def list_manual_overrides():
    """List all active manual classification overrides.

    Category: Sources

    Response: {"overrides": {"feature_id": {...}, ...}}
    """
    return jsonify({"overrides": get_manual_overrides()})


@app.route("/api/examples")
def list_all_examples():
    """View all few-shot examples for all channels.

    Category: Few-Shot Examples

    Response: {"twitter": [{...}], "email_newsletter": [{...}], ...}
    """
    return jsonify(FEW_SHOT_EXAMPLES)


@app.route("/api/examples/<channel_key>", methods=["GET"])
def get_channel_examples(channel_key):
    """View few-shot examples for a specific channel.

    Category: Few-Shot Examples

    Response: {"channel": "twitter", "examples": [{...}]}
    """
    examples = FEW_SHOT_EXAMPLES.get(channel_key)
    if examples is None:
        return jsonify({"error": f"No examples found for channel '{channel_key}'"}), 404
    return jsonify({"channel": channel_key, "examples": examples})


@app.route("/api/examples/<channel_key>", methods=["POST"])
def add_channel_example(channel_key):
    """Add a new few-shot example for a channel.

    Category: Few-Shot Examples

    Request Body:
    {
        "feature_context": "Description of the feature",
        "content": "The published marketing content"
    }

    Response: {"channel": "twitter", "examples": [{...}]}
    """
    data = request.get_json() or {}
    feature_context = data.get("feature_context")
    content = data.get("content")
    if not feature_context or not content:
        return jsonify({"error": "Both 'feature_context' and 'content' are required"}), 400

    if channel_key not in FEW_SHOT_EXAMPLES:
        FEW_SHOT_EXAMPLES[channel_key] = []

    FEW_SHOT_EXAMPLES[channel_key].append({
        "feature_context": feature_context,
        "content": content,
    })
    print(f"[examples] Added example for channel '{channel_key}' (now {len(FEW_SHOT_EXAMPLES[channel_key])} total)", flush=True)
    return jsonify({"channel": channel_key, "examples": FEW_SHOT_EXAMPLES[channel_key]})


@app.route("/api/examples/<channel_key>/<int:index>", methods=["DELETE"])
def delete_channel_example(channel_key, index):
    """Remove a few-shot example by index.

    Category: Few-Shot Examples

    Response: {"channel": "twitter", "removed": {...}, "examples": [{...}]}
    """
    examples = FEW_SHOT_EXAMPLES.get(channel_key)
    if examples is None:
        return jsonify({"error": f"No examples found for channel '{channel_key}'"}), 404
    if index < 0 or index >= len(examples):
        return jsonify({"error": f"Index {index} out of range (0-{len(examples)-1})"}), 400

    removed = examples.pop(index)
    print(f"[examples] Removed example {index} from channel '{channel_key}' (now {len(examples)} total)", flush=True)
    return jsonify({"channel": channel_key, "removed": removed, "examples": examples})


@app.route("/api/feedback", methods=["POST"])
def save_feedback_endpoint():
    """Save a feedback record (original vs approved draft) for learning.

    Category: Feedback Loop

    Request Body:
    {
        "channel": "twitter",
        "feature_title": "Artist Audience Overlap Tool",
        "original_draft": "the AI generated text...",
        "approved_draft": "the marketer's edited final version...",
        "feedback_note": "Made it shorter, removed the question format"
    }

    Response: {"success": true, "total_feedback_for_channel": 3, "record": {...}}
    """
    data = request.get_json() or {}
    channel = data.get("channel")
    feature_title = data.get("feature_title")
    original_draft = data.get("original_draft")
    approved_draft = data.get("approved_draft")
    feedback_note = data.get("feedback_note", "")

    if not channel or not feature_title or not original_draft or not approved_draft:
        return jsonify({"error": "channel, feature_title, original_draft, and approved_draft are all required"}), 400

    record = save_feedback(channel, feature_title, original_draft, approved_draft, feedback_note)
    total = len(get_feedback_history(channel, limit=999))
    print(f"[feedback] Saved feedback for '{feature_title}' on channel '{channel}' (total for channel: {total})", flush=True)
    return jsonify({"success": True, "total_feedback_for_channel": total, "record": record})


@app.route("/api/feedback", methods=["GET"])
def get_all_feedback_endpoint():
    """View all feedback history across all channels.

    Category: Feedback Loop

    Response: {"twitter": [{...}], "email_newsletter": [{...}], ...}
    """
    return jsonify(get_all_feedback())


@app.route("/api/feedback/<channel_key>", methods=["GET"])
def get_channel_feedback(channel_key):
    """View feedback history for a specific channel (most recent first).

    Category: Feedback Loop

    Query Params: ?limit=10

    Response: {"channel": "twitter", "feedback": [{...}], "total": 5}
    """
    limit = request.args.get("limit", default=10, type=int)
    records = get_feedback_history(channel_key, limit=limit)
    return jsonify({"channel": channel_key, "feedback": records, "total": len(records)})


@app.route("/api/approve", methods=["POST"])
def approve_and_save():
    """Approve a draft and save feedback for future learning.

    Category: Feedback Loop

    Request Body:
    {
        "feature": {"title": "..."},
        "channel": "twitter",
        "original_draft": "AI generated text...",
        "approved_draft": "final edited text...",
        "feedback_note": "optional note about changes"
    }

    Response: {"success": true, "message": "Approved and feedback saved for future learning"}
    """
    data = request.get_json() or {}
    channel = data.get("channel")
    original_draft = data.get("original_draft")
    approved_draft = data.get("approved_draft")
    feedback_note = data.get("feedback_note", "")

    feature = data.get("feature", {})
    feature_title = feature.get("title", "") if isinstance(feature, dict) else ""
    if not feature_title:
        feature_title = data.get("feature_title", "")

    if not channel or not original_draft or not approved_draft:
        return jsonify({"error": "channel, original_draft, and approved_draft are required"}), 400

    save_feedback(channel, feature_title, original_draft, approved_draft, feedback_note)
    print(f"[approve] Approved draft for '{feature_title}' on channel '{channel}'", flush=True)
    return jsonify({"success": True, "message": "Approved and feedback saved for future learning"})


@app.route("/api/generate", methods=["POST"])
def generate_content_endpoint():
    """Generate content for one feature across multiple channels.

    Category: Content Generation

    Request Body:
    {
        "feature": {"id": "...", "title": "...", "description": "..."},
        "channels": ["twitter", "email_newsletter"],
        "custom_instructions": ""
    }

    Response:
    {
        "feature_id": "...",
        "feature_title": "...",
        "generated_content": {"twitter": {"content": "...", "char_count": 142, ...}},
        "generated_at": "2026-03-31T12:00:00Z"
    }
    """
    data = request.get_json() or {}
    feature = data.get("feature")
    if not feature or not isinstance(feature, dict):
        return jsonify({"error": "feature is required and must be a feature object"}), 400

    channels = data.get("channels")
    custom_instructions = data.get("custom_instructions", "")

    if channels is not None and (not isinstance(channels, list) or not all(isinstance(c, str) for c in channels)):
        return jsonify({"error": "channels must be a list of strings"}), 400

    if not channels:
        classification = feature.get("classification", {})
        channels = classification.get("recommended_channels")

    try:
        print(f"[generate] Generating content for '{feature.get('title', 'unknown')}' on channels: {channels}", flush=True)
        results = generate_all_channels(feature, channels=channels, custom_instructions=custom_instructions or None)
        return jsonify({
            "feature_id": feature.get("id", ""),
            "feature_title": feature.get("title", ""),
            "generated_content": results,
            "classification": feature.get("classification"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Generate endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate/single", methods=["POST"])
def generate_single_endpoint():
    """Regenerate content for one channel, optionally with feedback on previous draft.

    Category: Content Generation

    Request Body:
    {
        "feature": {"id": "...", "title": "...", "description": "..."},
        "channel": "twitter",
        "custom_instructions": "",
        "feedback": "make it shorter, focus on the data angle"
    }

    Response: {"channel": "twitter", "content": "...", "char_count": 142, "success": true, ...}
    """
    data = request.get_json() or {}
    feature = data.get("feature")
    channel = data.get("channel")

    if not feature or not isinstance(feature, dict):
        return jsonify({"error": "feature is required and must be a feature object"}), 400
    if not channel or not isinstance(channel, str):
        return jsonify({"error": "channel is required (e.g. 'twitter')"}), 400

    from ai.channel_configs import CHANNEL_CONFIGS
    if channel not in CHANNEL_CONFIGS:
        return jsonify({"error": f"Unknown channel: '{channel}'. Valid channels: {list(CHANNEL_CONFIGS.keys())}"}), 400
    if not CHANNEL_CONFIGS[channel].get("enabled", False):
        return jsonify({"error": f"Channel '{channel}' is disabled"}), 400

    custom_instructions = data.get("custom_instructions", "")
    feedback = data.get("feedback", "")

    try:
        print(f"[generate/single] Regenerating '{feature.get('title', 'unknown')}' for channel '{channel}' (feedback: {bool(feedback)})", flush=True)
        result = generate_for_channel(
            feature, channel,
            custom_instructions=custom_instructions or None,
            feedback=feedback or None,
        )
        return jsonify(result)
    except Exception as e:
        logger.error(f"Generate single endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate/batch", methods=["POST"])
def generate_batch_endpoint():
    """Bulk generate content for multiple features. Auto-classifies if needed.

    Category: Content Generation

    Request Body:
    {
        "features": [{"id": "...", "title": "...", "description": "..."}, ...],
        "channels": ["twitter", "email_newsletter"],
        "min_importance": 3
    }

    Response:
    {
        "results": [{...}],
        "total_features": 10,
        "filtered_features": 5,
        "skipped_features": 5,
        "generated_at": "..."
    }
    """
    data = request.get_json() or {}
    features = data.get("features")
    channels = data.get("channels")
    min_importance = data.get("min_importance", 3)

    if not features or not isinstance(features, list):
        return jsonify({"error": "features is required and must be a list of feature objects"}), 400
    if not all(isinstance(f, dict) for f in features):
        return jsonify({"error": "Each feature must be a JSON object"}), 400
    if channels is not None and (not isinstance(channels, list) or not all(isinstance(c, str) for c in channels)):
        return jsonify({"error": "channels must be a list of strings"}), 400
    if not isinstance(min_importance, (int, float)):
        return jsonify({"error": "min_importance must be a number"}), 400
    min_importance = int(min_importance)

    try:
        print(f"[generate/batch] Processing {len(features)} features, min_importance={min_importance}", flush=True)

        needs_classification = [f for f in features if "classification" not in f]
        already_classified = [f for f in features if "classification" in f]

        if needs_classification:
            print(f"[generate/batch] Classifying {len(needs_classification)} unclassified features", flush=True)
            newly_classified = classify_features_batch(needs_classification)
            already_classified.extend(newly_classified)

        all_features = apply_manual_overrides(already_classified)
        total_features = len(all_features)

        filtered = [
            f for f in all_features
            if f.get("classification", {}).get("importance_score", 0) >= min_importance
        ]
        skipped = total_features - len(filtered)

        print(f"[generate/batch] {len(filtered)} features passed importance filter (skipped {skipped})", flush=True)

        results = []
        for f in filtered:
            feature_channels = channels
            if not feature_channels:
                feature_channels = f.get("classification", {}).get("recommended_channels")

            content = generate_all_channels(f, channels=feature_channels)
            results.append({
                "feature_id": f.get("id", ""),
                "feature_title": f.get("title", ""),
                "generated_content": content,
                "classification": f.get("classification"),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            })

        return jsonify({
            "results": results,
            "total_features": total_features,
            "filtered_features": len(filtered),
            "skipped_features": skipped,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Generate batch endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/channels")
@app.route("/api/test/channels")
def test_channels():
    """List all channel configurations.

    Category: Channels

    Response:
    {
        "channels": [
            {"key": "twitter", "display_name": "X / Twitter", "description": "...", "max_chars": 600, "enabled": true}
        ],
        "total": 7
    }
    """
    from ai.channel_configs import CHANNEL_CONFIGS
    channels = [
        {
            "key": k,
            "display_name": v["display_name"],
            "description": v.get("description", ""),
            "max_chars": v.get("max_chars"),
            "enabled": v.get("enabled", False),
        }
        for k, v in CHANNEL_CONFIGS.items()
    ]
    return jsonify({"channels": channels, "total": len(channels)})


@app.route("/api/debug/slack-links")
def debug_slack_links():
    """Debug endpoint showing raw Slack message links.

    Category: System

    Response: Array of messages with URLs extracted.
    """
    slack_source = SOURCE_REGISTRY["slack"]
    try:
        from sources.slack_source import _extract_links, _clean_slack_text
        client = slack_source._get_client()
        result = client.conversations_history(
            channel=slack_source.channel_id,
            limit=50,
        )
        messages = []
        for msg in result.get("messages", []):
            raw_text = msg.get("text", "")
            urls = _extract_links(raw_text)
            messages.append({
                "message_ts": msg.get("ts", ""),
                "message_preview": _clean_slack_text(raw_text)[:100],
                "all_urls": urls,
            })
        return jsonify(messages)
    except Exception as e:
        logger.error(f"Debug slack-links error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/features/<source_type>/<feature_id>")
def unified_detail(source_type, feature_id):
    """Get detailed context for a specific feature from any source.

    Category: Sources

    Response: Feature context object with title, description, metadata.
    """
    if source_type not in SOURCE_REGISTRY:
        return jsonify({"error": f"Unknown source: {source_type}"}), 404
    source = SOURCE_REGISTRY[source_type]
    try:
        ctx = source.get_feature_context(feature_id)
        return jsonify(ctx.to_dict())
    except Exception as e:
        logger.error(f"{source_type} detail error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/classify")
def test_classify():
    """Classify a hardcoded sample feature (Audience Overlap Tool).

    Category: Testing

    Response: {"sample_feature": {...}, "classification": {...}}
    """
    sample = {
        "id": "test-classify-001",
        "title": "New Artist Audience Overlap Tool",
        "description": "We've built a new tool that lets artists and managers compare their audience demographics with other artists. This helps identify collaboration opportunities, understand fan crossover, and plan tour routing based on shared audience geography. Available in the Artist Profile section under the new Audience Insights tab.",
        "release_status": True,
        "release_date": "2026-03-28",
        "reactions_breakdown": {"rocket": 5, "fire": 3, "heart": 2},
        "total_reactions": 10,
        "urgency_score": None,
    }
    classification = classify_feature(sample)
    return jsonify({"sample_feature": sample, "classification": classification})


@app.route("/api/test/generate")
def test_generate_full():
    """Full pipeline test: classify a sample feature then generate content for all channels.

    Category: Testing

    Response: {"feature": {...}, "classification": {...}, "generated_content": {"twitter": {...}, ...}}
    """
    sample = {
        "id": "test-001",
        "title": "New Artist Audience Overlap Tool",
        "description": "We've built a new tool that lets artists and managers compare their audience demographics with other artists. This helps identify collaboration opportunities, understand fan crossover, and plan tour routing based on shared audience geography. Available in the Artist Profile section under the new 'Audience Insights' tab. The tool shows percentage overlap across Spotify listeners, Instagram followers, and YouTube subscribers, with geographic heatmaps for the top 20 shared cities.",
        "release_status": True,
        "release_date": "2026-03-28",
        "reactions_breakdown": [
            {"name": "rocket", "count": 5},
            {"name": "fire", "count": 3},
            {"name": "heart", "count": 2},
        ],
        "total_reactions": 10,
        "urgency_score": None,
    }
    try:
        print("[test/generate] Classifying sample feature...", flush=True)
        classification = classify_feature(sample)
        sample["classification"] = classification

        print(f"[test/generate] Classification: score={classification.get('importance_score')}, channels={classification.get('recommended_channels')}", flush=True)
        print("[test/generate] Generating content for all enabled channels...", flush=True)
        content = generate_all_channels(sample)

        return jsonify({
            "feature": sample,
            "classification": classification,
            "generated_content": content,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Test generate error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/generate-twitter")
def test_generate_twitter():
    """Generate a sample tweet for the Audience Overlap Tool feature.

    Category: Testing

    Response: {"feature": {...}, "twitter_content": {"content": "...", "char_count": 142, ...}}
    """
    sample = {
        "id": "test-gen-001",
        "title": "New Artist Audience Overlap Tool",
        "description": "We've built a new tool that lets artists and managers compare their audience demographics with other artists. This helps identify collaboration opportunities, understand fan crossover, and plan tour routing based on shared audience geography. Available in the Artist Profile section under the new Audience Insights tab.",
        "release_status": True,
        "release_date": "2026-03-28",
        "reactions_breakdown": [
            {"name": "rocket", "count": 5},
            {"name": "fire", "count": 3},
            {"name": "heart", "count": 2},
        ],
        "total_reactions": 10,
        "urgency_score": None,
    }
    try:
        result = generate_for_channel(sample, "twitter")
        return jsonify({"feature": sample, "twitter_content": result})
    except Exception as e:
        logger.error(f"Test generate-twitter error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/generate-samples")
def test_generate_samples():
    """Generate content for 'Playlists to Pitch' across all enabled channels.

    Category: Testing

    Response: {"feature": {...}, "generated_content": {"twitter": {...}, "email_newsletter": {...}, ...}}
    """
    sample = {
        "id": "test-samples-001",
        "title": "Playlists to Pitch: Personalized Playlist Recommendations",
        "description": "A new feature on Track Pages that recommends playlists tailored to your specific track. Each recommendation includes a Fit Analysis explaining why the playlist is a strong match, key metrics like Added Reach and Added Streams, and direct links to reach out to playlist curators.",
        "release_status": True,
        "release_date": "2026-03-30",
        "reactions_breakdown": [
            {"name": "rocket", "count": 8},
            {"name": "fire", "count": 5},
            {"name": "heart", "count": 4},
        ],
        "total_reactions": 17,
        "urgency_score": None,
    }
    try:
        results = generate_all_channels(sample)
        return jsonify({"feature": sample, "generated_content": results})
    except Exception as e:
        logger.error(f"Test generate-samples error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/raw-fields")
def test_raw_fields():
    """Show all raw fields from Asana enriched with Slack data.

    Category: Testing

    Response: {"features": [{...}], "total": 356}
    """
    try:
        enriched = _get_enriched_features()
        return jsonify({"features": enriched, "total": len(enriched)})
    except Exception as e:
        logger.error(f"Test raw-fields error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/feedback-loop")
def test_feedback_loop():
    """Demo the feedback loop: generate, save feedback, generate again with learning.

    Category: Testing

    Response: {"first_generation": {...}, "feedback_saved": {...}, "second_generation_after_learning": {...}}
    """
    feature_1 = {
        "id": "test-gen-001",
        "title": "New Artist Audience Overlap Tool",
        "description": "We've built a new tool that lets artists and managers compare their audience demographics with other artists. This helps identify collaboration opportunities, understand fan crossover, and plan tour routing based on shared audience geography. Available in the Artist Profile section under the new Audience Insights tab.",
        "release_status": True,
        "release_date": "2026-03-28",
        "reactions_breakdown": [
            {"name": "rocket", "count": 5},
            {"name": "fire", "count": 3},
            {"name": "heart", "count": 2},
        ],
        "total_reactions": 10,
        "urgency_score": None,
    }
    feature_2 = {
        "id": "test-002",
        "title": "Playlist Placement Tracker",
        "description": "Track when and where your songs get added to editorial and algorithmic playlists across Spotify, Apple Music, and Deezer. See historical placement data and get alerts for new additions.",
        "release_status": True,
        "release_date": "2026-03-30",
    }

    try:
        first_draft = generate_for_channel(feature_1, "twitter")

        approved_text = "Compare your fanbase with any artist. Our new Audience Overlap tool shows listener crossover across Spotify, Instagram, and YouTube. #Chartmetric #AudienceData"
        feedback_record = save_feedback(
            channel="twitter",
            feature_title=feature_1["title"],
            original_draft=first_draft["content"],
            approved_draft=approved_text,
            feedback_note="Shorter, more direct, no questions, just state what it does clearly",
        )

        second_draft = generate_for_channel(feature_2, "twitter")

        return jsonify({
            "first_generation": first_draft,
            "feedback_saved": feedback_record,
            "second_generation_after_learning": second_draft,
        })
    except Exception as e:
        logger.error(f"Test feedback-loop error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/claude")
def test_claude():
    """Test Claude API connection with a simple prompt.

    Category: Testing

    Response: {"success": true, "content": "Hello! ...", "error": null}
    """
    from ai.claude_client import generate_content
    result = generate_content("You are a helpful assistant.", "Say hello in one sentence.", max_tokens=64)
    return jsonify(result)


@app.route("/api/docs")
def api_docs():
    """Auto-generated API documentation page.

    Category: System

    Response: HTML page listing all endpoints grouped by category.
    """
    categories = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.rule.startswith("/static"):
            continue
        endpoint_func = app.view_functions.get(rule.endpoint)
        if not endpoint_func:
            continue

        docstring = endpoint_func.__doc__ or ""
        methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
        if not methods:
            continue

        category = "Other"
        description_lines = []
        body_lines = []
        in_body = False

        for line in docstring.split("\n"):
            stripped = line.strip()
            if stripped.startswith("Category:"):
                category = stripped.replace("Category:", "").strip()
                in_body = False
            elif stripped.startswith("Request Body:") or stripped.startswith("Response:") or stripped.startswith("Query Params:"):
                in_body = True
                body_lines.append(stripped)
            elif in_body:
                body_lines.append(line.rstrip())
            elif stripped and not in_body:
                description_lines.append(stripped)

        description = " ".join(description_lines).strip()
        body_block = "\n".join(body_lines).strip()

        if category not in categories:
            categories[category] = []

        categories[category].append({
            "methods": methods,
            "url": rule.rule,
            "description": description,
            "body_block": body_block,
        })

    category_order = ["System", "Sources", "Channels", "Content Generation", "Few-Shot Examples", "Feedback Loop", "Testing", "Other"]
    sorted_categories = []
    for cat in category_order:
        if cat in categories:
            sorted_categories.append((cat, categories[cat]))
    for cat in sorted(categories.keys()):
        if cat not in category_order:
            sorted_categories.append((cat, categories[cat]))

    html_parts = ["""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amplify API Documentation</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #e1e4e8; line-height: 1.6; padding: 2rem; max-width: 960px; margin: 0 auto; }
h1 { font-size: 2rem; color: #fff; margin-bottom: 0.25rem; }
.subtitle { color: #8b949e; font-size: 1.1rem; margin-bottom: 2rem; }
h2 { font-size: 1.3rem; color: #58a6ff; margin: 2rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid #21262d; }
.endpoint { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 0.75rem; }
.endpoint-header { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
.method { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.75rem; font-weight: 700; padding: 0.2rem 0.5rem; border-radius: 4px; }
.method-GET { background: #1f6feb33; color: #58a6ff; }
.method-POST { background: #2ea04333; color: #3fb950; }
.method-DELETE { background: #f8514933; color: #f85149; }
.url { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.9rem; color: #f0f3f6; }
.url a { color: #f0f3f6; text-decoration: none; }
.url a:hover { text-decoration: underline; color: #58a6ff; }
.desc { color: #8b949e; font-size: 0.9rem; margin-top: 0.5rem; }
.body-block { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 0.75rem 1rem; margin-top: 0.75rem; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.8rem; color: #c9d1d9; white-space: pre-wrap; overflow-x: auto; }
.count { color: #484f58; font-size: 0.85rem; margin-left: 0.5rem; }
</style>
</head>
<body>
<h1>Amplify API Documentation</h1>
<p class="subtitle">Product Marketing Autopilot for Chartmetric</p>
"""]

    for cat_name, endpoints in sorted_categories:
        html_parts.append(f'<h2>{html.escape(cat_name)} <span class="count">({len(endpoints)})</span></h2>')
        for ep in endpoints:
            method_badges = " ".join(
                f'<span class="method method-{m}">{m}</span>' for m in ep["methods"]
            )
            url_escaped = html.escape(ep["url"])
            is_get = ep["methods"] == ["GET"]
            url_display = f'<a href="{url_escaped}">{url_escaped}</a>' if is_get else url_escaped

            html_parts.append(f'<div class="endpoint">')
            html_parts.append(f'  <div class="endpoint-header">{method_badges} <span class="url">{url_display}</span></div>')
            if ep["description"]:
                html_parts.append(f'  <div class="desc">{html.escape(ep["description"])}</div>')
            if ep["body_block"]:
                html_parts.append(f'  <div class="body-block">{html.escape(ep["body_block"])}</div>')
            html_parts.append(f'</div>')

    html_parts.append("</body></html>")
    return "\n".join(html_parts)


if __name__ == "__main__":
    import threading
    import time

    _shutdown = False

    def handle_sigterm(*args):
        global _shutdown
        _shutdown = True
        logger.info("Received SIGTERM, shutting down gracefully")
        sys.stdout.flush()
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    def keep_alive():
        while not _shutdown:
            logger.info("Amplify heartbeat \u2014 server alive")
            sys.stdout.flush()
            time.sleep(30)

    heartbeat = threading.Thread(target=keep_alive, daemon=True)
    heartbeat.start()

    port = config.PORT
    logger.info(f"Amplify starting on port {port}")
    print(f"Amplify starting on port {port}", flush=True)

    print("\n=== Registered Routes ===", flush=True)
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        print(f"  {methods:8s} {rule.rule}", flush=True)
    print("=========================\n", flush=True)
    sys.stdout.flush()
    from waitress import serve
    serve(
        app,
        host="0.0.0.0",
        port=port,
        _quiet=False,
        channel_timeout=300,
        recv_bytes=65536,
        threads=8,
    )
