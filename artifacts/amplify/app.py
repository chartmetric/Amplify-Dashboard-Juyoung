import os
import sys
import signal
import logging

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify, render_template
from config import Config

app = Flask(__name__, template_folder="templates")
app.secret_key = Config.SESSION_SECRET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("amplify")


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
    signal.signal(signal.SIGTERM, lambda *args: os._exit(0))
    port = Config.PORT
    logger.info(f"Amplify starting on port {port}")
    sys.stdout.flush()
    from waitress import serve
    serve(app, host="0.0.0.0", port=port, _quiet=False)
