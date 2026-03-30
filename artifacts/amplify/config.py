import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ASANA_ACCESS_TOKEN = os.environ.get("ASANA_ACCESS_TOKEN", "")
    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
    SESSION_SECRET = os.environ.get("SESSION_SECRET", "amplify-dev-key")
    PORT = int(os.environ.get("PORT", 5000))
