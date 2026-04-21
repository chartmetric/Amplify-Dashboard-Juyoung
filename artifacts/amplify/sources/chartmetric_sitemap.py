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
    "artist_id": "2762",
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


def _path_tokens(url_pattern: str) -> set[str]:
    """Tokens that come from the URL pattern itself (high signal)."""
    cleaned = re.sub(r"\{[^}]+\}", " ", url_pattern)
    cleaned = re.sub(r":\w+", " ", cleaned)
    cleaned = cleaned.replace("/", " ").replace("-", " ").replace("_", " ")
    return set(_tokens(cleaned))


def _load_sitemap() -> list[dict]:
    """One scoring entry per sitemap row, not per URL pattern.

    Grouping all features under the same pattern gave heavily-populated
    patterns (e.g. `/shortlist/...`) an unfair token volume advantage on
    generic words like "artist". Per-row scoring keeps each candidate
    focused."""
    global _sitemap_cache
    if _sitemap_cache is not None:
        return _sitemap_cache
    try:
        with open(_DATA_PATH, "r") as f:
            rows = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load chartmetric sitemap from {_DATA_PATH}: {e}")
        _sitemap_cache = []
        return _sitemap_cache

    entries: list[dict] = []
    for r in rows:
        pat = (r.get("url_pattern") or "").strip()
        if not pat:
            continue
        fn = (r.get("feature_name") or "").strip()
        fd = (r.get("feature_description") or "").strip()
        path_toks = _path_tokens(pat)
        name_toks = set(_tokens(fn))
        desc_toks = set(_tokens(fd))
        entries.append({
            "url_pattern": pat,
            "entity_type": (r.get("entity_type") or "").strip(),
            "feature_name": fn,
            "feature_desc": fd,
            "path_tokens": path_toks,
            "name_tokens": name_toks,
            "desc_tokens": desc_toks,
            "all_tokens": path_toks | name_toks | desc_toks,
        })

    _sitemap_cache = entries
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


_idf_cache: dict[str, float] | None = None


def _idf() -> dict[str, float]:
    """Inverse document frequency per token across all sitemap rows.
    Rare tokens (e.g. "shortlist", "live-events") get higher weight than
    generic ones (e.g. "artist", "page")."""
    global _idf_cache
    if _idf_cache is not None:
        return _idf_cache
    import math
    entries = _load_sitemap()
    n = max(len(entries), 1)
    df: dict[str, int] = defaultdict(int)
    for e in entries:
        for t in e["all_tokens"]:
            df[t] += 1
    _idf_cache = {t: math.log(1 + n / (1 + c)) for t, c in df.items()}
    return _idf_cache


def _bigrams(tokens: list[str]) -> set[str]:
    return {f"{a} {b}" for a, b in zip(tokens, tokens[1:])}


def infer_chartmetric_url(title: str, description: str = "", min_score: float = 2.0) -> Optional[str]:
    """Return the best-guess Chartmetric URL for a feature based on its
    title+description, or None if no candidate scores above the threshold.

    Scoring weights tokens by IDF (rare = more diagnostic), gives a 3x
    boost to tokens appearing in the URL path itself, a 2x boost to
    tokens in the feature name, and a large bigram bonus when a 2-word
    phrase from the input also appears in the URL path or feature name.
    """
    sitemap = _load_sitemap()
    if not sitemap:
        return None

    text_tokens = _tokens(f"{title or ''} {description or ''}")
    if not text_tokens:
        return None

    text_set = set(text_tokens)
    idf = _idf()
    input_bigrams = _bigrams(text_tokens)

    best = None
    best_score = 0.0
    for entry in sitemap:
        overlap = entry["all_tokens"] & text_set
        if not overlap:
            continue

        score = 0.0
        for t in overlap:
            w = idf.get(t, 1.0)
            if t in entry["path_tokens"]:
                score += w * 3.0
            elif t in entry["name_tokens"]:
                score += w * 2.0
            else:
                score += w

        if input_bigrams:
            path_text = entry["url_pattern"].replace("/", " ").replace("-", " ").replace("_", " ").lower()
            name_text = entry["feature_name"].lower()
            for bg in input_bigrams:
                if bg in path_text or bg in name_text:
                    score += 5.0
                    bg_dashed = bg.replace(" ", "-")
                    if bg_dashed in entry["url_pattern"].lower():
                        score += 5.0

        if score > best_score:
            best_score = score
            best = entry

    if not best or best_score < min_score:
        return None

    url = _fill_placeholders(best["url_pattern"])
    logger.info(
        f"[sitemap] Inferred URL for {title!r} -> {url} "
        f"(score={best_score:.1f}, matched feature={best['feature_name']!r})"
    )
    return url
