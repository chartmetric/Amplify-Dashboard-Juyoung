import os
import logging

import anthropic

logger = logging.getLogger("amplify.claude")


def generate_content(system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> dict:
    try:
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        content = message.content[0].text
        return {"success": True, "content": content, "error": None}
    except anthropic.RateLimitError as e:
        logger.error(f"Claude rate limit: {e}")
        return {"success": False, "content": "", "error": f"Rate limit exceeded: {e}"}
    except anthropic.APIError as e:
        logger.error(f"Claude API error: {e}")
        return {"success": False, "content": "", "error": f"API error: {e}"}
    except Exception as e:
        logger.error(f"Claude unexpected error: {e}")
        return {"success": False, "content": "", "error": str(e)}
