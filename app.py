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