"""Authentication helpers for Amplify.

Provides the login_required decorator used to protect all pages and API
endpoints.  Browser routes are redirected to /login; /api/* routes receive
a 401 JSON response instead.
"""
from functools import wraps

from flask import jsonify, redirect, request, session, url_for


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "Authentication required"}), 401
            session["next"] = request.url
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated
