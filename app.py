from flask import Flask, jsonify, request, render_template
import config

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "keys_loaded": {
        "anthropic": bool(config.ANTHROPIC_API_KEY),
        "asana": bool(config.ASANA_ACCESS_TOKEN),
        "slack": bool(config.SLACK_BOT_TOKEN),
    }})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

from sources.asana_source import AsanaSource

# Replace with your actual project GID
asana_source = AsanaSource(project_gid="1213445772342530")

@app.route("/api/sources/asana/features")
def list_asana_features():
    features = asana_source.list_recent_features()
    return jsonify(features)

@app.route("/api/sources/asana/features/<feature_id>")
def get_asana_feature(feature_id):
    ctx = asana_source.get_feature_context(feature_id)
    return jsonify({
        "title": ctx.title,
        "description": ctx.description,
        "raw_details": ctx.raw_details,
        "source_type": ctx.source_type,
        "metadata": ctx.metadata,
    })