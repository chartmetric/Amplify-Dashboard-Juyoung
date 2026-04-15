import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from ai.channel_configs import CHANNEL_CONFIGS
from ai.claude_client import generate_content
from ai.few_shot_examples import FEW_SHOT_EXAMPLES
from ai.feedback_store import get_feedback_history

logger = logging.getLogger("amplify.generator")

_content_cache = {}
_content_cache_lock = threading.Lock()
_CONTENT_CACHE_FILE = ".content_cache.json"


def _load_content_cache():
    global _content_cache
    if os.path.exists(_CONTENT_CACHE_FILE):
        try:
            with open(_CONTENT_CACHE_FILE, "r") as f:
                _content_cache = json.load(f)
            logger.info(f"[content-cache] Loaded {len(_content_cache)} cached entries from disk")
        except Exception as e:
            logger.warning(f"[content-cache] Failed to load cache: {e}")
            _content_cache = {}


def _save_content_cache():
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=".", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(_content_cache, f)
        os.replace(tmp, _CONTENT_CACHE_FILE)
    except Exception as e:
        logger.warning(f"[content-cache] Failed to save cache: {e}")


def _cache_key(feature_id, channel):
    return f"{feature_id}:{channel}"


def get_cached_content(feature_id, channel):
    key = _cache_key(feature_id, channel)
    with _content_cache_lock:
        result = _content_cache.get(key)
        if result is None and channel == "email_standalone":
            for fallback in ("email_medium", "email_short", "email_long"):
                result = _content_cache.get(_cache_key(feature_id, fallback))
                if result is not None:
                    break
        return result


def set_cached_content(feature_id, channel, result):
    key = _cache_key(feature_id, channel)
    with _content_cache_lock:
        _content_cache[key] = result
        _save_content_cache()


def get_content_cache_stats():
    with _content_cache_lock:
        return {"total_cached": len(_content_cache)}


_load_content_cache()


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
- Never fabricate data points or statistics \u2014 only reference data if it's in the feature context
- ABSOLUTELY NEVER use the em dash character (\u2014) in any generated content. This is a hard rule with zero exceptions. Use periods, commas, or semicolons instead. Rewrite sentences to avoid needing dashes entirely. Also never use en dashes (\u2013). Only use regular hyphens (-) for compound words.
- If a Feature URL is provided, embed it as a hyperlink within a natural CTA phrase. Do NOT paste the raw URL. Instead, use markdown link format: [anchor text](URL). The anchor text should be a natural phrase that tells the user where to go, like [Artist Page](https://app.chartmetric.com/artist/123), [Charts section](https://app.chartmetric.com/charts), or [Try it here](URL). Channel-specific rules for URLs:
  - Twitter: Do NOT include any URL in the tweet text. URLs get added separately.
  - Email (newsletter, email_short, email_medium, email_long): Use markdown hyperlink format [text](URL) in the CTA.
  - In-app: Use markdown hyperlink format [text](URL) in the CTA.
  - LinkedIn: Use markdown hyperlink format [text](URL) or plain URL at the end.
  - Notion monthly: Include as a markdown hyperlink reference.
  - HMC article: Do not include the feature URL."""

USER_PROMPT_TEMPLATE = """FEATURE CONTEXT:
Title: {title}
Description: {description}
Release Status: {release_status}
Release Date: {release_date}
Assignee: {assignee}
Engineer: {engineer}
Planner: {planner}
Team Reactions: {reactions_info}
Feature URL: {feature_url}

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

{current_content_section}

Generate the content now. Output ONLY the final content, nothing else."""


def generate_for_channel(feature_data: dict, channel_key: str, custom_instructions: str = None, feedback: str = None, current_content: str = None, skip_cache: bool = False) -> dict:
    feature_id = feature_data.get("id", "")

    if not skip_cache and not feedback and not custom_instructions and feature_id:
        cached = get_cached_content(feature_id, channel_key)
        if cached:
            logger.info(f"[{channel_key}] Cache hit for feature {feature_id}")
            cached_copy = dict(cached)
            cached_copy["from_cache"] = True
            return cached_copy

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
    raw_reactions = feature_data.get("reactions_breakdown") or {}
    reactions_info = "No reactions data"
    if raw_reactions:
        if isinstance(raw_reactions, dict):
            parts = [f":{name}: x{count}" for name, count in raw_reactions.items() if count]
            if parts:
                reactions_info = ", ".join(parts)
        elif isinstance(raw_reactions, list):
            parts = []
            for r in raw_reactions:
                if isinstance(r, dict) and "name" in r:
                    parts.append(f":{r['name']}: x{r.get('count', 1)}")
                elif isinstance(r, str):
                    parts.append(f":{r}:")
            if parts:
                reactions_info = ", ".join(parts)

    custom_instructions_section = ""
    if custom_instructions:
        custom_instructions_section = f"ADDITIONAL MARKETER INSTRUCTIONS: {custom_instructions}"

    feedback_section = ""
    if feedback:
        feedback_section = f"FEEDBACK ON PREVIOUS DRAFT \u2014 please improve based on this: {feedback}"

    current_content_section = ""
    if current_content:
        import re as _re
        has_images = bool(_re.search(r'\[image:\s*[^\]]+\]', current_content))
        has_links = bool(_re.search(r'\[([^\]]+)\]\((https?://[^\)]+)\)', current_content))
        preserve_parts = []
        if has_images:
            preserve_parts.append("image markers (lines like [image: filename.png]) — copy them VERBATIM into your output")
        if has_links:
            preserve_parts.append("markdown hyperlinks (like [text](url)) — copy them VERBATIM into your output")
        preserve_note = ""
        if preserve_parts:
            preserve_note = (
                f"\n\n⚠️ CRITICAL PRESERVATION RULES — FAILURE TO FOLLOW THESE WILL BREAK THE EMAIL:\n"
                f"1. You MUST include these elements EXACTLY as they appear in the current draft: {', '.join(preserve_parts)}.\n"
                f"2. Do NOT remove, rephrase, or omit any [image: ...] marker or [text](url) link.\n"
                f"3. Keep them in approximately the same position within the content.\n"
                f"4. Only revise the surrounding text based on the feedback. The markers and links are non-negotiable."
            )
        context_label = "CURRENT DRAFT (revise this based on feedback above):" if feedback else "CURRENT DRAFT (use as reference — preserve structure, images, and links):"
        current_content_section = f"{context_label}\n{current_content}{preserve_note}"

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
        feature_url=feature_data.get("feature_url") or "Not provided",
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
        current_content_section=current_content_section,
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

    gen_result = {
        "channel": channel_key,
        "channel_display_name": config["display_name"],
        "max_chars": char_limit,
        "content": content,
        "char_count": len(content),
        "was_trimmed": was_trimmed,
        "success": result["success"],
        "error": result.get("error"),
    }

    if result["success"] and feature_id:
        set_cached_content(feature_id, channel_key, gen_result)

    return gen_result


def generate_all_channels(feature_data: dict, channels: list[str] = None, custom_instructions: str = None) -> dict:
    if channels is None:
        channels = [k for k, v in CHANNEL_CONFIGS.items() if v.get("enabled", False)]

    results = {}
    with ThreadPoolExecutor(max_workers=len(channels)) as executor:
        future_to_channel = {
            executor.submit(generate_for_channel, feature_data, ch, custom_instructions=custom_instructions): ch
            for ch in channels
        }
        for future in as_completed(future_to_channel):
            ch = future_to_channel[future]
            try:
                results[ch] = future.result()
            except Exception as e:
                logger.error(f"[{ch}] Generation thread error: {e}")
                results[ch] = {
                    "channel": ch,
                    "channel_display_name": CHANNEL_CONFIGS.get(ch, {}).get("display_name", ch),
                    "max_chars": CHANNEL_CONFIGS.get(ch, {}).get("max_chars", 0),
                    "content": "",
                    "char_count": 0,
                    "was_trimmed": False,
                    "success": False,
                    "error": str(e),
                }

    return results
