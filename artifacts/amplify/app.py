import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify, render_template
from config import Config

app = Flask(__name__, template_folder="templates")
app.secret_key = Config.SESSION_SECRET


@app.route("/")
def dashboard():
    return render_template("index.html")


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
    app.run(host="0.0.0.0", port=Config.PORT, debug=False, use_reloader=False)
