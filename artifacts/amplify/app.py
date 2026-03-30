import os
from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, render_template

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SESSION_SECRET", "amplify-dev-key")


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "app": "Amplify"})


@app.route("/status")
def status():
    return jsonify({
        "status": "running",
        "version": "1.0.0",
        "services": {
            "ai": "ready",
            "sources": "ready",
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
