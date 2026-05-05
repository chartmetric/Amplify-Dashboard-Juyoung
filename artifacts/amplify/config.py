import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY_2", "") or os.environ.get("ANTHROPIC_API_KEY", "")
ASANA_ACCESS_TOKEN = os.environ.get("ASANA_ACCESS_TOKEN", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "amplify-dev-secret")
PORT = int(os.environ.get("PORT", 5000))

# Google OAuth 2.0 credentials.
# Create a project in Google Cloud Console, enable the Google Identity API,
# and create an OAuth 2.0 Client ID (Web application).
# Add <your-app-url>/auth/callback as an Authorized Redirect URI.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "") or os.environ.get("SENDGRID_FROM_EMAIL", "")

# In-app announcements admin (Task #143)
# Live mode is enabled automatically when BOTH
# CHARTMETRIC_ADMIN_API_BASE_URL and CHARTMETRIC_ADMIN_API_TOKEN are set;
# either being missing forces stub mode. ANNOUNCEMENTS_STUB_MODE is a
# manual kill switch (truthy => force stub even when env is wired);
# leave it unset for normal operation.
CHARTMETRIC_ADMIN_API_BASE_URL = os.environ.get("CHARTMETRIC_ADMIN_API_BASE_URL", "")
CHARTMETRIC_ADMIN_API_TOKEN = os.environ.get("CHARTMETRIC_ADMIN_API_TOKEN", "")
CHARTMETRIC_MEDIA_UPLOAD_URL = os.environ.get("CHARTMETRIC_MEDIA_UPLOAD_URL", "")
ANNOUNCEMENTS_STUB_MODE = os.environ.get("ANNOUNCEMENTS_STUB_MODE", "")
