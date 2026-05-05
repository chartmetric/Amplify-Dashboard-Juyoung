"""Firebase Authentication blueprint for Amplify.

Routes
------
GET  /login            Render the login page (no auth required)
POST /auth/firebase    Verify Firebase ID token; set session; return redirect URL
GET  /logout           Clear session and redirect to /login
"""
import logging

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
import google.oauth2.id_token
import google.auth.transport.requests

import config

logger = logging.getLogger("amplify.auth")

bp = Blueprint("auth", __name__)

# Reusable transport – reuses the underlying urllib3 connection pool
_google_request = google.auth.transport.requests.Request()


def init_oauth(app):
    """No-op – kept so app.py import stays unchanged."""
    pass


@bp.route("/login")
def login():
    error = request.args.get("error")
    firebase_configured = bool(config.FIREBASE_API_KEY and config.FIREBASE_PROJECT_ID)
    return render_template(
        "login.html",
        error=error,
        firebase_configured=firebase_configured,
        firebase_api_key=config.FIREBASE_API_KEY,
        firebase_project_id=config.FIREBASE_PROJECT_ID,
        firebase_app_id=config.FIREBASE_APP_ID,
        firebase_auth_domain=config.FIREBASE_AUTH_DOMAIN,
        google_allowed_domain=config.GOOGLE_ALLOWED_DOMAIN,
    )


@bp.route("/auth/firebase", methods=["POST"])
def firebase_verify():
    """Receive a Firebase ID token from the client and exchange it for a session."""
    data = request.get_json(silent=True) or {}
    id_token = data.get("idToken", "")
    if not id_token:
        return jsonify({"success": False, "error": "Missing token"}), 400

    try:
        decoded = google.oauth2.id_token.verify_firebase_token(
            id_token, _google_request, config.FIREBASE_PROJECT_ID
        )
    except Exception as exc:
        logger.warning("[auth] Firebase token verification failed: %s", exc)
        return jsonify({"success": False, "error": "Sign-in failed. Please try again."}), 401

    session.permanent = True

    if config.GOOGLE_ALLOWED_DOMAIN:
        hd = (decoded.get("hd") or "").strip().lower()
        allowed = config.GOOGLE_ALLOWED_DOMAIN.strip().lower()
        if hd != allowed:
            logger.warning(
                "[auth] domain mismatch: hd=%r email=%s allowed=%s",
                hd or "(none)", decoded.get("email", ""), config.GOOGLE_ALLOWED_DOMAIN,
            )
            return jsonify({
                "success": False,
                "error": f"Only @{config.GOOGLE_ALLOWED_DOMAIN} accounts are allowed.",
            }), 403


    session["user"] = {
        "name": decoded.get("name", ""),
        "email": decoded.get("email", ""),
        "picture": decoded.get("picture", ""),
    }
    logger.info("[auth] login ok email=%s", session["user"].get("email"))
    next_path = session.pop("next", None) or "/"
    # Ensure we only ever redirect to a relative path — never an internal hostname
    if next_path.startswith("http"):
        from urllib.parse import urlparse as _up
        next_path = _up(next_path).path or "/"
    return jsonify({"success": True, "redirect": next_path})


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
