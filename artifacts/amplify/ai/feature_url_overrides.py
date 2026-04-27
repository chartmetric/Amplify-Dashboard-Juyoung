import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

logger = logging.getLogger("amplify.feature_url_overrides")

_OVERRIDES_FILE = os.path.join(os.path.dirname(__file__), "..", ".feature_url_overrides.json")
_overrides_lock = threading.Lock()


def _load_from_disk() -> list:
    try:
        if os.path.exists(_OVERRIDES_FILE):
            with open(_OVERRIDES_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                logger.info(f"[url-override] Loaded {len(data)} URL override entries from disk")
                return data
    except Exception as e:
        logger.warning(f"[url-override] Failed to load URL overrides from disk: {e}")
    return []


def _save_to_disk():
    with _overrides_lock:
        try:
            tmp = _OVERRIDES_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(FEATURE_URL_OVERRIDES, f, separators=(",", ":"))
            os.replace(tmp, _OVERRIDES_FILE)
        except Exception as e:
            logger.warning(f"[url-override] Failed to save URL overrides to disk: {e}")


FEATURE_URL_OVERRIDES = _load_from_disk()


_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_title(title: str) -> str:
    if not title:
        return ""
    return _NORM_RE.sub(" ", title.lower()).strip()


def _token_set(title: str) -> set:
    norm = _norm_title(title)
    return {t for t in norm.split(" ") if t and len(t) > 2}


def save_url_override(feature_id: str, feature_title: str, original_url: str, new_url: str, reason: str = ""):
    """Persist a feature URL correction so it survives reloads and so future
    URL inference can learn from it.

    The newest entry for a given (feature_id) supersedes older ones — older
    entries stay in the history file for transparency / few-shot learning."""
    entry = {
        "feature_id": feature_id or "",
        "feature_title": feature_title or "",
        "original_url": original_url or "",
        "new_url": new_url or "",
        "reason": reason or "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    FEATURE_URL_OVERRIDES.insert(0, entry)
    _save_to_disk()
    logger.info(
        f"[url-override] Saved override for {feature_id!r} ({feature_title[:60]!r}): "
        f"{original_url!r} -> {new_url!r}"
    )
    return entry


def get_url_overrides(limit: int | None = None) -> list:
    if limit:
        return FEATURE_URL_OVERRIDES[:limit]
    return list(FEATURE_URL_OVERRIDES)


def get_url_override_for_feature(feature_id: str) -> dict | None:
    """Most recent override entry that targets this feature_id, if any."""
    if not feature_id:
        return None
    for entry in FEATURE_URL_OVERRIDES:
        if entry.get("feature_id") == feature_id and entry.get("new_url"):
            return entry
    return None


def get_url_override_for_title(title: str) -> dict | None:
    """Most recent override entry whose feature_title matches the given
    title — exact (normalized) match first, then a high-overlap token
    match. Returns None if nothing close enough is found."""
    if not title:
        return None
    norm = _norm_title(title)
    if not norm:
        return None

    for entry in FEATURE_URL_OVERRIDES:
        if not entry.get("new_url"):
            continue
        if _norm_title(entry.get("feature_title", "")) == norm:
            return entry

    input_tokens = _token_set(title)
    if len(input_tokens) < 2:
        return None
    best = None
    best_score = 0.0
    for entry in FEATURE_URL_OVERRIDES:
        if not entry.get("new_url"):
            continue
        cand_tokens = _token_set(entry.get("feature_title", ""))
        if not cand_tokens:
            continue
        common = input_tokens & cand_tokens
        if not common:
            continue
        # Jaccard-ish score; require high overlap to short-circuit so
        # we don't accidentally apply an unrelated correction.
        union = input_tokens | cand_tokens
        score = len(common) / max(len(union), 1)
        if score > best_score:
            best_score = score
            best = entry
    if best and best_score >= 0.7:
        return best
    return None


def get_url_override_learning_context(limit: int = 3) -> str:
    """Format the most recent N URL corrections as few-shot text suitable
    for appending to a Claude prompt."""
    if not FEATURE_URL_OVERRIDES:
        return ""

    entries = FEATURE_URL_OVERRIDES[:limit]
    lines = [
        "\nLEARNING FROM PAST FEATURE URL CORRECTIONS (a human marketer fixed these auto-inferred URLs — match this judgment when picking URLs for similar features):"
    ]
    for e in entries:
        lines.append("---")
        lines.append(f"Feature: {e.get('feature_title') or '(no title)'}")
        if e.get("original_url"):
            lines.append(f"AI/auto-inferred URL: {e['original_url']}")
        else:
            lines.append("AI/auto-inferred URL: (none)")
        lines.append(f"Marketer corrected to: {e.get('new_url') or '(cleared)'}")
        lines.append(f"Reason: {e.get('reason') or 'No reason given'}")
    lines.append("---")
    return "\n".join(lines)
