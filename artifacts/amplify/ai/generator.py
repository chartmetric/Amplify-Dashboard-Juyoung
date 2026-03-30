from config import Config


class ContentGenerator:
    def __init__(self):
        self.client = None

    def connect(self) -> bool:
        api_key = Config.ANTHROPIC_API_KEY
        if not api_key:
            return False

        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key)
            return True
        except Exception:
            return False

    def generate(self, prompt: str, max_tokens: int = 1024) -> str:
        if not self.client:
            return ""

        message = self.client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
