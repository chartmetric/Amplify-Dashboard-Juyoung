from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from sources.base import SourceAdapter, FeatureContext
from config import Config


class SlackSource(SourceAdapter):
    def __init__(self, channel_id: str = ""):
        self.channel_id = channel_id
        self.client = None

    def connect(self) -> bool:
        token = Config.SLACK_BOT_TOKEN
        if not token:
            return False
        self.client = WebClient(token=token)
        return True

    def fetch_features(self) -> list[FeatureContext]:
        if not self.client or not self.channel_id:
            return []

        features = []
        try:
            result = self.client.conversations_history(
                channel=self.channel_id,
                limit=50,
            )

            for message in result.get("messages", []):
                text = message.get("text", "")
                if not text:
                    continue

                lines = text.strip().split("\n")
                title = lines[0][:120]
                description = "\n".join(lines[1:]) if len(lines) > 1 else ""

                feature = FeatureContext(
                    title=title,
                    description=description,
                    source="slack",
                    raw_data=message,
                )
                features.append(feature)

        except SlackApiError:
            return []

        return features

    def get_source_name(self) -> str:
        return "slack"
