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
from ai.feature_url_overrides import get_url_override_learning_context

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
- HEADLINE / TITLE HANDLING: The "Internal Feature Name" provided in FEATURE CONTEXT is the raw Asana / Slack ticket title (e.g., "Introduce New Playlist Scores on Spotify Playlist List 2", "Add X to Y", "Refactor Z"). It is engineering-speak, not marketing copy. NEVER reuse it verbatim as the headline / title / header line for any channel. Always craft a fresh, benefit-driven, user-facing headline derived from the Description (which is usually the cleaner, marketer-ready summary) and the actual user impact. Drop internal verbs like "Introduce", "Add", "Implement", "Refactor"; drop ticket-suffix noise like trailing numbers ("2"), "v2", or "(WIP)". Lead with what the user can now do, not what was built. The Internal Feature Name is provided only as context for what the feature is.
- TITLE + SUBTITLE PATTERN: For every channel that emits a headline (email_newsletter, email_short, email_medium, email_long, email_standalone, email_standalone_digest, inapp, notion_monthly, article_hmc), open the body with a TWO-LINE pair before any other content:

  THINK OF IT THIS WAY:
    Title    → "What is it?"          (names the capability)
    Subtitle → "Why should I care?"   (what the user can now do or find)

  1. TITLE (line 1) — bolded with **...** (or `# ` heading for the HMC article). A punchy NOUN PHRASE that NAMES THE CAPABILITY. ~5-7 words. NOT a sentence, NOT an announcement, NOT a launch headline. Examples: "Smarter Playlist Discovery on Spotify", "Full Analytics on Your Shortlists", "Real-Time Playlist Scoring", "Plain-English Data Assistant".
  2. SUBTITLE (line 2, directly under the title — no blank line between them) — BOLDED with **...** (same emphasis as the title). No bullet, no '#'. One short line that reframes the title FROM THE USER'S PERSPECTIVE: what they can now DO or FIND. Same energy as the title, conversational, no jargon. Examples: "**Find playlists people actually listen to**", "**See exactly who listens, where, and why**", "**Stop guessing which playlists matter**", "**Ask your music questions in plain English**".

  Then a BLANK LINE, then the body content.

  HARD RULES — read carefully, the AI keeps breaking these:
    a) The TITLE is a NOUN PHRASE that names the capability. It is NEVER a launch / availability / release announcement. BANNED title phrasings include any form of: "Now Available", "Now Live", "Just Launched", "Just Released", "Introducing", "Announcing", "Coming Soon", "Available to All [Users/Plans/Tiers]", "Now Open to ...", "Rolling Out to ...", "Live for ...". Drop that framing entirely and name the thing itself.
    b) The SUBTITLE MUST contain DIFFERENT WORDS from the title — not a cosmetic rewording, not a definition, not a paraphrase. Title = WHAT the capability is. Subtitle = WHAT THE USER CAN DO / FIND with it. Different angle, different vocabulary.
    c) The subtitle should usually start with a user-facing verb ("Find...", "See...", "Spot...", "Stop guessing...", "Understand...", "Get...", "Ask...") OR a noun phrase that names the user's outcome ("Fewer dead-end pitches", "More playlists you can actually pitch to").
    d) Concrete WRONG / RIGHT pairs:

       WRONG TITLE (announcement framing)  →  **Data Assistant Now Available to All Premium Users**
                                              **The Data Assistant is now available to all premium users.**
       RIGHT (noun-phrase title + user-perspective subtitle)
                                           →  **Plain-English Data Assistant**
                                              **Ask your music questions and get answers without writing a query.**

       WRONG TITLE (announcement framing)  →  **Introducing Smarter Playlist Discovery on Spotify**
                                              **We're excited to launch smarter playlist discovery.**
       RIGHT                               →  **Smarter Playlist Discovery on Spotify**
                                              **Find playlists people actually listen to.**

       WRONG SUBTITLE (mirrors title)      →  **Full Analytics on Your Shortlists**
                                              **Full analytics for your shortlists.**
       RIGHT                               →  **Full Analytics on Your Shortlists**
                                              **Spot the strongest tracks before your A&R meeting.**

       WRONG SUBTITLE (defines the title)  →  **Real-Time Playlist Scoring**
                                              **Real-time playlist scoring is now available.**
       RIGHT                               →  **Real-Time Playlist Scoring**
                                              **Know which playlists are worth pitching the moment they move.**

  Channels WITHOUT this pattern: twitter (single block, no headline), linkedin (trend-hook open), did_you_know (single fact line).
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
Internal Feature Name (raw ticket title — do NOT use verbatim as a headline; craft a fresh benefit-driven headline from the Description below): {title}
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


_BENEFIT_TITLE_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_BENEFIT_TITLE_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
_BENEFIT_TITLE_SUBJECT_RE = re.compile(
    r"^\s*Subject:\s*(.+?)(?:\r?\n\s*\r?\n|\r?\n|$)", re.IGNORECASE
)
_BENEFIT_TITLE_CTA_PREFIXES = (
    "check it out", "try it", "explore", "learn more", "see ",
    "get started", "click here", "discover", "head to", "head over",
    "find out", "read more",
)


def extract_benefit_title(
    content: str, channel_key: str, raw_title: str = ""
) -> str | None:
    """Pull a benefit-driven, marketer-ready headline from AI-generated content
    so the batch card can show it instead of the raw Asana / Slack ticket
    title. Returns None when no clean headline can be lifted (Twitter has no
    headline format; some prose channels may not emit a heading; the AI may
    have ignored prompt rules and reused the raw title — in all those cases
    callers should fall back to the original feature title).

    Strategy:
      1. Skip the leading "Subject: ..." line for email channels.
      2. Look for the first markdown heading (`# X`, `## X`, ...).
      3. Otherwise look for a standalone bold line (`**X**` on its own line)
         within the first few lines.
      4. Reject anything too short (<2 words) or too long (>14 words),
         containing a URL, looking like a CTA, or essentially identical
         to the raw ticket title (case- and punctuation-insensitive).
    """
    if not content or channel_key == "twitter":
        return None

    text = content
    sub_match = _BENEFIT_TITLE_SUBJECT_RE.match(text)
    if sub_match:
        text = text[sub_match.end():].lstrip()

    headline = None
    h_match = _BENEFIT_TITLE_HEADING_RE.search(text)
    if h_match:
        headline = h_match.group(1).strip()
    else:
        for line in text.split("\n", 8)[:5]:
            stripped = line.strip()
            if not stripped:
                continue
            b_match = _BENEFIT_TITLE_BOLD_RE.match(stripped)
            if b_match:
                headline = b_match.group(1).strip()
            break  # only consider the first non-empty line as a candidate

    if not headline:
        return None

    headline = re.sub(r"\s+", " ", headline).strip().strip("\"'`*_")
    if not headline:
        return None

    word_count = len(headline.split())
    if word_count < 2 or word_count > 14:
        return None
    lower = headline.lower()
    if "http://" in lower or "https://" in lower or "www." in lower:
        return None
    if lower.startswith(_BENEFIT_TITLE_CTA_PREFIXES):
        return None

    if raw_title:
        norm_a = re.sub(r"[^a-z0-9]+", "", lower)
        norm_b = re.sub(r"[^a-z0-9]+", "", raw_title.lower())
        if norm_a and norm_a == norm_b:
            return None

    return headline


_TITLE_LINE_PATTERNS = (
    re.compile(r"^\s*\*\*(.+?)\*\*\s*$"),
    re.compile(r"^\s*#{1,6}\s+(.+?)\s*$"),
)


def _normalize_for_compare(s: str) -> str:
    """Lowercase + strip non-alphanumeric so 'Foo Bar!' == '**foo-bar**'."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _strip_title_markup(line: str) -> str | None:
    """If `line` is a recognized title line (`**X**` or `# X`), return X (with
    surrounding markdown markers stripped). Otherwise return None."""
    if not line:
        return None
    for pat in _TITLE_LINE_PATTERNS:
        m = pat.match(line)
        if m:
            inner = m.group(1).strip()
            inner = re.sub(r"^[*_`#\s]+|[*_`#\s]+$", "", inner)
            return inner or None
    return None


_BANNED_TITLE_PATTERNS = [
    re.compile(r"\bnow\s+available\b", re.IGNORECASE),
    re.compile(r"\bnow\s+live\b", re.IGNORECASE),
    re.compile(r"\bjust\s+launched\b", re.IGNORECASE),
    re.compile(r"\bjust\s+released\b", re.IGNORECASE),
    re.compile(r"\bintroducing\b", re.IGNORECASE),
    re.compile(r"\bannouncing\b", re.IGNORECASE),
    re.compile(r"\bcoming\s+soon\b", re.IGNORECASE),
    re.compile(r"\bavailable\s+to\s+all\b", re.IGNORECASE),
    re.compile(r"\bnow\s+open\s+to\b", re.IGNORECASE),
    re.compile(r"\brolling\s+out\s+to\b", re.IGNORECASE),
    re.compile(r"\blive\s+for\s+(?:all\s+)?(?:premium|free|pro|users|customers|members)\b", re.IGNORECASE),
]


def _extract_first_title_line(content: str, channel_key: str) -> str | None:
    """Return the title text (e.g. inside **...** or `# ...`) at the very top
    of the content, after skipping the optional Resend subject prefix and the
    optional `meta_description:` line for HMC. Returns None if there is no
    recognizable title-style line."""
    if not content:
        return None
    text = content
    sub_match = _BENEFIT_TITLE_SUBJECT_RE.match(text)
    if sub_match:
        text = text[sub_match.end():]
    if channel_key == "article_hmc":
        m = re.match(r"^(\s*meta_description:[^\n]*\n+)", text, re.IGNORECASE)
        if m:
            text = text[m.end():]
    for line in text.split("\n"):
        if not line.strip():
            continue
        stripped = _strip_title_markup(line)
        if stripped:
            return stripped
        return line.strip()
    return None


def detect_banned_title_phrase(content: str, channel_key: str) -> str | None:
    """If the first title line uses launch/availability announcement framing
    (e.g. "Now Available to All Premium Users", "Introducing X", "Just
    Launched"), return the matched phrase. Otherwise return None.
    Skips channels that don't use the title+subtitle pattern."""
    if channel_key in {"twitter", "linkedin", "did_you_know"}:
        return None
    title = _extract_first_title_line(content, channel_key)
    if not title:
        return None
    for pattern in _BANNED_TITLE_PATTERNS:
        m = pattern.search(title)
        if m:
            return m.group(0)
    return None


def _find_title_subtitle_pair(content: str, channel_key: str):
    """Locate the title + subtitle adjacent pair at the top of content.
    Returns a dict with the parsed pieces, or None if there isn't a
    title+subtitle layout to inspect.

    Returned dict keys:
      prefix:        text before the title (Resend Subject:/HMC meta_description)
      lines:         the body lines (text.split('\\n'))
      title_idx:     index of the title line in `lines`
      subtitle_idx:  index of the subtitle line in `lines` (the next non-blank
                     line after the title — usually title_idx+1, but the AI
                     sometimes inserts a blank line between the bolded title
                     and the bolded subtitle, so we skip blanks here)
      title_text:    the title text without markup
      subtitle_raw:  the subtitle line as-is
      subtitle_clean: subtitle stripped of bold/punct for comparison
    """
    if not content:
        return None
    if channel_key in {"twitter", "linkedin", "did_you_know"}:
        return None

    text = content
    sub_match = _BENEFIT_TITLE_SUBJECT_RE.match(text)
    prefix = ""
    if sub_match:
        prefix = text[:sub_match.end()]
        text = text[sub_match.end():]

    if channel_key == "article_hmc":
        m = re.match(r"^(\s*meta_description:[^\n]*\n+)", text, re.IGNORECASE)
        if m:
            prefix = prefix + m.group(1)
            text = text[m.end():]

    lines = text.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) - 1:
        return None

    title_text = _strip_title_markup(lines[i])
    if not title_text:
        return None

    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines):
        return None
    subtitle_raw = lines[j]
    if not subtitle_raw.strip():
        return None  # no non-blank line after title -> no subtitle slot

    subtitle_clean = re.sub(r"^[*_`#\s]+|[*_`#\s\.\!\?]+$", "", subtitle_raw.strip())
    if not subtitle_clean:
        return None

    return {
        "prefix": prefix,
        "lines": lines,
        "title_idx": i,
        "subtitle_idx": j,
        "title_text": title_text,
        "subtitle_raw": subtitle_raw,
        "subtitle_clean": subtitle_clean,
    }


def detect_duplicate_subtitle(content: str, channel_key: str):
    """If the title+subtitle pair has the subtitle mirroring the title
    (alphanumeric-equivalent), return a (title, subtitle) tuple. Otherwise
    return None. Skips channels that don't use the title+subtitle pattern."""
    pair = _find_title_subtitle_pair(content, channel_key)
    if not pair:
        return None
    if _normalize_for_compare(pair["subtitle_clean"]) == _normalize_for_compare(pair["title_text"]):
        return (pair["title_text"], pair["subtitle_raw"].strip())
    return None


def dedupe_title_subtitle(content: str, channel_key: str) -> str:
    """Last-resort safety net: if the AI emitted a subtitle that is
    identical (or alphanumeric-equivalent) to the title, drop the duplicate
    subtitle line. Prefer the regen retry in generate_for_channel — this
    only fires if the AI still produced a duplicate after the retry.

    Examples handled:
      "**Foo Bar**\nFoo Bar\n\nbody"        -> "**Foo Bar**\n\nbody"
      "**Foo Bar**\nfoo bar.\n\nbody"       -> "**Foo Bar**\n\nbody"
      "### **Foo Bar**\nFoo Bar\nbody"      -> "### **Foo Bar**\nbody"
      "# Foo Bar\nFoo Bar\n\nbody"          -> "# Foo Bar\n\nbody"
    """
    pair = _find_title_subtitle_pair(content, channel_key)
    if not pair:
        return content
    if _normalize_for_compare(pair["subtitle_clean"]) != _normalize_for_compare(pair["title_text"]):
        return content

    logger.info(
        f"[{channel_key}] Stripping duplicate subtitle line that mirrors title: "
        f"{pair['subtitle_raw'].strip()!r}"
    )
    lines = list(pair["lines"])
    j = pair["subtitle_idx"]
    title_idx = pair["title_idx"]
    # Remove the duplicate subtitle line.
    del lines[j]
    # If the AI inserted blank lines BETWEEN the title and the (now removed)
    # subtitle, those blanks would now sit immediately under the title and
    # then bump up against more blanks after the subtitle, producing an
    # unsightly gap. Collapse blanks between the title and the next non-blank
    # paragraph down to a single blank line.
    k = title_idx + 1
    blank_run_start = k
    while k < len(lines) and not lines[k].strip():
        k += 1
    if k - blank_run_start > 1:
        del lines[blank_run_start + 1:k]
    new_text = "\n".join(lines)
    return pair["prefix"] + new_text


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

    url_override_section = get_url_override_learning_context(limit=3)
    if url_override_section:
        feedback_learning_section = (
            f"{feedback_learning_section}\n{url_override_section}"
            if feedback_learning_section else url_override_section
        )

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

    if result["success"] and content:
        banned = detect_banned_title_phrase(content, channel_key)
        if banned:
            logger.info(
                f"[{channel_key}] Title contains banned announcement phrasing "
                f"{banned!r}. Regenerating once with explicit feedback."
            )
            regen_prompt = (
                f"The title in the content below uses the BANNED announcement phrase \"{banned}\". "
                f"Per the TITLE+SUBTITLE rules, the title is a NOUN PHRASE that NAMES THE CAPABILITY — "
                f"never a launch / availability / release announcement. Drop \"{banned}\" entirely "
                f"and rewrite the title as a punchy 5-7 word noun phrase that names what the capability IS "
                f"(examples of the right shape: \"Plain-English Data Assistant\", "
                f"\"Smarter Playlist Discovery on Spotify\", \"Real-Time Playlist Scoring\"). "
                f"Then write a fresh subtitle from the user's perspective — different words from the title, "
                f"answering \"why should I care?\" (e.g. \"Ask your music questions and get answers without writing a query.\"). "
                f"Keep the rest of the body, the closing CTA sentence, and any attachment markup intact. "
                f"Output ONLY the rewritten content:\n\n{content}"
            )
            regen_result = generate_content(SYSTEM_PROMPT, regen_prompt, max_tokens=max_tokens)
            if regen_result["success"] and regen_result.get("content"):
                still_banned = detect_banned_title_phrase(regen_result["content"], channel_key)
                if still_banned:
                    logger.warning(
                        f"[{channel_key}] Title still contains banned phrase "
                        f"{still_banned!r} after regen. Keeping regenerated draft anyway."
                    )
                content = regen_result["content"]
            else:
                logger.warning(f"[{channel_key}] Title-rewrite retry failed; keeping original draft.")

        dup = detect_duplicate_subtitle(content, channel_key)
        if dup:
            dup_title, dup_subtitle = dup
            logger.info(
                f"[{channel_key}] Subtitle duplicates title ({dup_subtitle!r} == {dup_title!r}). "
                f"Regenerating once with explicit feedback."
            )
            dup_regen_prompt = (
                f"In the content below, the subtitle line is identical (or a cosmetic rewording) "
                f"of the title line. The title is \"{dup_title}\" and the subtitle is \"{dup_subtitle}\". "
                f"This violates the TITLE+SUBTITLE rule. The subtitle MUST contain DIFFERENT WORDS from "
                f"the title and reframe it from the user's perspective — what they can now DO or FIND. "
                f"Keep the title \"{dup_title}\" exactly as-is, but replace the subtitle line with a fresh "
                f"one-line subtitle that answers \"why should I care?\" — different vocabulary from the title, "
                f"starting with a user-facing verb (Find..., See..., Spot..., Stop guessing..., Understand..., "
                f"Get..., Ask...) or a noun phrase naming the user's outcome. "
                f"The subtitle line MUST be wrapped in **...** (bold), same emphasis as the title. "
                f"Keep everything else (body, closing CTA sentence, attachment markup) intact. "
                f"Output ONLY the rewritten content:\n\n{content}"
            )
            dup_regen_result = generate_content(SYSTEM_PROMPT, dup_regen_prompt, max_tokens=max_tokens)
            if dup_regen_result["success"] and dup_regen_result.get("content"):
                still_dup = detect_duplicate_subtitle(dup_regen_result["content"], channel_key)
                if still_dup:
                    logger.warning(
                        f"[{channel_key}] Subtitle still duplicates title after regen "
                        f"({still_dup[1]!r}). dedupe_title_subtitle will strip it as last resort."
                    )
                content = dup_regen_result["content"]
            else:
                logger.warning(f"[{channel_key}] Duplicate-subtitle retry failed; keeping original draft.")

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

        # Final guard: shorten/expand could theoretically reintroduce banned
        # title phrasing. If so, log it — we don't retry again to avoid
        # ping-ponging with the length retries above.
        post_retry_banned = detect_banned_title_phrase(content, channel_key)
        if post_retry_banned:
            logger.warning(
                f"[{channel_key}] Banned title phrasing {post_retry_banned!r} "
                f"reappeared after length retries. Keeping draft; user can regenerate."
            )

        content = dedupe_title_subtitle(content, channel_key)

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
