import os
import sys
import signal
import logging

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, jsonify, render_template, request
import config
from sources.asana_source import AsanaSource
from sources.slack_source import SlackSource
from sources.manual_source import ManualSource
from ai.classifier import classify_features_batch, classify_feature, set_manual_override, get_manual_overrides, remove_manual_override, apply_manual_overrides
from ai.generator import generate_for_channel, generate_all_channels
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
    return render_template("dashboard.html")


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "keys": {
            "anthropic": bool(config.ANTHROPIC_API_KEY),
            "asana": bool(config.ASANA_ACCESS_TOKEN),
            "slack": bool(config.SLACK_BOT_TOKEN),
        },
    })


@app.route("/api/sources")
def list_sources():
    return jsonify(list(SOURCE_REGISTRY.keys()))


@app.route("/api/sources/asana/features")
def asana_list():
    source = SOURCE_REGISTRY["asana"]
    try:
        features = source.list_recent_features()
        return jsonify(features)
    except Exception as e:
        logger.error(f"Asana list error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/asana/features/<feature_id>")
def asana_detail(feature_id):
    source = SOURCE_REGISTRY["asana"]
    try:
        ctx = source.get_feature_context(feature_id)
        return jsonify(ctx.to_dict())
    except Exception as e:
        logger.error(f"Asana detail error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/slack/features")
def slack_list():
    source = SOURCE_REGISTRY["slack"]
    try:
        features = source.list_recent_features()
        return jsonify(features)
    except Exception as e:
        logger.error(f"Slack list error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/slack/features/<feature_id>")
def slack_detail(feature_id):
    source = SOURCE_REGISTRY["slack"]
    try:
        ctx = source.get_feature_context(feature_id)
        return jsonify(ctx.to_dict())
    except Exception as e:
        logger.error(f"Slack detail error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/manual/feature", methods=["POST"])
def manual_create():
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
    try:
        enriched = _get_enriched_features()
        return jsonify(enriched)
    except Exception as e:
        logger.error(f"Enriched endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/features/classify", methods=["POST"])
def classify_features_endpoint():
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
    removed = remove_manual_override(feature_id)
    if removed is None:
        return jsonify({"error": f"No override found for {feature_id}"}), 404
    return jsonify({"status": "override_removed", "feature_id": feature_id})


@app.route("/api/features/overrides")
def list_manual_overrides():
    return jsonify({"overrides": get_manual_overrides()})


@app.route("/api/generate", methods=["POST"])
def generate_content_endpoint():
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


@app.route("/api/debug/slack-links")
def debug_slack_links():
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
    from ai.classifier import classify_feature
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


@app.route("/api/test/channels")
def test_channels():
    from ai.channel_configs import CHANNEL_CONFIGS
    channels = [{"key": k, "display_name": v["display_name"]} for k, v in CHANNEL_CONFIGS.items()]
    return jsonify({"channels": channels, "total": len(channels)})


@app.route("/api/test/claude")
def test_claude():
    from ai.claude_client import generate_content
    result = generate_content("You are a helpful assistant.", "Say hello in one sentence.", max_tokens=64)
    return jsonify(result)


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
            logger.info("Amplify heartbeat — server alive")
            sys.stdout.flush()
            time.sleep(30)

    heartbeat = threading.Thread(target=keep_alive, daemon=True)
    heartbeat.start()

    port = config.PORT
    logger.info(f"Amplify starting on port {port}")
    print(f"Amplify starting on port {port}", flush=True)
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
