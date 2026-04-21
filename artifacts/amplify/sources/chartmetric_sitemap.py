"""Heuristic mapper from raw feature text -> a Chartmetric app URL.

We ship `data/chartmetric_sitemap.json` (snapshot of the `sitemap` /
`sitemap_feature` join) and use simple keyword scoring against
feature_name + feature_description + url path tokens to pick the most
likely page when an Asana task body doesn't contain a Chartmetric URL.

Placeholder ids in url patterns (e.g. `{artist_id}`) are filled with the
canonical ids in PLACEHOLDER_IDS so the generated URL is a real,
load-able Chartmetric page.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from typing import Optional

logger = logging.getLogger("amplify.sitemap")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATA_PATH = os.path.normpath(os.path.join(_HERE, "..", "data", "chartmetric_sitemap.json"))

CHARTMETRIC_BASE = "https://app.chartmetric.com"

PLACEHOLDER_IDS = {
    "artist_id": "14209602",
    "album_id": "43",
    "track_id": "10922649",
    "brand_id": "1",
    "songwriter_id": "1",
    "label_id": "1",
    "festival_id": "176624",
    "playlist_id": "1",
    "curator_id": "1",
    "city_id": "1",
    "country_id": "1",
    "genre_id": "1",
    "video_id": "1",
    "sound_id": "1",
    "id": "1",
    "metric": "instagram",
}

_STOP = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "by", "is", "be", "are", "as", "at", "from", "this", "that", "these",
    "those", "it", "its", "into", "your", "you", "we", "our", "us",
    "new", "now", "page", "tab", "feature", "added", "add", "support",
    "supports", "release", "released", "update", "updated", "updates",
    "improvement", "improvements", "improved", "fix", "fixed", "fixes",
    "enhancement", "enhanced", "enhancements",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    toks = _TOKEN_RE.findall(text.lower())
    return [t for t in toks if t not in _STOP and len(t) > 1]


_sitemap_cache: list[dict] | None = None
_pattern_index: dict[str, dict] | None = None


def _load_sitemap() -> list[dict]:
    global _sitemap_cache, _pattern_index
    if _sitemap_cache is not None:
        return _sitemap_cache
    try:
        with open(_DATA_PATH, "r") as f:
            rows = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load chartmetric sitemap from {_DATA_PATH}: {e}")
        _sitemap_cache = []
        _pattern_index = {}
        return _sitemap_cache

    grouped: dict[str, dict] = {}
    for r in rows:
        pat = (r.get("url_pattern") or "").strip()
        if not pat:
            continue
        bucket = grouped.setdefault(
            pat,
            {
                "url_pattern": pat,
                "entity_type": (r.get("entity_type") or "").strip(),
                "feature_names": [],
                "feature_descs": [],
                "tokens": set(_tokens(pat.replace("/", " ").replace("_", " "))),
            },
        )
        fn = (r.get("feature_name") or "").strip()
        fd = (r.get("feature_description") or "").strip()
        if fn:
            bucket["feature_names"].append(fn)
            bucket["tokens"].update(_tokens(fn))
        if fd:
            bucket["feature_descs"].append(fd)
            bucket["tokens"].update(_tokens(fd))

    _sitemap_cache = list(grouped.values())
    _pattern_index = grouped
    return _sitemap_cache


def _fill_placeholders(url_pattern: str) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return str(PLACEHOLDER_IDS.get(key, "1"))

    out = re.sub(r"\{([a-zA-Z_]+)\}", repl, url_pattern)
    out = re.sub(r":(\w+)", lambda m: str(PLACEHOLDER_IDS.get(m.group(1), "1")), out)
    if not out.startswith("/"):
        out = "/" + out
    return CHARTMETRIC_BASE + out


def infer_chartmetric_url(title: str, description: str = "", min_score: int = 2) -> Optional[str]:
    """Return the best-guess Chartmetric URL for a feature based on its
    title+description, or None if no candidate scores above the threshold."""
    sitemap = _load_sitemap()
    if not sitemap:
        return None

    text_tokens = _tokens(f"{title or ''} {description or ''}")
    if not text_tokens:
        return None

    text_set = set(text_tokens)
    text_freq = defaultdict(int)
    for t in text_tokens:
        text_freq[t] += 1

    best = None
    best_score = 0
    for entry in sitemap:
        overlap = entry["tokens"] & text_set
        if not overlap:
            continue
        score = sum(text_freq[t] for t in overlap)
        depth_bonus = entry["url_pattern"].count("/") * 0.1
        score = score + depth_bonus
        if score > best_score:
            best_score = score
            best = entry

    if not best or best_score < min_score:
        return None

    url = _fill_placeholders(best["url_pattern"])
    fn_preview = best["feature_names"][0] if best["feature_names"] else best["entity_type"]
    logger.info(
        f"[sitemap] Inferred URL for {title!r} -> {url} "
        f"(score={best_score:.1f}, matched feature={fn_preview!r})"
    )
    return url
