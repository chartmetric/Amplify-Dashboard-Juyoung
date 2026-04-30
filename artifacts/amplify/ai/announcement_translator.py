"""Claude-backed translation for announcement posts and categories.

For posts: takes the English title + Slate.js content blocks and returns
``translations`` JSONB matching the spec in
``docs/chartmetric-announcement-admin-api.md`` §4. Slate block structure
is preserved — only ``text`` node values are translated. Product names
(Chartmetric, Onesheet, Spotify, etc.) stay in English.

For categories: takes the English ``name`` and returns ``{ lang: { name } }``.

When ``ANTHROPIC_API_KEY`` is not configured, the helpers return an empty
``translations`` dict (the UI then shows empty per-language tabs that
the marketer can fill manually).
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from ai.announcement_serializer import deepcopy_blocks, walk_text_leaves
from ai.claude_client import generate_content

logger = logging.getLogger("amplify.announcement_translator")

TARGET_LOCALES = ("de", "es", "fr", "ja", "ko", "pt")
LOCALE_NAMES = {
    "de": "German",
    "es": "Spanish",
    "fr": "French",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese (European)",
}

_DO_NOT_TRANSLATE = (
    "Chartmetric", "Onesheet", "Spotify", "Apple Music", "TikTok",
    "YouTube", "Instagram", "SoundCloud", "Deezer", "Pandora",
)


def _translation_system_prompt() -> str:
    keep = ", ".join(_DO_NOT_TRANSLATE)
    return (
        "You translate marketing copy for the Chartmetric music data "
        "platform. Translate ONLY user-visible text. Keep these brand and "
        f"product names untranslated: {keep}. Preserve casing, punctuation, "
        "URLs, and any leading/trailing whitespace. Do not add any "
        "commentary or explanations.\n\n"
        "You will receive a JSON array of English strings and must reply "
        "with ONLY a JSON array of the same length where each element is "
        "the translation of the corresponding input string. No markdown "
        "fences, no extra keys, no commentary."
    )


def _translate_strings(strings: list[str], locale: str) -> list[str]:
    """Send a batch of English strings to Claude and return translations.

    Falls back to returning the originals on any error so the caller can
    still produce a usable record (UI will show English in that locale's
    tab and the marketer can edit).
    """
    if not strings:
        return []
    if not config.ANTHROPIC_API_KEY:
        return list(strings)
    user_prompt = (
        f"Translate to {LOCALE_NAMES[locale]}.\n"
        "Input strings (JSON array):\n"
        + json.dumps(strings, ensure_ascii=False)
        + "\n\nOutput a JSON array of the same length with the translations."
    )
    result = generate_content(
        system_prompt=_translation_system_prompt(),
        user_prompt=user_prompt,
        max_tokens=2048,
    )
    if not result.get("success"):
        logger.warning("Claude translation to %s failed: %s",
                       locale, result.get("error"))
        return list(strings)
    raw = (result.get("content") or "").strip()
    # Strip optional ```json fences just in case.
    if raw.startswith("```"):
        raw = raw.strip("`")
        first_newline = raw.find("\n")
        if first_newline != -1:
            raw = raw[first_newline + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and len(parsed) == len(strings):
            return [str(p) for p in parsed]
        logger.warning("Claude translation shape mismatch (lang=%s): "
                       "got %d items want %d",
                       locale, len(parsed) if isinstance(parsed, list) else -1,
                       len(strings))
    except Exception as e:
        logger.warning("Claude translation JSON parse failed (lang=%s): %s",
                       locale, e)
    return list(strings)


def translate_post(title: str, content_blocks: list[dict],
                   locales: tuple[str, ...] = TARGET_LOCALES) -> dict:
    """Generate ``translations`` JSONB for a post.

    Returns a dict like ``{ "de": {"title": "...", "content": [...]}, ... }``.
    Always returns an entry for every locale in ``locales`` (translations
    fall back to English on failure so the per-language tab is editable).
    """
    leaves = walk_text_leaves(content_blocks or [])
    leaf_texts = [l.get("text", "") for l in leaves]
    # Build a single payload of [title, ...leaf_texts] per locale.
    payload = [title] + leaf_texts

    out: dict[str, dict] = {l: {"title": title,
                                 "content": deepcopy_blocks(content_blocks or [])}
                            for l in locales}

    if not payload or all(not s for s in payload):
        return out

    def _do_one(lang: str) -> tuple[str, list[str]]:
        return lang, _translate_strings(payload, lang)

    with ThreadPoolExecutor(max_workers=min(6, len(locales))) as ex:
        futures = [ex.submit(_do_one, l) for l in locales]
        for fut in as_completed(futures):
            try:
                lang, translated = fut.result()
            except Exception as e:
                logger.warning("translate_post worker failed: %s", e)
                continue
            if not translated or len(translated) != len(payload):
                continue
            new_title = translated[0]
            new_leaves = translated[1:]
            new_blocks = out[lang]["content"]
            target_leaves = walk_text_leaves(new_blocks)
            for original_leaf, new_text in zip(target_leaves, new_leaves):
                original_leaf["text"] = new_text
            out[lang]["title"] = new_title
            out[lang]["content"] = new_blocks
    return out


def translate_category(name: str,
                       locales: tuple[str, ...] = TARGET_LOCALES) -> dict:
    """Generate ``translations`` JSONB for a category."""
    out: dict[str, dict] = {l: {"name": name} for l in locales}
    if not name:
        return out

    def _do_one(lang: str) -> tuple[str, list[str]]:
        return lang, _translate_strings([name], lang)

    with ThreadPoolExecutor(max_workers=min(6, len(locales))) as ex:
        futures = [ex.submit(_do_one, l) for l in locales]
        for fut in as_completed(futures):
            try:
                lang, translated = fut.result()
            except Exception as e:
                logger.warning("translate_category worker failed: %s", e)
                continue
            if translated and len(translated) == 1:
                out[lang] = {"name": translated[0]}
    return out
