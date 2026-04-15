import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY_2", "") or os.environ.get("ANTHROPIC_API_KEY", "")
ASANA_ACCESS_TOKEN = os.environ.get("ASANA_ACCESS_TOKEN", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "amplify-dev-secret")
PORT = int(os.environ.get("PORT", 5000))
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "") or os.environ.get("SENDGRID_FROM_EMAIL", "")
