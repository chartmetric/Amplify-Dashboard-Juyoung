import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY_2", "") or os.environ.get("ANTHROPIC_API_KEY", "")
ASANA_ACCESS_TOKEN = os.environ.get("ASANA_ACCESS_TOKEN", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "amplify-dev-secret")
PORT = int(os.environ.get("PORT", 5000))

# Firebase / Google Sign-In credentials
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "")
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
FIREBASE_APP_ID = os.environ.get("FIREBASE_APP_ID", "")
# Auth domain follows the standard Firebase convention
FIREBASE_AUTH_DOMAIN = f"{FIREBASE_PROJECT_ID}.firebaseapp.com" if FIREBASE_PROJECT_ID else ""

# Legacy Google OAuth credentials (kept for backwards-compat references, unused)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "") or os.environ.get("SENDGRID_FROM_EMAIL", "")

# Google Workspace domain restriction
# When set (e.g. "chartmetric.com"), only accounts from that domain may sign in.
# When unset, all Google accounts are accepted (backward-compatible default).
GOOGLE_ALLOWED_DOMAIN = os.environ.get("GOOGLE_ALLOWED_DOMAIN", "")

# Chartmetric API proxy — Update Hub cookie-based service-account auth.
# When CM_API_BASE_URL + CM_SERVICE_ACCOUNT_EMAIL + CM_SERVICE_ACCOUNT_PASSWORD
# are all set, the client logs in once and caches the session cookie.
CM_API_BASE_URL = os.environ.get("CM_API_BASE_URL", "")
CM_SERVICE_ACCOUNT_EMAIL = os.environ.get("CM_SERVICE_ACCOUNT_EMAIL", "")
CM_SERVICE_ACCOUNT_PASSWORD = os.environ.get("CM_SERVICE_ACCOUNT_PASSWORD", "")

# In-app announcements admin (Task #143)
# Live mode is enabled automatically when EITHER the new cookie-based creds
# (CM_API_BASE_URL + CM_SERVICE_ACCOUNT_*) OR the legacy bearer-token pair
# (CHARTMETRIC_ADMIN_API_BASE_URL + CHARTMETRIC_ADMIN_API_TOKEN) are set.
# ANNOUNCEMENTS_STUB_MODE is a manual kill switch (truthy => force stub).
#
# CM_API_BASE_URL takes precedence over CHARTMETRIC_ADMIN_API_BASE_URL.
CHARTMETRIC_ADMIN_API_BASE_URL = CM_API_BASE_URL or os.environ.get("CHARTMETRIC_ADMIN_API_BASE_URL", "")
CHARTMETRIC_ADMIN_API_TOKEN = os.environ.get("CHARTMETRIC_ADMIN_API_TOKEN", "")
CHARTMETRIC_MEDIA_UPLOAD_URL = os.environ.get("CHARTMETRIC_MEDIA_UPLOAD_URL", "")
ANNOUNCEMENTS_STUB_MODE = os.environ.get("ANNOUNCEMENTS_STUB_MODE", "")

# Persistent session lifetime (Task #152)
# Override via SESSION_LIFETIME_DAYS env var; defaults to 7 days.
SESSION_LIFETIME_DAYS = int(os.environ.get("SESSION_LIFETIME_DAYS", "7"))
