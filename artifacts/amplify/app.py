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
from ai.classifier import classify_features_batch

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
    try:
        enriched = _get_enriched_features()
    except Exception as e:
        logger.error(f"Classified endpoint - enrichment error: {e}")
        return jsonify({"error": f"Feature enrichment failed: {e}"}), 500

    try:
        classified = classify_features_batch(enriched)
    except Exception as e:
        logger.error(f"Classified endpoint - classification error: {e}")
        return jsonify({"error": f"Classification failed: {e}"}), 500

    total = len(classified)
    min_importance = request.args.get("min_importance", type=int)
    if min_importance is not None:
        classified = [
            f for f in classified
            if f.get("classification", {}).get("importance_score", 0) >= min_importance
        ]

    return jsonify({
        "classified_features": classified,
        "total": total,
        "filtered": len(classified),
        "min_importance_applied": min_importance,
    })


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
    signal.signal(signal.SIGTERM, lambda *args: os._exit(0))
    port = config.PORT
    logger.info(f"Amplify starting on port {port}")
    sys.stdout.flush()
    from waitress import serve
    serve(app, host="0.0.0.0", port=port, _quiet=False)
