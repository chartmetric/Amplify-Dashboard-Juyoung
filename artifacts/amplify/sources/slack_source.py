from slack_sdk import WebClient
import config
from sources.base import SourceAdapter, FeatureContext


class SlackSource(SourceAdapter):
    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self._client = None

    def _get_client(self):
        if self._client is None:
            token = config.SLACK_BOT_TOKEN
            if not token:
                raise RuntimeError("SLACK_BOT_TOKEN not set")
            self._client = WebClient(token=token)
        return self._client

    def list_recent_features(self) -> list[dict]:
        client = self._get_client()
        result = client.conversations_history(
            channel=self.channel_id,
            limit=30,
        )

        features = []
        for msg in result.get("messages", []):
            text = msg.get("text", "")
            if len(text) < 50:
                continue
            features.append({
                "id": msg.get("ts", ""),
                "title": text[:80],
                "date": msg.get("ts", ""),
            })
            if len(features) >= 15:
                break

        return features

    def get_feature_context(self, feature_id: str, **kwargs) -> FeatureContext:
        client = self._get_client()

        result = client.conversations_history(
            channel=self.channel_id,
            oldest=feature_id,
            latest=feature_id,
            inclusive=True,
            limit=1,
        )
        messages = result.get("messages", [])
        if not messages:
            raise ValueError(f"Message {feature_id} not found")

        msg = messages[0]
        text = msg.get("text", "")

        replies_text = ""
        if msg.get("reply_count", 0) > 0:
            thread = client.conversations_replies(
                channel=self.channel_id,
                ts=feature_id,
            )
            reply_texts = []
            for reply in thread.get("messages", [])[1:]:
                reply_texts.append(reply.get("text", ""))
            replies_text = "\n---\n".join(reply_texts)

        return FeatureContext(
            title=text[:80],
            description=text,
            raw_details=replies_text,
            source_type="slack",
            metadata={
                "ts": msg.get("ts", ""),
                "user": msg.get("user", ""),
                "reply_count": msg.get("reply_count", 0),
            },
        )
