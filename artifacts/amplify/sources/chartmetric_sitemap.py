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


def _stem(t: str) -> str:
    """Tiny singularizer so plural/singular forms collide.

    Examples: influencers->influencer, playlists->playlist,
    countries->country, demographics->demographic. Conservative:
    leaves words <=3 chars alone and avoids 'ss'/'us' endings.
    """
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not t.endswith("us"):
        return t[:-1]
    return t


def _tokens(text: str) -> list[str]:
    if not text:
        return []
    toks = _TOKEN_RE.findall(text.lower())
    return [_stem(t) for t in toks if t not in _STOP and len(t) > 1]


def _norm_text(text: str) -> str:
    """Stemmed, space-separated form of free text — used for bigram
    substring checks so 'live event' matches 'live events' in the URL."""
    if not text:
        return ""
    return " ".join(_stem(t) for t in _TOKEN_RE.findall(text.lower()))


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
            "path_text_norm": _norm_text(re.sub(r"\{[^}]+\}", " ", pat)),
            "name_text_norm": _norm_text(fn),
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


# Stemmed entity-type words. When the input contains one of these, rows
# whose URL is scoped to that entity get a small bias bonus.
_ENTITY_WORDS = {
    "artist", "track", "album", "playlist", "label", "brand", "festival",
    "songwriter", "curator", "city", "country", "genre", "shortlist",
    "influencer", "video", "sound",
}


def _entities_in(text_set: set[str]) -> set[str]:
    return _ENTITY_WORDS & text_set


_chart_platform_cache: dict[str, str] | None = None


def _chart_platforms() -> dict[str, str]:
    """Map of chart-platform slug (e.g. 'spotify', 'apple-music',
    'genius') -> a sensible default /charts/<platform>/... URL.

    We mine platforms from the sitemap (any row whose URL matches
    /charts/<platform>/...) and then extend with a small set of
    known platforms that are live on chartmetric but not yet in
    the snapshot (e.g. genius).
    """
    global _chart_platform_cache
    if _chart_platform_cache is not None:
        return _chart_platform_cache

    out: dict[str, str] = {}
    for r in _load_sitemap():
        m = re.match(r"^/charts/([^/]+)/", r["url_pattern"])
        if m:
            plat = m.group(1)
            out.setdefault(plat, r["url_pattern"])

    # Platforms live on chartmetric but missing from the snapshot.
    extras = {
        "genius": "/charts/genius/top-tracks",
    }
    for k, v in extras.items():
        out.setdefault(k, v)

    _chart_platform_cache = out
    return out


def _detect_chart_platform_url(text_norm: str) -> Optional[str]:
    """If the text mentions '<platform> chart(s)', return the canonical
    /charts/<platform>/... URL — used as a final override when the
    normal matcher picks a non-charts row but the feature is clearly
    about a charts page."""
    if not text_norm:
        return None
    for plat, default_url in _chart_platforms().items():
        plat_norm = " ".join(_stem(t) for t in plat.split("-") if t)
        if not plat_norm:
            continue
        # Match "<plat> chart" — _norm_text already singularised
        # "charts" -> "chart" via _stem.
        if re.search(r"(?<![a-z0-9])" + re.escape(plat_norm) + r" chart(?![a-z0-9])", text_norm):
            return default_url
    return None


def infer_chartmetric_url(title: str, description: str = "", min_score: float = 2.0) -> Optional[str]:
    """Return the best-guess Chartmetric URL for a feature based on its
    title+description, or None if no candidate scores above the threshold.

    Scoring weights tokens by IDF (rare = more diagnostic), gives a 3x
    boost to tokens appearing in the URL path itself, a 2x boost to
    tokens in the feature name, and a large bigram bonus when a 2-word
    phrase from the input also appears in the URL path or feature name.

    Before scoring, consult the human-in-the-loop URL override store: if
    a marketer previously corrected the URL for an exact-title or
    high-overlap-title match, short-circuit to their corrected URL so we
    never re-infer a URL we already know is wrong for this feature.
    """
    try:
        from ai.feature_url_overrides import get_url_override_for_title
        override = get_url_override_for_title(title or "")
        if override and override.get("new_url"):
            logger.info(
                f"[sitemap] Using human-corrected URL for {title!r} -> {override['new_url']} "
                f"(override from {override.get('timestamp', '?')})"
            )
            return override["new_url"]
    except Exception as e:
        logger.warning(f"[sitemap] URL override lookup failed: {e}")

    sitemap = _load_sitemap()
    if not sitemap:
        return None

    text_tokens = _tokens(f"{title or ''} {description or ''}")
    if not text_tokens:
        return None

    text_set = set(text_tokens)
    idf = _idf()
    input_bigrams = _bigrams(text_tokens)
    text_norm_full = _norm_text(f"{title or ''} {description or ''}")

    best = None
    best_score = 0.0
    for entry in sitemap:
        path_overlap = entry["path_tokens"] & text_set
        name_overlap = entry["name_tokens"] & text_set
        desc_overlap = entry["desc_tokens"] & text_set
        if not (path_overlap or name_overlap or desc_overlap):
            continue

        # Topical floor: description tokens only contribute fully when
        # the URL path itself agrees with the topic. If only the name
        # matched (e.g. row name "Full Page" — generic), description
        # tokens get heavily discounted so a bloated description can't
        # win on volume alone. If nothing topical matched, near-zero.
        score = 0.0
        for t in path_overlap:
            score += idf.get(t, 1.0) * 3.0
        for t in name_overlap - path_overlap:
            score += idf.get(t, 1.0) * 2.0
        # Cap desc contribution to the TOP 5 matched tokens (by IDF)
        # so a row with a bloated marketing-brochure description (e.g.
        # the /artists "Full Page" row has 130+ desc tokens) can't
        # accumulate a runaway score on token volume alone.
        if path_overlap:
            desc_extras = sorted(
                (idf.get(t, 1.0) for t in desc_overlap - path_overlap - name_overlap),
                reverse=True,
            )[:5]
            score += sum(desc_extras)
        elif name_overlap:
            desc_extras = sorted(
                (idf.get(t, 1.0) for t in desc_overlap - name_overlap),
                reverse=True,
            )[:5]
            score += sum(desc_extras) * 0.3
        else:
            desc_extras = sorted(
                (idf.get(t, 1.0) for t in desc_overlap),
                reverse=True,
            )[:5]
            score += sum(desc_extras) * 0.2

        # Landing-page jackpot: if the URL is a single, non-placeholder
        # segment (e.g. /influencers, /talent-search, /shortlists) and
        # that one path token is reasonably specific *and* present in
        # the input, this row is almost certainly the right answer —
        # beating deeply-nested rows that only match the same word as
        # a side topic.
        if (
            len(entry["path_tokens"]) == 1
            and "{" not in entry["url_pattern"]
            and ":" not in entry["url_pattern"]
        ):
            (only,) = tuple(entry["path_tokens"])
            if only in path_overlap and idf.get(only, 0) >= 2.5:
                score += idf[only] * 5.0

        # Entity-type bias: when the input mentions an entity word
        # (artist, track, album, shortlist, ...), prefer URL patterns
        # that are scoped to that entity. This disambiguates rows
        # whose feature_name is shared across entity types — e.g.
        # "Stats & Trends" exists under /artist, /album and /playlist.
        for ent in _entities_in(text_set):
            pat = entry["url_pattern"]
            if pat == f"/{ent}s" or pat == f"/{ent}" or pat.startswith(f"/{ent}/") or pat.startswith(f"/{ent}s/"):
                score += 5.0
                break

        # Name-substring bonus: when a multi-token feature_name appears
        # verbatim inside the input text, this row is essentially named
        # in the input — give it a strong boost so e.g. a row literally
        # named "Stats & Trends" wins over an unrelated /industry row
        # that just happens to share a single rare token.
        if entry["name_text_norm"] and " " in entry["name_text_norm"]:
            if entry["name_text_norm"] in text_norm_full:
                ntoks = entry["name_text_norm"].count(" ") + 1
                score += 8.0 * ntoks

        if input_bigrams:
            path_text = entry["path_text_norm"]
            name_text = entry["name_text_norm"]
            for bg in input_bigrams:
                if bg in path_text or bg in name_text:
                    score += 5.0
                    bg_dashed = bg.replace(" ", "-")
                    if bg_dashed in entry["url_pattern"].lower():
                        score += 5.0

        if score > best_score:
            best_score = score
            best = entry

    # Final override: if the input clearly mentions a "<platform> chart(s)"
    # but the best match isn't a /charts/<platform>/... row, the matcher
    # has gotten confused (likely because a chart platform is missing
    # from the snapshot or because a description mentions the platform
    # in passing on an unrelated row). Trust the explicit phrasing.
    chart_url = _detect_chart_platform_url(text_norm_full)
    if chart_url:
        best_pat = best["url_pattern"] if best else ""
        target_prefix = "/".join(chart_url.split("/")[:3]) + "/"  # '/charts/<plat>/'
        if not best_pat.startswith(target_prefix):
            url = _fill_placeholders(chart_url)
            logger.info(
                f"[sitemap] Chart-platform override for {title!r} -> {url} "
                f"(was best={best_pat or 'None'} score={best_score:.1f})"
            )
            return url

    if not best or best_score < min_score:
        return None

    url = _fill_placeholders(best["url_pattern"])
    logger.info(
        f"[sitemap] Inferred URL for {title!r} -> {url} "
        f"(score={best_score:.1f}, matched feature={best['feature_name']!r})"
    )
    return url
