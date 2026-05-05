"""Google OAuth 2.0 authentication blueprint for Amplify.

Routes
------
GET  /login           Render the login page (no auth required)
GET  /auth/google     Start the Google OAuth redirect
GET  /auth/callback   Handle the Google callback; set session; redirect to next
GET  /logout          Clear session and redirect to /login
"""
import logging

from flask import Blueprint, redirect, render_template, request, session, url_for
from authlib.integrations.flask_client import OAuth

import config

logger = logging.getLogger("amplify.auth")

bp = Blueprint("auth", __name__)
oauth = OAuth()


def init_oauth(app):
    """Initialise Authlib and register the google provider."""
    oauth.init_app(app)
    oauth.register(
        name="google",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


@bp.route("/login")
def login():
    error = request.args.get("error")
    google_configured = bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET)
    return render_template("login.html", error=error, google_configured=google_configured)


@bp.route("/auth/google")
def google_login():
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route("/auth/callback")
def google_callback():
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo") or {}
        session["user"] = {
            "name": userinfo.get("name", ""),
            "email": userinfo.get("email", ""),
            "picture": userinfo.get("picture", ""),
        }
        logger.info("[auth] login ok email=%s", session["user"].get("email"))
        next_url = session.pop("next", None) or url_for("dashboard")
        return redirect(next_url)
    except Exception as exc:
        logger.warning("[auth] callback error: %s", exc)
        return redirect(url_for("auth.login", error="Sign-in failed. Please try again."))


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
