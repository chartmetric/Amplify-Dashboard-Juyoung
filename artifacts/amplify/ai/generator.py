import json
import logging
import os
import re
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


def get_content_cache_index():
    index = {}
    with _content_cache_lock:
        for key in _content_cache.keys():
            if ":" not in key:
                continue
            fid, ch = key.split(":", 1)
            if not fid or not ch:
                continue
            index.setdefault(fid, []).append(ch)
    return index


_load_content_cache()


_ATTACHMENT_LINE_RE = re.compile(
    r"^\s*\[(?:banner|badge|cta|image|video|hosted_image|attachment)\s*:.*?\]\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _clean_for_length(text: str) -> str:
    """Return text with attachment markup removed so length measurements
    reflect actual prose. Strips:
      - Whole-line `[banner|badge|cta|image|video|hosted_image|attachment: ...]` blocks
      - Markdown image syntax `![alt](url)`
      - Markdown link URLs (keeps the visible `[text]` portion only)
    """
    if not text:
        return ""
    cleaned = _ATTACHMENT_LINE_RE.sub("", text)
    cleaned = _MARKDOWN_IMAGE_RE.sub("", cleaned)
    cleaned = _MARKDOWN_LINK_RE.sub(lambda m: m.group(1), cleaned)
    return cleaned.strip()


def _measured_len(text: str) -> int:
    return len(_clean_for_length(text))


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
- LINK HANDLING:
  - When a Feature URL is provided, embed it according to the channel's rules below. When no Feature URL is provided ("Not provided"), write a natural verbal CTA phrase instead and never invent a URL.
  - Never paste any URL other than the supplied Feature URL.
  - Reference Chartmetric features and pages by name (e.g., "Charts", "Artist Page", "Sync") even when also linking to them.
- Channel-specific link rules:
  - Twitter: End the tweet with the Feature URL on its own final line (raw URL, no markdown). Twitter renders it as a clickable link automatically. Keep it under the character limit including the URL.
  - HMC article: No feature URL anywhere.
  - In-app, LinkedIn, Notion monthly, Marketing Newsletter (email_newsletter): End the body with a CTA sentence in the form "Check it out on [page/tab name](URL)" / "Try it in [page/tab name](URL)" / "Explore it on [page/tab name](URL)". The markdown link MUST wrap the destination's page/tab name (e.g., `[any Artist Page's YouTube tab](URL)`, `[the Sync tab](URL)`), never the verbal CTA phrase itself. Do not paste the bare URL.
  - Resend email channels (email_short, email_medium, email_long, email_standalone, email_standalone_digest): End the body with an inline hyperlink CTA sentence in the form "Check it out on [page/tab name](URL)" / "Try it on [page/tab name](URL)" / "Explore it in [page/tab name](URL)". The markdown link MUST wrap the destination's page/tab name (e.g., `[any Artist Page's YouTube tab](URL)`, `[the new Genius Charts page](URL)`, `[the Sync tab](URL)`) — never wrap the verbal CTA phrase itself ("Check it out", "Try it now", etc.) and never paste the bare URL. Place this sentence on its own final line so it reads as the closing CTA. Never emit `[cta: ...]` button syntax. Never repeat the URL twice."""

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
CHARACTER LIMIT: {max_chars}{length_range_hint}
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


AUTO_CTA_LINK_CHANNELS = {
    "email_newsletter",
    "inapp",
    "linkedin",
    "notion_monthly",
}

CHARTMETRIC_PATH_LABELS = [
    (r"/artist(?:/|$)", "the Artist Page"),
    (r"/track(?:/|$)", "the Track Page"),
    (r"/playlist(?:/|$)", "the Playlists page"),
    (r"/album(?:/|$)", "the Album Page"),
    (r"/label(?:/|$)", "the Label Page"),
    (r"/curator(?:/|$)", "the Curator Page"),
    (r"/charts?(?:/|$)", "the Charts page"),
    (r"/trends?(?:/|$)", "the Trends page"),
    (r"/sync(?:/|$)", "the Sync tab"),
    (r"/influencer", "the Influencers tab"),
    (r"/tiktok", "the TikTok tab"),
    (r"/youtube", "the YouTube tab"),
    (r"/spotify", "the Spotify tab"),
    (r"/instagram", "the Instagram tab"),
    (r"/cmpro|/insights", "My Insights"),
    (r"/discover", "Discover"),
    (r"/search", "Search"),
    (r"/dashboard", "your dashboard"),
]


def _label_from_url(url: str) -> str:
    """Best-effort short page/tab name derived from a Chartmetric URL path,
    used as the link text when the AI failed to embed the link itself."""
    import re as _re
    if not url:
        return "the page"
    try:
        path = _re.sub(r"^https?://[^/]+", "", url).split("?")[0].split("#")[0]
    except Exception:
        path = url
    for pattern, label in CHARTMETRIC_PATH_LABELS:
        if _re.search(pattern, path, _re.IGNORECASE):
            return label
    seg = path.strip("/").split("/")[0] if path.strip("/") else ""
    if seg:
        pretty = seg.replace("-", " ").replace("_", " ").strip().title()
        return f"the {pretty} page"
    return "the page"

RESEND_EMAIL_CHANNELS = {
    "email_short",
    "email_medium",
    "email_long",
    "email_standalone",
    "email_standalone_digest",
}

CONVERSION_URL_HINTS = (
    "/pricing", "/plans", "/plan", "/signup", "/sign-up", "/upgrade",
    "/billing", "/subscribe", "/trial", "/demo", "/contact",
)


def _is_conversion_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower().rstrip("/")
    if any(hint in u for hint in CONVERSION_URL_HINTS):
        return True
    import re as _re
    if _re.match(r"^https?://(?:www\.|app\.)?chartmetric\.com/?$", u):
        return True
    return False


def _conversion_cta_label(url: str) -> str:
    u = (url or "").lower()
    if "pricing" in u or "plans" in u or "plan" in u:
        return "See pricing"
    if "signup" in u or "sign-up" in u:
        return "Sign up"
    if "upgrade" in u or "billing" in u or "subscribe" in u:
        return "Upgrade now"
    if "trial" in u:
        return "Start free trial"
    if "demo" in u:
        return "Book a demo"
    if "contact" in u:
        return "Contact us"
    return "Get started"


def _auto_append_cta_link(content: str, channel_key: str, feature_url: str | None) -> str:
    """Make sure the Feature URL ends up in the rendered draft.

    - email_newsletter / inapp / linkedin / notion_monthly: append a markdown
      `[Label](url)` line (those surfaces don't have a separate CTA mechanism).
    - twitter: append the raw URL on its own final line so X autolinks it.
    - Resend email channels: prefer to leave whatever the AI produced (it is
      instructed to weave inline links for specific pages and use a
      `[cta: text=...|url=...]` button for conversion pages). If the AI
      forgot the URL entirely, fall back to a `[cta: ...]` block for
      conversion URLs or a standalone `[Learn more](url)` line for feature
      pages — sendgrid_client renders both as buttons.

    No-ops if the body already references the URL."""
    if not feature_url or not content:
        return content
    if feature_url in content:
        return content

    if channel_key == "twitter":
        return content.rstrip() + "\n\n" + feature_url

    if channel_key in AUTO_CTA_LINK_CHANNELS:
        label = _label_from_url(feature_url)
        verb = "Try it on" if "tab" in label else "Check it out on"
        return content.rstrip() + "\n\n" + f"{verb} [{label}]({feature_url})."

    if channel_key in RESEND_EMAIL_CHANNELS:
        page_label = _label_from_url(feature_url)
        verb = "Try it on" if "tab" in page_label else "Check it out on"
        return content.rstrip() + "\n\n" + f"{verb} [{page_label}]({feature_url})."

    return content


def generate_for_channel(feature_data: dict, channel_key: str, custom_instructions: str = None, feedback: str = None, current_content: str = None, skip_cache: bool = False, mode: str = None) -> dict:
    feature_id = feature_data.get("id", "")

    requested_channel = channel_key
    if mode == "digest" and channel_key == "email_standalone":
        channel_key = "email_standalone_digest"

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

    min_chars = config.get("min_chars")
    if min_chars:
        length_range_hint = (
            f"\nLENGTH RANGE: Aim for {min_chars}–{config['max_chars']} characters of prose. "
            f"Drafts under {min_chars} characters will be rejected and regenerated. "
            f"This range is measured on prose only — banners, badges, CTA blocks, and image/video attachments don't count."
        )
    else:
        length_range_hint = ""

    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=feature_data.get("title", ""),
        description=feature_data.get("description", ""),
        release_status="Released" if release_status else "In Progress",
        release_date=feature_data.get("release_date", "N/A"),
        assignee=feature_data.get("assignee") or "N/A",
        engineer=feature_data.get("engineer") or "N/A",
        planner=feature_data.get("planner") or "N/A",
        reactions_info=reactions_info,
        feature_url=feature_data.get("feature_url") or feature_data.get("chartmetric_url") or "Not provided",
        channel_display_name=config["display_name"],
        max_chars=config["max_chars"],
        length_range_hint=length_range_hint,
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
    char_floor = config.get("min_chars")

    if result["success"]:
        measured = _measured_len(content)
        if measured > char_limit:
            logger.info(f"[{channel_key}] Content is {measured} prose chars, exceeds {char_limit}. Requesting shorter version.")
            shorten_prompt = (
                f"The following content is {measured} characters but must be under {char_limit} characters. "
                f"Shorten it while keeping the same tone and key message. Output ONLY the shortened version:\n\n{content}"
            )
            retry_result = generate_content(SYSTEM_PROMPT, shorten_prompt, max_tokens=max_tokens)
            if retry_result["success"] and retry_result.get("content"):
                content = retry_result["content"]
                was_trimmed = True
                measured = _measured_len(content)
                logger.info(f"[{channel_key}] Shortened to {measured} prose chars.")

            if measured > char_limit:
                logger.warning(f"[{channel_key}] Still {measured} prose chars after retry. Truncating at last sentence.")
                content = _truncate_to_last_sentence(content, char_limit)
                was_trimmed = True
                measured = _measured_len(content)

        if char_floor and measured < char_floor:
            logger.info(f"[{channel_key}] Content is {measured} prose chars, below floor {char_floor}. Requesting expanded version.")
            expand_prompt = (
                f"The following content is {measured} characters but should be between {char_floor} and {char_limit} characters of prose. "
                f"Expand it to fall within that range while preserving tone, structure, the closing CTA sentence, and any attachment markup. "
                f"Add concrete benefits, use cases, or supporting detail — do not pad with filler. "
                f"Output ONLY the expanded version:\n\n{content}"
            )
            expand_result = generate_content(SYSTEM_PROMPT, expand_prompt, max_tokens=max_tokens)
            if expand_result["success"] and expand_result.get("content"):
                content = expand_result["content"]
                measured = _measured_len(content)
                logger.info(f"[{channel_key}] Expanded to {measured} prose chars.")
            else:
                logger.warning(f"[{channel_key}] Expand retry failed; keeping original short draft.")

        cta_url = feature_data.get("feature_url") or feature_data.get("chartmetric_url")
        content_before_cta = content
        content = _auto_append_cta_link(content, channel_key, cta_url)

        if content != content_before_cta and _measured_len(content) > char_limit:
            suffix = content[len(content_before_cta):]
            suffix_len = _measured_len(suffix)
            body_budget = char_limit - suffix_len - 4
            if body_budget > 0:
                trimmed_body = _truncate_to_last_sentence(content_before_cta, body_budget)
                content = _auto_append_cta_link(trimmed_body, channel_key, cta_url)
                was_trimmed = True
                logger.info(f"[{channel_key}] Trimmed body to {_measured_len(trimmed_body)} chars to fit appended CTA under {char_limit}.")

    gen_result = {
        "channel": requested_channel,
        "channel_display_name": config["display_name"],
        "max_chars": char_limit,
        "content": content,
        "char_count": _measured_len(content),
        "was_trimmed": was_trimmed,
        "success": result["success"],
        "error": result.get("error"),
        "mode": mode or "default",
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
