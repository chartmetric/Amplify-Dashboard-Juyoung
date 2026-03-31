import logging

from ai.channel_configs import CHANNEL_CONFIGS
from ai.claude_client import generate_content
from ai.few_shot_examples import FEW_SHOT_EXAMPLES
from ai.feedback_store import get_feedback_history

logger = logging.getLogger("amplify.generator")


def _truncate_to_last_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    for sep in [". ", "! ", "? ", ".\n", "!\n", "?\n"]:
        last_pos = truncated.rfind(sep)
        if last_pos > 0:
            return truncated[:last_pos + 1]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        return truncated[:last_space] + "..."
    return truncated[:max_chars - 3] + "..."


SYSTEM_PROMPT = """You are Amplify, a product marketing AI for Chartmetric \u2014 the leading music data analytics platform used by artists, managers, labels, publishers, and playlist curators worldwide.

Your job: Transform raw feature/update context into publish-ready marketing content for a specific channel.

BRAND VOICE:
- Data-informed, never hype-driven \u2014 let the numbers and user value speak
- Empowering \u2014 frame everything through what the USER can now do, not what was built
- Industry-savvy \u2014 you understand the music business deeply (streaming, charts, playlists, royalties, sync, touring)
- Professional but approachable \u2014 knowledgeable insider, not corporate press release
- Never salesy, never clickbait, never "we're excited to announce"

CORE PRINCIPLE: Every piece of content must answer "why should the reader care?" before explaining "what changed." Lead with value, impact, or insight \u2014 not feature mechanics.

TARGET PERSONAS:
- Artists & Managers: Want actionable insights to grow their career and understand their audience
- Labels & A&R: Want data to discover talent, evaluate signings, and track roster performance
- Music Publishers: Want royalty, sync licensing, and catalog intelligence
- Playlist Curators: Want to discover trending music with data backing and audience fit

IMPORTANT RULES:
- Write ONLY the content for the specified channel \u2014 no meta-commentary, no "here's your draft", no explanations
- Stay strictly within the character limit
- Adapt tone and format precisely to match the channel's conventions
- If the feature is backend-only or not user-facing, focus on the indirect user benefit (e.g., faster load times, more accurate data)
- If the feature context is vague, infer the most likely user benefit from context clues
- Reference specific Chartmetric features/pages by name when relevant (e.g., 'Artist Page', 'Track Page', 'Playlist tab')
- Never fabricate data points or statistics \u2014 only reference data if it's in the feature context"""

USER_PROMPT_TEMPLATE = """FEATURE CONTEXT:
Title: {title}
Description: {description}
Release Status: {release_status}
Release Date: {release_date}
Assignee: {assignee}
Engineer: {engineer}
Planner: {planner}
Team Reactions: {reactions_info}

CHANNEL: {channel_display_name}
CHARACTER LIMIT: {max_chars}
TONE: {tone}
FORMAT: {format_rules}
TARGET AUDIENCE: {audience}
EXPECTED OUTPUT FORMAT: {example_output_format}

{few_shot_section}

{feedback_learning_section}

{custom_instructions_section}

{feedback_section}

Generate the content now. Output ONLY the final content, nothing else."""


def generate_for_channel(feature_data: dict, channel_key: str, custom_instructions: str = None, feedback: str = None) -> dict:
    if channel_key not in CHANNEL_CONFIGS:
        return {
            "channel": channel_key,
            "content": "",
            "char_count": 0,
            "success": False,
            "error": f"Unknown channel: {channel_key}",
        }

    config = CHANNEL_CONFIGS[channel_key]
    if not config.get("enabled", False):
        return {
            "channel": channel_key,
            "content": "",
            "char_count": 0,
            "success": False,
            "error": f"Channel '{channel_key}' is disabled",
        }

    release_status = feature_data.get("release_status", False)
    reactions_breakdown = feature_data.get("reactions_breakdown") or []
    if reactions_breakdown:
        reactions_info = ", ".join(f":{r['name']}: x{r['count']}" for r in reactions_breakdown)
    else:
        reactions_info = "No reactions data"

    custom_instructions_section = ""
    if custom_instructions:
        custom_instructions_section = f"ADDITIONAL MARKETER INSTRUCTIONS: {custom_instructions}"

    feedback_section = ""
    if feedback:
        feedback_section = f"FEEDBACK ON PREVIOUS DRAFT \u2014 please improve based on this: {feedback}"

    examples = FEW_SHOT_EXAMPLES.get(channel_key, [])[:3]
    few_shot_section = ""
    if examples:
        parts = ["EXAMPLES OF REAL CHARTMETRIC CONTENT FOR THIS CHANNEL (match this style and quality):"]
        for ex in examples:
            parts.append(f"---\nContext: {ex['feature_context']}\nPublished Content:\n{ex['content']}\n---")
        few_shot_section = "\n".join(parts)

    feedback_records = get_feedback_history(channel_key, limit=3)
    feedback_learning_section = ""
    if feedback_records:
        parts = ["LEARNING FROM PAST EDITS (the marketer revised these AI drafts - learn from their corrections):"]
        for rec in feedback_records:
            parts.append(
                f"---\n"
                f"Feature: {rec['feature_title']}\n"
                f"Original AI Draft: {rec['original_draft']}\n"
                f"Marketer's Approved Version: {rec['approved_draft']}\n"
                f"What changed: {rec['feedback_note']}\n"
                f"---"
            )
        feedback_learning_section = "\n".join(parts)

    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=feature_data.get("title", ""),
        description=feature_data.get("description", ""),
        release_status="Released" if release_status else "In Progress",
        release_date=feature_data.get("release_date", "N/A"),
        assignee=feature_data.get("assignee") or "N/A",
        engineer=feature_data.get("engineer") or "N/A",
        planner=feature_data.get("planner") or "N/A",
        reactions_info=reactions_info,
        channel_display_name=config["display_name"],
        max_chars=config["max_chars"],
        tone=config["tone"],
        format_rules=config["format_rules"],
        audience=config["audience"],
        example_output_format=config["example_output_format"],
        few_shot_section=few_shot_section,
        feedback_learning_section=feedback_learning_section,
        custom_instructions_section=custom_instructions_section,
        feedback_section=feedback_section,
    )

    max_tokens = 4096 if channel_key == "article_hmc" else 1024

    result = generate_content(SYSTEM_PROMPT, user_prompt, max_tokens=max_tokens)

    content = result.get("content", "")
    was_trimmed = False
    char_limit = config["max_chars"]

    if result["success"] and len(content) > char_limit:
        logger.info(f"[{channel_key}] Content is {len(content)} chars, exceeds {char_limit}. Requesting shorter version.")
        shorten_prompt = (
            f"The following content is {len(content)} characters but must be under {char_limit} characters. "
            f"Shorten it while keeping the same tone and key message. Output ONLY the shortened version:\n\n{content}"
        )
        retry_result = generate_content(SYSTEM_PROMPT, shorten_prompt, max_tokens=max_tokens)
        if retry_result["success"] and retry_result.get("content"):
            content = retry_result["content"]
            was_trimmed = True
            logger.info(f"[{channel_key}] Shortened to {len(content)} chars.")

        if len(content) > char_limit:
            logger.warning(f"[{channel_key}] Still {len(content)} chars after retry. Truncating at last sentence.")
            truncated = _truncate_to_last_sentence(content, char_limit)
            content = truncated
            was_trimmed = True

    return {
        "channel": channel_key,
        "channel_display_name": config["display_name"],
        "max_chars": char_limit,
        "content": content,
        "char_count": len(content),
        "was_trimmed": was_trimmed,
        "success": result["success"],
        "error": result.get("error"),
    }


def generate_all_channels(feature_data: dict, channels: list[str] = None, custom_instructions: str = None) -> dict:
    if channels is None:
        channels = [k for k, v in CHANNEL_CONFIGS.items() if v.get("enabled", False)]

    results = {}
    for channel_key in channels:
        results[channel_key] = generate_for_channel(
            feature_data, channel_key, custom_instructions=custom_instructions
        )

    return results
