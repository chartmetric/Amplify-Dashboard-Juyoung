import os
import sys
import signal
import logging
import html
import json
import re as re_module
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, jsonify, render_template, request
import config
from sources.asana_source import AsanaSource
from sources.slack_source import SlackSource
from sources.manual_source import ManualSource
from ai.classifier import (
    classify_features_batch, classify_feature, set_manual_override,
    get_manual_overrides, remove_manual_override, apply_manual_overrides,
    get_cached_classification, get_all_cached_classifications, clear_cache,
    CLASSIFICATION_CACHE,
    quick_classify, get_keyword_list, add_keyword, remove_keyword,
    record_keyword_override, get_classification_tier_stats,
)
from ai.pre_filter import pre_filter_batch  # kept for backward compat, not used in main pipeline
from ai.generator import generate_for_channel, generate_all_channels
from ai.few_shot_examples import FEW_SHOT_EXAMPLES
from ai.feedback_store import save_feedback, get_feedback_history, get_all_feedback, clear_feedback
from ai.publish_store import mark_published, save_image as save_publish_image, get_image as get_publish_image, remove_image as remove_publish_image, get_feature_state, get_all_published, save_video as save_publish_video, get_video_path, get_video_thumb_path, list_feature_videos
from ai.classification_overrides import save_override as save_classification_override, get_overrides as get_classification_overrides
from ai.feature_sets import save_set as save_feature_set, get_sets as get_feature_sets, delete_set as delete_feature_set
from datetime import datetime, timezone

_app_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_app_dir, "templates"), static_folder=os.path.join(_app_dir, "static"))
app.secret_key = config.SESSION_SECRET
app.config["MAX_CONTENT_LENGTH"] = 75 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("amplify")

SOURCE_REGISTRY = {
    "asana": AsanaSource(project_gid="1213445772342530"),
    "slack": SlackSource(channel_id="C014BMSCGS2"),
    "manual": ManualSource(),
}

_batch_state = {
    "running": False,
    "total": 0,
    "classified": 0,
    "in_progress": False,
}
_batch_lock = threading.Lock()

_metrics = {
    "generate_count": 0,
    "approve_count": 0,
    "edit_count": 0,
    "daily_generates": {},
}
_metrics_lock = threading.Lock()


def _inc_generate(count=1):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _metrics_lock:
        _metrics["generate_count"] += count
        _metrics["daily_generates"][today] = _metrics["daily_generates"].get(today, 0) + count


def _inc_approve(was_edited=False):
    with _metrics_lock:
        _metrics["approve_count"] += 1
        if was_edited:
            _metrics["edit_count"] += 1


@app.route("/")
def dashboard():
    """Dashboard home page.

    Category: System
    Response: HTML dashboard page.
    """
    return render_template("dashboard.html")


@app.route("/api/health")
def health():
    """Health check with API key status, channel count, and example counts.

    Category: System

    Response:
    {
        "status": "ok",
        "api_key_configured": true,
        "channels_loaded": 7,
        "examples_loaded": {"twitter": 3, ...},
        "keys": {"anthropic": true, "asana": true, "slack": true}
    }
    """
    from ai.channel_configs import CHANNEL_CONFIGS
    examples_loaded = {k: len(v) for k, v in FEW_SHOT_EXAMPLES.items()}
    return jsonify({
        "status": "ok",
        "api_key_configured": bool(config.ANTHROPIC_API_KEY),
        "channels_loaded": len(CHANNEL_CONFIGS),
        "examples_loaded": examples_loaded,
        "keys": {
            "anthropic": bool(config.ANTHROPIC_API_KEY),
            "asana": bool(config.ASANA_ACCESS_TOKEN),
            "slack": bool(config.SLACK_BOT_TOKEN),
            "resend": bool(config.RESEND_API_KEY),
        },
    })


@app.route("/api/sources")
def list_sources():
    """List available data sources.

    Category: Sources

    Response: ["asana", "slack", "manual"]
    """
    return jsonify(list(SOURCE_REGISTRY.keys()))


@app.route("/api/sources/asana/features")
def asana_list():
    """List all features from Asana projects.

    Category: Sources

    Response: Array of feature objects with id, title, description, date, section, custom fields.
    """
    source = SOURCE_REGISTRY["asana"]
    try:
        features = source.list_recent_features()
        return jsonify(features)
    except Exception as e:
        logger.error(f"Asana list error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/asana/features/<feature_id>")
def asana_detail(feature_id):
    """Get detailed context for a single Asana feature by task GID.

    Category: Sources

    Response: Feature context with title, description, comments, custom fields, permalink.
    """
    source = SOURCE_REGISTRY["asana"]
    try:
        ctx = source.get_feature_context(feature_id)
        return jsonify(ctx.to_dict())
    except Exception as e:
        logger.error(f"Asana detail error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/slack/features")
def slack_list():
    """List recent feature releases from Slack channel.

    Category: Sources

    Response: Array of released features with reactions and timestamps.
    """
    source = SOURCE_REGISTRY["slack"]
    try:
        features = source.list_recent_features()
        return jsonify(features)
    except Exception as e:
        logger.error(f"Slack list error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/slack/features/<feature_id>")
def slack_detail(feature_id):
    """Get detailed context for a Slack feature by message timestamp.

    Category: Sources

    Response: Feature context with title, description, reactions.
    """
    source = SOURCE_REGISTRY["slack"]
    try:
        ctx = source.get_feature_context(feature_id)
        return jsonify(ctx.to_dict())
    except Exception as e:
        logger.error(f"Slack detail error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/manual/feature", methods=["POST"])
def manual_create():
    """Create a manual feature entry for classification/generation.

    Category: Sources

    Request Body:
    {
        "title": "Feature name",
        "description": "Feature description"
    }

    Response: Feature context object.
    """
    data = request.get_json() or {}
    title = data.get("title", "")
    description = data.get("description", "")

    if not title:
        return jsonify({"error": "title is required"}), 400

    source = SOURCE_REGISTRY["manual"]
    ctx = source.get_feature_context(title=title, description=description)
    return jsonify(ctx.to_dict())


@app.route("/api/features/manual", methods=["POST"])
def manual_feature_add():
    """Add a manual feature, classify it, and return it in the same format as /api/features/classified.

    Category: Sources

    Request Body:
    {
        "title": "Feature name",
        "description": "Feature description",
        "category": "new_feature"  (optional)
    }

    Response: {"feature": {...classified feature object...}}
    """
    data = request.get_json() or {}
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    category = data.get("category", "new_feature")

    if not title:
        return jsonify({"error": "title is required"}), 400
    if not description:
        return jsonify({"error": "description is required"}), 400

    import uuid
    feature_id = "manual-" + uuid.uuid4().hex[:8]

    feature = {
        "id": feature_id,
        "title": title,
        "description": description,
        "source": "manual",
        "release_status": True,
    }

    try:
        cl = classify_feature(feature)
        if cl:
            if category and category != cl.get("category"):
                cl["category"] = category
            feature["classification"] = cl
        else:
            feature["classification"] = {
                "importance_score": 3,
                "category": category,
                "recommended_channels": ["twitter", "linkedin", "inapp"],
                "marketing_summary": description[:200],
                "classification_method": "manual",
            }
    except Exception as e:
        logger.warning("Classification failed for manual feature: %s", e)
        feature["classification"] = {
            "importance_score": 3,
            "category": category,
            "recommended_channels": ["twitter", "linkedin", "inapp"],
            "marketing_summary": description[:200],
            "classification_method": "manual_fallback",
        }

    return jsonify({"feature": feature})


@app.route("/api/features/from-url", methods=["POST"])
def feature_from_url():
    """Extract a feature from a pasted URL (Slack, Asana, GitHub) or plain text.

    Category: Sources

    Request Body: {"url": "https://..."} or {"url": "plain text title"}
    Response: {"source": "slack"|"asana"|"github"|"manual", "feature": {...}}
    """
    data = request.get_json() or {}
    raw_input = (data.get("url") or "").strip()
    if not raw_input:
        return jsonify({"error": "url or text is required"}), 400

    inputs = [line.strip() for line in raw_input.replace(",", "\n").split("\n") if line.strip()]
    results = []

    for item in inputs:
        try:
            result = _extract_feature_from_input(item)
            results.append(result)
        except Exception as e:
            logger.error(f"from-url extraction error for '{item[:80]}': {e}")
            results.append({"source": "error", "error": str(e), "input": item[:200]})

    if len(results) == 1:
        return jsonify(results[0])
    return jsonify({"results": results, "total": len(results)})


def _extract_feature_from_input(item: str) -> dict:

    slack_match = re_module.search(r"(?:https?://)?[\w-]+\.slack\.com/archives/(C[\w]+)/p(\d+)", item)
    if slack_match:
        channel_id = slack_match.group(1)
        raw_ts = slack_match.group(2)
        msg_ts = raw_ts[:10] + "." + raw_ts[10:]
        return _fetch_feature_from_slack(channel_id, msg_ts)

    asana_match = re_module.search(r"app\.asana\.com/\d+/(\d+)/(\d+)", item)
    if not asana_match:
        asana_match = re_module.search(r"app\.asana\.com/[^/]+/task/(\d+)", item)
    if asana_match:
        task_gid = asana_match.group(asana_match.lastindex)
        return _fetch_feature_from_asana(task_gid)

    github_match = re_module.search(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)", item)
    if github_match:
        org, repo, pr_num = github_match.group(1), github_match.group(2), github_match.group(3)
        return _fetch_feature_from_github(org, repo, pr_num, item)

    if re_module.match(r"https?://", item):
        return {
            "source": "manual",
            "feature": {
                "id": f"manual-{__import__('uuid').uuid4().hex[:12]}",
                "title": item,
                "description": "",
                "source": "manual",
                "released": False,
                "asana_linked": False,
            },
        }

    return {
        "source": "manual",
        "feature": {
            "id": f"manual-{__import__('uuid').uuid4().hex[:12]}",
            "title": item,
            "description": "",
            "source": "manual",
            "released": False,
            "asana_linked": False,
        },
    }


def _fetch_feature_from_slack(channel_id: str, msg_ts: str) -> dict:
    slack_source = SOURCE_REGISTRY["slack"]
    client = slack_source._get_client()

    result = client.conversations_history(
        channel=channel_id,
        oldest=msg_ts,
        latest=msg_ts,
        inclusive=True,
        limit=1,
    )
    messages = result.get("messages", [])
    if not messages:
        raise ValueError("Slack message not found")

    msg = messages[0]
    from sources.slack_source import _clean_slack_text, _extract_reactions, _extract_asana_task_ids, _extract_github_urls

    raw_text = msg.get("text", "")
    text = _clean_slack_text(raw_text)
    total_reactions, reactions = _extract_reactions(msg)
    asana_ids = _extract_asana_task_ids(raw_text)
    github_urls = _extract_github_urls(raw_text)

    thread_text = ""
    thread_asana_ids = []
    thread_github_urls = []
    if msg.get("reply_count", 0) > 0:
        try:
            thread = client.conversations_replies(channel=channel_id, ts=msg_ts)
            for reply in thread.get("messages", [])[1:]:
                reply_text = reply.get("text", "")
                thread_text += "\n" + _clean_slack_text(reply_text)
                thread_asana_ids.extend(_extract_asana_task_ids(reply_text))
                thread_github_urls.extend(_extract_github_urls(reply_text))
        except Exception:
            pass

    all_asana_ids = list(dict.fromkeys(asana_ids + thread_asana_ids))
    all_github = list(dict.fromkeys(github_urls + thread_github_urls))

    title_lines = [l.strip() for l in text.split("\n") if l.strip()]
    title = title_lines[0][:200] if title_lines else "Slack message"
    for line in title_lines:
        if line.startswith(("\u2022", "-", "*")) or re_module.match(r"(PE|Devin|FE|BE):", line):
            title = re_module.sub(r"^[\u2022\-\*]\s*", "", line)
            title = re_module.sub(r"^(PE|Devin|FE|BE):\s*", "", title).strip()
            break

    feature = {
        "id": f"manual-slack-{msg_ts.replace('.', '')}",
        "title": title[:300],
        "description": text,
        "source": "slack_only",
        "released": True,
        "slack_url": f"https://chartmetric.slack.com/archives/{channel_id}/p{msg_ts.replace('.', '')}",
        "asana_url": None,
        "github_url": all_github[0] if all_github else None,
        "asana_linked": False,
        "total_reactions": total_reactions,
        "reactions_breakdown": reactions,
        "release_date": msg.get("ts", ""),
        "release_version": {"fe": None, "be": None},
        "source_prefix": None,
    }

    if all_asana_ids:
        asana_source = SOURCE_REGISTRY["asana"]
        task_data = asana_source._fetch_task_by_id(all_asana_ids[0])
        if task_data:
            feature["asana_linked"] = True
            feature["source"] = "slack+asana"
            feature["asana_url"] = task_data.get("asana_url")
            feature["asana_task_id"] = all_asana_ids[0]
            if task_data.get("description"):
                feature["description"] = task_data["description"]
            for field in ["engineer", "assignee", "team", "task_type", "urgency_score", "planner"]:
                if task_data.get(field):
                    feature[field] = task_data[field]

    return {"source": "slack", "feature": feature, "linked_asana": feature["asana_linked"], "linked_github": bool(all_github)}


def _fetch_feature_from_asana(task_gid: str) -> dict:
    asana_source = SOURCE_REGISTRY["asana"]
    task_data = asana_source._fetch_task_by_id(task_gid)
    if not task_data:
        raise ValueError(f"Asana task {task_gid} not found")

    import asana as asana_lib
    client = asana_source._get_client()
    tasks_api = asana_lib.TasksApi(client)
    task_raw = tasks_api.get_task(task_gid, {"opt_fields": "name,permalink_url"})
    t = task_raw.to_dict() if hasattr(task_raw, "to_dict") else task_raw

    feature = {
        "id": task_gid,
        "title": t.get("name", "Asana Task"),
        "description": task_data.get("description", ""),
        "source": "asana_only",
        "released": False,
        "asana_linked": True,
        "asana_url": task_data.get("asana_url") or t.get("permalink_url"),
        "slack_url": None,
        "github_url": None,
        "asana_task_id": task_gid,
        "release_version": {"fe": None, "be": None},
        "release_date": "",
        "source_prefix": None,
        "total_reactions": 0,
        "reactions_breakdown": {},
    }
    for field in ["engineer", "assignee", "team", "task_type", "urgency_score", "planner", "subtasks", "comments", "project_info"]:
        if task_data.get(field):
            feature[field] = task_data[field]

    return {"source": "asana", "feature": feature}


def _fetch_feature_from_github(org: str, repo: str, pr_num: str, url: str) -> dict:
    feature = {
        "id": f"github-{org}-{repo}-{pr_num}",
        "title": f"{repo} PR #{pr_num}",
        "description": f"GitHub Pull Request: {url}",
        "source": "manual",
        "released": False,
        "asana_linked": False,
        "github_url": url,
        "slack_url": None,
        "asana_url": None,
        "release_version": {"fe": None, "be": None},
        "release_date": "",
        "source_prefix": f"#{pr_num}",
        "total_reactions": 0,
        "reactions_breakdown": {},
    }
    try:
        import requests as req_lib
        resp = req_lib.get(f"https://api.github.com/repos/{org}/{repo}/pulls/{pr_num}",
                          headers={"Accept": "application/vnd.github.v3+json"}, timeout=10)
        if resp.status_code == 200:
            pr_data = resp.json()
            feature["title"] = pr_data.get("title", feature["title"])
            feature["description"] = pr_data.get("body", "") or ""
    except Exception as e:
        logger.warning(f"GitHub API fetch failed for {org}/{repo}#{pr_num}: {e}")

    return {"source": "github", "feature": feature}


@app.route("/api/features/<source_type>")
def unified_list(source_type):
    """List features from a specific source (asana, slack, manual).

    Category: Sources

    Response: Array of feature objects from the specified source.
    """
    if source_type not in SOURCE_REGISTRY:
        return jsonify({"error": f"Unknown source: {source_type}"}), 404
    source = SOURCE_REGISTRY[source_type]
    try:
        features = source.list_recent_features()
        return jsonify(features)
    except Exception as e:
        logger.error(f"{source_type} list error: {e}")
        return jsonify({"error": str(e)}), 500


_pipeline_cache = {}
_PIPELINE_TTL = 120
_FEATURES_CACHE_DIR = os.path.dirname(__file__)
_pipeline_refresh_lock = threading.Lock()
_pipeline_refreshing_keys = set()
_disk_write_lock = threading.Lock()


def _disk_cache_path(days: int) -> str:
    return os.path.join(_FEATURES_CACHE_DIR, f".features_cache_days{days}.json")


def _load_features_from_disk(days: int = 30) -> dict | None:
    path = _disk_cache_path(days)
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.load(f)
            if data.get("features") and data.get("days") == days:
                logger.info(f"[pipeline] Loaded {len(data['features'])} features from disk cache (days={days})")
                return data
    except Exception as e:
        logger.warning(f"[pipeline] Failed to load features cache from disk: {e}")
    return None


def _save_features_to_disk(features: list, debug_info: dict, timestamp: float, days: int = 30):
    import uuid
    path = _disk_cache_path(days)
    with _disk_write_lock:
        try:
            tmp = path + f".{uuid.uuid4().hex[:8]}.tmp"
            with open(tmp, "w") as f:
                json.dump({"features": features, "debug": debug_info, "timestamp": timestamp, "days": days}, f, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[pipeline] Failed to save features cache to disk: {e}")


def _run_pipeline_fetch(days: int = 30):
    slack_source = SOURCE_REGISTRY["slack"]
    asana_source = SOURCE_REGISTRY["asana"]

    logger.info(f"[pipeline] Starting Slack-first pipeline (days={days})")

    slack_result = slack_source.extract_features_from_channel(days=days)
    features = slack_result["features"]
    debug_info = slack_result["stats"]
    debug_info["skipped"] = slack_result["skipped"]
    debug_info["parse_errors"] = slack_result["parse_errors"]
    debug_info["asana_matches"] = {"url": 0, "search": 0, "none": 0}

    logger.info(f"[pipeline] Extracted {len(features)} features from Slack, enriching with Asana...")

    def _enrich_one(f):
        try:
            asana_source.enrich_feature(f)
            return f.get("asana_match_method", "none")
        except Exception as e:
            logger.warning(f"Asana enrichment failed for '{f.get('title', '')[:50]}': {e}")
            f["asana_linked"] = False
            f["asana_match_method"] = "error"
            return "none"

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as pool:
        methods = list(pool.map(_enrich_one, features))
    for m in methods:
        debug_info["asana_matches"][m] = debug_info["asana_matches"].get(m, 0) + 1

    announced_ids = set()
    for f in features:
        tid = f.get("asana_task_id")
        if tid:
            announced_ids.add(tid)

    try:
        unannounced = asana_source.list_unannounced_tasks(days=days, announced_task_ids=announced_ids)
        debug_info["asana_only_count"] = len(unannounced)
        features.extend(unannounced)
    except Exception as e:
        logger.warning(f"Asana unannounced scan failed: {e}")
        debug_info["asana_only_count"] = 0

    debug_info["total_features_final"] = len(features)
    now = time.time()
    logger.info(f"[pipeline] Pipeline complete: {len(features)} total features ({debug_info['asana_matches']})")

    cache_key = f"days_{days}"
    _pipeline_cache[cache_key] = {
        "features": features,
        "debug": debug_info,
        "timestamp": now,
    }
    _save_features_to_disk(features, debug_info, now, days=days)

    return {"features": features, "debug": debug_info}


def _trigger_background_refresh(days: int = 30):
    cache_key = f"days_{days}"
    with _pipeline_refresh_lock:
        if cache_key in _pipeline_refreshing_keys:
            return
        _pipeline_refreshing_keys.add(cache_key)

    def _do_refresh():
        try:
            _run_pipeline_fetch(days=days)
        except Exception as e:
            logger.error(f"[pipeline] Background refresh failed: {e}")
        finally:
            with _pipeline_refresh_lock:
                _pipeline_refreshing_keys.discard(cache_key)

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()


def _get_slack_first_features(days: int = 30, force_refresh: bool = False) -> dict:
    now = time.time()
    cache_key = f"days_{days}"

    cached = _pipeline_cache.get(cache_key)
    if not force_refresh and cached is not None and (now - cached["timestamp"]) < _PIPELINE_TTL:
        return {"features": cached["features"], "debug": cached["debug"]}

    if not force_refresh:
        if cached is None:
            disk = _load_features_from_disk(days=days)
            if disk is not None:
                _pipeline_cache[cache_key] = {
                    "features": disk["features"],
                    "debug": disk.get("debug", {}),
                    "timestamp": disk.get("timestamp", now),
                }
                _trigger_background_refresh(days=days)
                return {"features": disk["features"], "debug": disk.get("debug", {})}
        else:
            _trigger_background_refresh(days=days)
            return {"features": cached["features"], "debug": cached["debug"]}

    return _run_pipeline_fetch(days=days)


def _get_enriched_features():
    result = _get_slack_first_features()
    return result["features"]


@app.route("/api/features/enriched")
def enriched_features():
    """Fetch all features from Asana cross-referenced with Slack release data.

    Category: Sources

    Response: Array of enriched feature objects with release_status, release_date, reactions.
    """
    try:
        enriched = _get_enriched_features()
        return jsonify(enriched)
    except Exception as e:
        logger.error(f"Enriched endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/features/classify", methods=["POST"])
def classify_features_endpoint():
    """Classify a custom list of features using Claude AI.

    Category: Sources

    Request Body:
    {
        "features": [
            {"id": "...", "title": "...", "description": "..."}
        ]
    }

    Query Params: ?min_importance=N (optional filter)

    Response:
    {
        "classified_features": [
            {"title": "...", "classification": {"importance_score": 4, "category": "new_feature", ...}}
        ]
    }
    """
    data = request.get_json() or {}
    features = data.get("features", [])
    if not isinstance(features, list) or not features:
        return jsonify({"error": "No features provided. Send {\"features\": [...]} with a list of feature objects"}), 400
    if not all(isinstance(f, dict) for f in features):
        return jsonify({"error": "Each feature must be a JSON object with at least id, title, and description"}), 400

    try:
        classified = classify_features_batch(features)
    except Exception as e:
        logger.error(f"Classification error: {e}")
        return jsonify({"error": str(e)}), 500

    min_importance = request.args.get("min_importance", type=int)
    if min_importance is not None:
        classified = [
            f for f in classified
            if f.get("classification", {}).get("importance_score", 0) >= min_importance
        ]

    return jsonify({"classified_features": classified})


@app.route("/api/features/all-raw")
def all_features_raw_endpoint():
    """Return all features without pre-filter, as unclassified cards.

    Category: Sources

    Response: {"features": [...], "total": N, "pre_filter_applied": false}
    """
    try:
        enriched = _get_enriched_features()
    except Exception as e:
        logger.error(f"all_features_raw endpoint - enrichment error: {e}")
        return jsonify({"error": f"Feature enrichment failed: {e}"}), 500

    total_from_asana = len(enriched)
    logger.info(f"[all_features_raw] Returning all {total_from_asana} features unclassified")
    features_out = []
    for f in enriched:
        features_out.append({
            **f,
            "classification": {
                "importance_score": 0,
                "importance_score_reason": "Not classified (pre-filter bypassed)",
                "category": "unknown",
                "categories": ["unknown"],
                "recommended_channels": [],
                "marketing_summary": "",
                "target_audience": [],
                "unclassified": True,
            },
        })
    return jsonify({
        "features": features_out,
        "total": total_from_asana,
        "pre_filter_applied": False,
        "pre_filter_skipped_count": 0,
        "sent_to_claude_count": 0,
    })




@app.route("/api/features/classified")
def classified_features():
    """Fetch and auto-classify all enriched features, sorted by importance.

    Category: Sources

    Query Params: ?limit=20&min_importance=N&released_only=true&pre_filter=false

    Response:
    {
        "classified_features": [...],
        "total_enriched": 356,
        "classified_count": 20,
        "filtered": 15
    }
    """
    limit = request.args.get("limit", default=20, type=int)
    released_only = request.args.get("released_only", default="false").lower() == "true"
    use_pre_filter = request.args.get("pre_filter", default="true").lower() != "false"

    try:
        enriched = _get_enriched_features()
    except Exception as e:
        logger.error(f"Classified endpoint - enrichment error: {e}")
        return jsonify({"error": f"Feature enrichment failed: {e}"}), 500

    total_enriched = len(enriched)
    logger.info(f"[classified] Step 1 – Asana fetch: {total_enriched} total features")

    if released_only:
        before = len(enriched)
        enriched = [f for f in enriched if f.get("release_status")]
        logger.info(f"[classified] Step 2 – released_only filter: {before} → {len(enriched)} (excluded {before - len(enriched)})")

    before_limit = len(enriched)
    enriched = enriched[:limit]
    logger.info(f"[classified] Step 3 – limit={limit}: {before_limit} → {len(enriched)}")

    if use_pre_filter:
        logger.info(f"[classified] Step 4 – pre-filter: evaluating {len(enriched)} features")
        filter_result = pre_filter_batch(enriched)
        to_classify = filter_result["to_classify"]
        skipped = filter_result["skipped"]
        logger.info(f"[classified] Step 4 result: {len(to_classify)} to classify, {len(skipped)} skipped (condition: skip_classification=True)")
    else:
        logger.info(f"[classified] Step 4 – pre-filter BYPASSED (pre_filter=false), sending all {len(enriched)} to classify")
        to_classify = enriched
        skipped = []

    classified = []
    if to_classify:
        logger.info(f"[classified] Step 5 – Claude classification: sending {len(to_classify)} features")
        try:
            classified = classify_features_batch(to_classify)
            logger.info(f"[classified] Step 5 result: {len(classified)} classified")
        except Exception as e:
            logger.error(f"Classified endpoint - classification error: {e}")
            return jsonify({"error": f"Classification failed: {e}"}), 500

    all_features = classified + skipped
    all_features = apply_manual_overrides(all_features)

    sort_by = request.args.get("sort_by", "importance")
    if sort_by == "recency":
        all_features.sort(key=lambda f: f.get("release_date") or "", reverse=True)
    else:
        all_features.sort(key=lambda f: f.get("classification", {}).get("importance_score", 0), reverse=True)

    total = len(all_features)
    min_importance = request.args.get("min_importance", type=int)
    if min_importance is not None:
        before_imp = len(all_features)
        all_features = [
            f for f in all_features
            if f.get("classification", {}).get("importance_score", 0) >= min_importance
        ]
        logger.info(f"[classified] Step 6 – min_importance={min_importance}: {before_imp} → {len(all_features)} (excluded {before_imp - len(all_features)})")

    logger.info(f"[classified] Final: showing {len(all_features)} of {total_enriched} total features")

    return jsonify({
        "classified_features": all_features,
        "total_enriched": total_enriched,
        "classified_count": total,
        "filtered": len(all_features),
        "pre_filtered_skipped": len(skipped),
        "sent_to_claude": len(to_classify),
        "limit_applied": limit,
        "released_only": released_only,
        "pre_filter_applied": use_pre_filter,
        "min_importance_applied": min_importance,
        "manual_overrides_applied": len(get_manual_overrides()),
    })


@app.route("/api/features/all")
def all_features_unclassified():
    """Fetch all features using Slack-first pipeline, with cached classifications.

    Category: Sources

    Query Params: ?days=30&limit=100&refresh=false

    Response: {"features": [...], "total": N}
    """
    days = request.args.get("days", default=30, type=int)
    force_refresh = request.args.get("refresh", default="false").lower() == "true"
    try:
        result = _get_slack_first_features(days=days, force_refresh=force_refresh)
        features = result["features"]
    except Exception as e:
        logger.error(f"All features endpoint error: {e}")
        return jsonify({"error": f"Feature pipeline failed: {e}"}), 500

    cache = get_all_cached_classifications()
    overrides = get_manual_overrides()
    for f in features:
        fid = f.get("id", "")
        cl = cache.get(fid)
        if cl is not None:
            f["classification"] = {**cl}
        if fid in overrides:
            if "classification" not in f:
                f["classification"] = {}
            f["classification"].update(overrides[fid])
            f["classification"]["manual_override"] = True

    cache_key = f"days_{days}"
    cached_entry = _pipeline_cache.get(cache_key)
    last_refreshed = cached_entry["timestamp"] if cached_entry else time.time()

    return jsonify({
        "features": features,
        "total": len(features),
        "last_refreshed": last_refreshed,
    })


@app.route("/api/features/<feature_id>/classify", methods=["POST"])
def classify_single_feature(feature_id):
    """Classify a single feature on demand, using cache if available.

    Category: Sources

    Response: {"classification": {...}, "cached": bool}
    """
    cached = get_cached_classification(feature_id)
    if cached is not None:
        return jsonify({"classification": cached, "cached": True})

    try:
        features = _get_enriched_features()
    except Exception as e:
        return jsonify({"error": f"Feature pipeline failed: {e}"}), 500

    feature = next((f for f in features if f.get("id") == feature_id), None)
    if feature is None:
        return jsonify({"error": f"Feature {feature_id} not found"}), 404

    try:
        cl = classify_feature(feature)
    except Exception as e:
        return jsonify({"error": f"Classification failed: {e}"}), 500

    overrides = get_manual_overrides()
    if feature_id in overrides:
        cl = {**cl, **overrides[feature_id], "manual_override": True}

    return jsonify({"classification": cl, "cached": False})


def _run_batch_classification(features: list[dict]):
    with _batch_lock:
        _batch_state["running"] = True
        _batch_state["total"] = len(features)
        _batch_state["classified"] = 0
        _batch_state["in_progress"] = True

    already_cached = []
    to_classify = []
    for f in features:
        fid = f.get("id", "")
        if fid and fid in CLASSIFICATION_CACHE:
            already_cached.append(f)
        else:
            to_classify.append(f)

    with _batch_lock:
        _batch_state["classified"] = len(already_cached)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _do_classify(feature):
        cl = classify_feature(feature)
        fid = feature.get("id", "")
        overrides = get_manual_overrides()
        if fid in overrides:
            cl = {**cl, **overrides[fid], "manual_override": True}
            CLASSIFICATION_CACHE[fid] = cl
        with _batch_lock:
            _batch_state["classified"] += 1
        return fid, cl

    max_workers = 2
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_do_classify, f) for f in to_classify]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Batch classification error: {e}")
                with _batch_lock:
                    _batch_state["classified"] += 1

    from ai.classifier import _save_cache_to_disk
    _save_cache_to_disk()

    with _batch_lock:
        _batch_state["running"] = False
        _batch_state["in_progress"] = False


@app.route("/api/features/classify-batch-async", methods=["POST"])
def classify_batch_async():
    """Start background classification of all features. Returns immediately.

    Category: Sources

    Response: {"status": "started"|"already_running"|"complete", "total": N, "classified": N}
    """
    with _batch_lock:
        if _batch_state["in_progress"]:
            return jsonify({
                "status": "already_running",
                "total": _batch_state["total"],
                "classified": _batch_state["classified"],
            })

    days = request.args.get("days", default=30, type=int)
    try:
        result = _get_slack_first_features(days=days)
        features = result["features"]
    except Exception as e:
        return jsonify({"error": f"Feature pipeline failed: {e}"}), 500

    already_cached = [f for f in features if f.get("id") in CLASSIFICATION_CACHE]
    if len(already_cached) == len(features):
        return jsonify({
            "status": "complete",
            "total": len(features),
            "classified": len(features),
        })

    t = threading.Thread(target=_run_batch_classification, args=(features,), daemon=True)
    t.start()

    return jsonify({
        "status": "started",
        "total": len(features),
        "classified": len(already_cached),
    })


@app.route("/api/classifications/status")
def classifications_status():
    """Get current classification progress status.

    Category: Sources

    Response: {"total": N, "classified": N, "pending": N, "in_progress": bool}
    """
    with _batch_lock:
        total = _batch_state["total"]
        classified = _batch_state["classified"]
        in_progress = _batch_state["in_progress"]

    if total == 0:
        cache_count = len(CLASSIFICATION_CACHE)
        return jsonify({
            "total": cache_count,
            "classified": cache_count,
            "pending": 0,
            "in_progress": False,
        })

    return jsonify({
        "total": total,
        "classified": classified,
        "pending": max(0, total - classified),
        "in_progress": in_progress,
    })


@app.route("/api/classifications/cache")
def classifications_cache():
    """Return full classification cache.

    Category: Sources

    Response: {"cache": {...}, "count": N}
    """
    cache = get_all_cached_classifications()
    return jsonify({"cache": cache, "count": len(cache)})


@app.route("/api/features/override", methods=["POST"])
def add_manual_override():
    """Set a manual override for a feature's classification.

    Category: Sources

    Request Body:
    {
        "feature_id": "123",
        "importance_score": 5,
        "category": "new_feature",
        "recommended_channels": ["twitter", "inapp"]
    }

    Response: {"status": "override_set", "feature_id": "123", "override": {...}}
    """
    data = request.get_json() or {}
    feature_id = data.get("feature_id")
    if not feature_id:
        return jsonify({"error": "feature_id is required"}), 400

    override = {}
    if "importance_score" in data:
        override["importance_score"] = int(data["importance_score"])
    if "importance_score_reason" in data:
        override["importance_score_reason"] = data["importance_score_reason"]
    if "category" in data:
        override["category"] = data["category"]
    if "recommended_channels" in data:
        override["recommended_channels"] = data["recommended_channels"]
    if "marketing_summary" in data:
        override["marketing_summary"] = data["marketing_summary"]
    if "target_audience" in data:
        override["target_audience"] = data["target_audience"]

    if not override:
        return jsonify({"error": "Provide at least one field to override (e.g. importance_score, category, recommended_channels)"}), 400

    set_manual_override(feature_id, override)
    return jsonify({"status": "override_set", "feature_id": feature_id, "override": override})


@app.route("/api/features/override/<feature_id>", methods=["DELETE"])
def delete_manual_override(feature_id):
    """Remove a manual override for a feature.

    Category: Sources

    Response: {"status": "override_removed", "feature_id": "123"}
    """
    removed = remove_manual_override(feature_id)
    if removed is None:
        return jsonify({"error": f"No override found for {feature_id}"}), 404
    return jsonify({"status": "override_removed", "feature_id": feature_id})


@app.route("/api/features/overrides")
def list_manual_overrides():
    """List all active manual classification overrides.

    Category: Sources

    Response: {"overrides": {"feature_id": {...}, ...}}
    """
    return jsonify({"overrides": get_manual_overrides()})


@app.route("/api/classification/override", methods=["POST"])
def add_classification_override():
    """Save a classification override and teach the AI from marketer corrections.

    Category: Feedback Loop

    Body:
    {
        "feature_id": "abc123",
        "feature_title": "Track Page Redesign",
        "original_classification": {"category": "infrastructure", "importance_score": 2, ...},
        "override_classification": {"category": "improvement", "importance_score": 4},
        "reason": "This impacts artist-facing search UX significantly"
    }

    Response: {"success": true, "entry": {...}, "recommended_channels": [...]}
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    feature_id = data.get("feature_id")
    feature_title = data.get("feature_title", "")
    original = data.get("original_classification", {})
    override = data.get("override_classification", {})
    reason = data.get("reason", "")

    if not feature_id:
        return jsonify({"error": "feature_id is required"}), 400
    if not override.get("category") and not override.get("importance_score"):
        return jsonify({"error": "override_classification must include category or importance_score"}), 400

    entry = save_classification_override(feature_id, feature_title, original, override, reason)

    cached = get_cached_classification(feature_id) or {}
    classification_method = original.get("classification_method") or cached.get("classification_method")
    matched_kw = original.get("matched_keyword") or cached.get("matched_keyword")
    if classification_method == "quick_keyword" and matched_kw:
        record_keyword_override(matched_kw)
        logger.info(f"[tiered] Recorded override for quick_classify keyword '{matched_kw}'")

    override_cats = override.get("categories", [override.get("category", original.get("category"))])
    set_manual_override(feature_id, {
        "category": override.get("category", original.get("category")),
        "categories": override_cats,
        "importance_score": override.get("importance_score", original.get("importance_score")),
        "recommended_channels": entry["override_classification"]["recommended_channels"],
        "manual_override": True,
    })

    return jsonify({
        "success": True,
        "message": "Override saved and will improve future classifications",
        "entry": entry,
        "recommended_channels": entry["override_classification"]["recommended_channels"],
    })


@app.route("/api/classification/overrides")
def list_classification_overrides():
    """List all classification override history, most recent first.

    Category: Feedback Loop

    Response: {"overrides": [...], "count": 5}
    """
    overrides = get_classification_overrides()
    return jsonify({"overrides": overrides, "count": len(overrides)})


@app.route("/api/feature-sets", methods=["GET"])
def list_feature_sets():
    sets = get_feature_sets()
    return jsonify({"sets": sets, "count": len(sets)})


@app.route("/api/feature-sets", methods=["POST"])
def create_feature_set():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    channel = (data.get("channel") or "").strip()
    feature_ids = data.get("feature_ids", [])
    if not name:
        return jsonify({"error": "Set name is required"}), 400
    if not channel:
        return jsonify({"error": "Channel is required"}), 400
    if not feature_ids:
        return jsonify({"error": "At least one feature must be selected"}), 400
    entry = save_feature_set(name, channel, feature_ids)
    return jsonify({"set": entry})


@app.route("/api/feature-sets/<set_id>", methods=["DELETE"])
def remove_feature_set(set_id):
    removed = delete_feature_set(set_id)
    if not removed:
        return jsonify({"error": "Set not found"}), 404
    return jsonify({"deleted": removed})


@app.route("/api/classifier/keywords", methods=["GET"])
def get_classifier_keywords():
    keywords = get_keyword_list()
    return jsonify({"keywords": keywords, "count": len(keywords)})


@app.route("/api/classifier/keywords", methods=["POST"])
def manage_classifier_keywords():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    action = data.get("action", "add")
    keyword = data.get("keyword", "").strip()
    category = data.get("category", "infrastructure")

    if not keyword:
        return jsonify({"error": "keyword is required"}), 400

    if action == "add":
        add_keyword(keyword, category)
        return jsonify({"success": True, "message": f"Added keyword '{keyword}' -> '{category}'"})
    elif action == "remove":
        removed = remove_keyword(keyword)
        if removed:
            return jsonify({"success": True, "message": f"Removed keyword '{keyword}'"})
        return jsonify({"error": f"Keyword '{keyword}' not found"}), 404
    else:
        return jsonify({"error": "action must be 'add' or 'remove'"}), 400


@app.route("/api/classifier/tier-stats")
def get_tier_stats():
    stats = get_classification_tier_stats()
    return jsonify(stats)


@app.route("/api/features/reclassify", methods=["POST"])
def reclassify_feature_with_claude():
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    feature_id = data.get("feature_id")
    if not feature_id:
        return jsonify({"error": "feature_id is required"}), 400

    feature_data = None
    for f in getattr(reclassify_feature_with_claude, '_all_features', []):
        if f.get("id") == feature_id:
            feature_data = f
            break

    if not feature_data:
        if feature_id in CLASSIFICATION_CACHE:
            cached = CLASSIFICATION_CACHE[feature_id]
            feature_data = {
                "id": feature_id,
                "title": cached.get("title", ""),
                "description": data.get("description", ""),
            }
        else:
            feature_data = {
                "id": feature_id,
                "title": data.get("title", ""),
                "description": data.get("description", ""),
            }

    if feature_id in CLASSIFICATION_CACHE:
        del CLASSIFICATION_CACHE[feature_id]

    remove_manual_override(feature_id)

    classification = classify_feature(feature_data, force_claude=True)
    return jsonify({
        "success": True,
        "classification": classification,
        "method": "claude",
    })


@app.route("/api/examples")
def list_all_examples():
    """View all few-shot examples for all channels.

    Category: Few-Shot Examples

    Response: {"twitter": [{...}], "email_newsletter": [{...}], ...}
    """
    return jsonify(FEW_SHOT_EXAMPLES)


@app.route("/api/examples/<channel_key>", methods=["GET"])
def get_channel_examples(channel_key):
    """View few-shot examples for a specific channel.

    Category: Few-Shot Examples

    Response: {"channel": "twitter", "examples": [{...}]}
    """
    examples = FEW_SHOT_EXAMPLES.get(channel_key)
    if examples is None:
        return jsonify({"error": f"No examples found for channel '{channel_key}'"}), 404
    return jsonify({"channel": channel_key, "examples": examples})


@app.route("/api/examples/<channel_key>", methods=["POST"])
def add_channel_example(channel_key):
    """Add a new few-shot example for a channel.

    Category: Few-Shot Examples

    Request Body:
    {
        "feature_context": "Description of the feature",
        "content": "The published marketing content"
    }

    Response: {"channel": "twitter", "examples": [{...}]}
    """
    data = request.get_json() or {}
    feature_context = data.get("feature_context")
    content = data.get("content")
    if not feature_context or not content:
        return jsonify({"error": "Both 'feature_context' and 'content' are required"}), 400

    if channel_key not in FEW_SHOT_EXAMPLES:
        FEW_SHOT_EXAMPLES[channel_key] = []

    FEW_SHOT_EXAMPLES[channel_key].append({
        "feature_context": feature_context,
        "content": content,
    })
    print(f"[examples] Added example for channel '{channel_key}' (now {len(FEW_SHOT_EXAMPLES[channel_key])} total)", flush=True)
    return jsonify({"channel": channel_key, "examples": FEW_SHOT_EXAMPLES[channel_key]})


@app.route("/api/examples/<channel_key>/<int:index>", methods=["DELETE"])
def delete_channel_example(channel_key, index):
    """Remove a few-shot example by index.

    Category: Few-Shot Examples

    Response: {"channel": "twitter", "removed": {...}, "examples": [{...}]}
    """
    examples = FEW_SHOT_EXAMPLES.get(channel_key)
    if examples is None:
        return jsonify({"error": f"No examples found for channel '{channel_key}'"}), 404
    if index < 0 or index >= len(examples):
        return jsonify({"error": f"Index {index} out of range (0-{len(examples)-1})"}), 400

    removed = examples.pop(index)
    print(f"[examples] Removed example {index} from channel '{channel_key}' (now {len(examples)} total)", flush=True)
    return jsonify({"channel": channel_key, "removed": removed, "examples": examples})


@app.route("/api/feedback", methods=["POST"])
def save_feedback_endpoint():
    """Save a feedback record (original vs approved draft) for learning.

    Category: Feedback Loop

    Request Body:
    {
        "channel": "twitter",
        "feature_title": "Artist Audience Overlap Tool",
        "original_draft": "the AI generated text...",
        "approved_draft": "the marketer's edited final version...",
        "feedback_note": "Made it shorter, removed the question format"
    }

    Response: {"success": true, "total_feedback_for_channel": 3, "record": {...}}
    """
    data = request.get_json() or {}
    channel = data.get("channel")
    feature_title = data.get("feature_title")
    original_draft = data.get("original_draft")
    approved_draft = data.get("approved_draft")
    feedback_note = data.get("feedback_note", "")

    if not channel or not feature_title or not original_draft or not approved_draft:
        return jsonify({"error": "channel, feature_title, original_draft, and approved_draft are all required"}), 400

    record = save_feedback(channel, feature_title, original_draft, approved_draft, feedback_note)
    total = len(get_feedback_history(channel, limit=999))
    print(f"[feedback] Saved feedback for '{feature_title}' on channel '{channel}' (total for channel: {total})", flush=True)
    return jsonify({"success": True, "total_feedback_for_channel": total, "record": record})


@app.route("/api/feedback", methods=["GET"])
def get_all_feedback_endpoint():
    """View all feedback history across all channels.

    Category: Feedback Loop

    Response: {"twitter": [{...}], "email_newsletter": [{...}], ...}
    """
    return jsonify(get_all_feedback())


@app.route("/api/feedback/<channel_key>", methods=["GET"])
def get_channel_feedback(channel_key):
    """View feedback history for a specific channel (most recent first).

    Category: Feedback Loop

    Query Params: ?limit=10

    Response: {"channel": "twitter", "feedback": [{...}], "total": 5}
    """
    limit = request.args.get("limit", default=10, type=int)
    records = get_feedback_history(channel_key, limit=limit)
    return jsonify({"channel": channel_key, "feedback": records, "total": len(records)})


@app.route("/api/approve", methods=["POST"])
def approve_and_save():
    """Approve a draft and save feedback for future learning.

    Category: Feedback Loop

    Request Body:
    {
        "feature": {"title": "..."},
        "channel": "twitter",
        "original_draft": "AI generated text...",
        "approved_draft": "final edited text...",
        "feedback_note": "optional note about changes"
    }

    Response: {"success": true, "message": "Approved and feedback saved for future learning"}
    """
    data = request.get_json() or {}
    channel = data.get("channel")
    original_draft = data.get("original_draft")
    approved_draft = data.get("approved_draft")
    feedback_note = data.get("feedback_note", "")

    feature = data.get("feature", {})
    feature_title = feature.get("title", "") if isinstance(feature, dict) else ""
    if not feature_title:
        feature_title = data.get("feature_title", "")

    if not channel or not original_draft or not approved_draft:
        return jsonify({"error": "channel, original_draft, and approved_draft are required"}), 400

    was_edited = (original_draft.strip() != approved_draft.strip())
    _inc_approve(was_edited=was_edited)
    save_feedback(channel, feature_title, original_draft, approved_draft, feedback_note)
    print(f"[approve] Approved draft for '{feature_title}' on channel '{channel}' (edited={was_edited})", flush=True)
    return jsonify({"success": True, "message": "Approved and feedback saved for future learning"})


@app.route("/api/publish/twitter", methods=["POST"])
def publish_twitter():
    """Publish a tweet via Twitter API or fallback to intent URL.

    Category: Publishing

    Request Body:
    {
        "content": "the tweet text"
    }

    Response: {"success": true, "tweet_url": "...", "method": "api"|"fallback"}
    """
    from integrations.twitter_client import publish_tweet

    logger.info(f"[publish/twitter] Request received, content-length header: {request.content_length}")
    data = request.get_json() or {}
    content = data.get("content", "").strip()
    has_image = bool(data.get("image"))
    logger.info(f"[publish/twitter] Content: {len(content)} chars, has_image: {has_image}")
    if not content:
        return jsonify({"success": False, "error": "content is required"}), 400

    feature_id = data.get("feature_id", "")
    image_base64 = data.get("image")
    result = publish_tweet(content, image_base64=image_base64)
    if result.get("success") and result.get("method") == "api" and feature_id:
        mark_published(feature_id, "twitter", tweet_url=result.get("tweet_url"))
    status_code = 200 if result.get("success") else 500
    return jsonify(result), status_code


@app.route("/api/publish/state", methods=["GET"])
def get_publish_state():
    feature_id = request.args.get("feature_id", "")
    channels_str = request.args.get("channels", "")
    if not feature_id or not channels_str:
        return jsonify({}), 200
    channels = [c.strip() for c in channels_str.split(",") if c.strip()]
    state = get_feature_state(feature_id, channels)
    return jsonify(state), 200


def _build_video_map(feature_id):
    if not feature_id:
        return {}
    vids = list_feature_videos(feature_id)
    if not vids:
        return {}
    base = os.environ.get("REPLIT_DEV_DOMAIN", "")
    scheme = "https" if base else "http"
    if not base:
        base = "localhost:5000"
    fallback_thumb = "https://via.placeholder.com/640x360/222222/999999?text=Video"
    video_map = {}
    for v in vids:
        vid_id = v.get("video_id", "")
        fname = v.get("filename", "")
        if vid_id and fname:
            has_thumb = v.get("has_thumb", True)
            video_map[fname] = {
                "thumb_url": f"{scheme}://{base}/api/videos/{vid_id}/thumb" if has_thumb else fallback_thumb,
                "video_url": f"{scheme}://{base}/api/videos/{vid_id}",
            }
    return video_map


@app.route("/api/publish/email", methods=["POST"])
def publish_email():
    from integrations.sendgrid_client import send_email

    data = request.get_json() or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"success": False, "error": "content is required"}), 400

    subject = data.get("subject", "").strip()
    channel = data.get("channel", "email_standalone")
    to_email = data.get("to_email", "").strip() or None
    is_test = data.get("is_test", True)
    feature_id = data.get("feature_id", "")
    feature_ids = data.get("feature_ids", None)
    if isinstance(feature_ids, list):
        feature_ids = [str(f).strip() for f in feature_ids if str(f).strip()]
    else:
        feature_ids = None

    lines = content.split("\n", 1)
    has_subject_line = lines[0].lower().startswith("subject:")
    if has_subject_line:
        content = lines[1].strip() if len(lines) > 1 else content
    if not subject:
        if has_subject_line:
            subject = lines[0][len("subject:"):].strip()
        elif channel == "email_newsletter":
            subject = "Chartmetric Product Update"
        else:
            subject = "Chartmetric Product Update"

    images = data.get("images", None)

    if to_email:
        recipient_count = len([e for e in to_email.split(",") if e.strip()])
        max_recipients = 2000
        if recipient_count > max_recipients:
            return jsonify({"success": False, "error": f"Too many recipients ({recipient_count}). Maximum is {max_recipients}."}), 400
        if recipient_count > 5 and is_test:
            return jsonify({"success": False, "error": "Cannot send to more than 5 recipients in test mode. Select an audience to send to a larger group."}), 400

    from_name = data.get("from_name", "").strip() or None
    template_id = data.get("template_id", "").strip() or None
    bcc_email = data.get("bcc_email", "").strip() or None

    if feature_ids:
        videos = {}
        for fid in feature_ids:
            videos.update(_build_video_map(fid))
    else:
        videos = _build_video_map(feature_id)
    result = send_email(subject=subject, body=content, to_email=to_email, is_test=is_test, images=images, from_name=from_name, template_id=template_id, videos=videos, bcc_email=bcc_email)
    if result.get("success") and result.get("method") in ("sendgrid", "resend"):
        if feature_ids:
            for fid in feature_ids:
                mark_published(fid, channel)
        elif feature_id:
            mark_published(feature_id, channel)
    status_code = 200 if result.get("success") else 500
    return jsonify(result), status_code


@app.route("/api/publish/email/preview", methods=["GET", "POST"])
def preview_email():
    from integrations.sendgrid_client import render_email_html

    if request.method == "POST":
        data = request.get_json() or {}
        content = data.get("content", "")
        subject = data.get("subject", "Chartmetric Product Update")
        images = data.get("images", None)
        from_name = data.get("from_name", "").strip() or "Chartmetric"
        template_id = data.get("template_id", "").strip() or ""
    else:
        content = request.args.get("content", "")
        subject = request.args.get("subject", "Chartmetric Product Update")
        images = None
        from_name = request.args.get("from_name", "Chartmetric")
        template_id = ""

    if template_id:
        from integrations.sendgrid_client import get_resend_template
        from markupsafe import escape
        import html as _html_mod
        safe_tid = escape(template_id)
        safe_subject = escape(subject)
        result = get_resend_template(template_id)
        if not result.get("success"):
            err_msg = escape(result.get("error", "Unknown error"))
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#f4f4f7;padding:32px 16px;">
<div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #fecaca;border-left:4px solid #dc2626;border-radius:8px;padding:22px 24px;box-shadow:0 1px 3px rgba(0,0,0,0.05);">
<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
<span style="display:inline-block;width:18px;height:18px;border-radius:50%;background:#dc2626;color:#fff;font-size:12px;font-weight:700;text-align:center;line-height:18px;">!</span>
<h2 style="margin:0;font-size:15px;color:#dc2626;font-weight:700;">Could not load Resend template</h2>
</div>
<p style="margin:0 0 10px 0;font-size:13px;color:#444;">Template ID: <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px;font-family:Menlo,Consolas,monospace;font-size:12px;">{safe_tid}</code></p>
<p style="margin:0 0 14px 0;font-size:13px;color:#666;line-height:1.5;">{err_msg}</p>
<p style="margin:0;font-size:12px;color:#999;">The send call will still pass this template ID to Resend; this preview just couldn't render the layout locally.</p>
</div>
</body></html>"""
            return html, 200, {"Content-Type": "text/html; charset=utf-8"}

        tpl_html = result.get("html") or ""
        tpl_name = result.get("name") or template_id
        if not tpl_html.strip():
            tpl_html = """<!DOCTYPE html><html><body style="margin:0;padding:48px 24px;font-family:-apple-system,sans-serif;color:#888;text-align:center;background:#fff;">
<p style="margin:0;font-size:14px;">This Resend template has no HTML body to preview.</p>
<p style="margin:8px 0 0 0;font-size:12px;color:#aaa;">It may use Resend's React/MJML editor or be empty.</p></body></html>"""

        safe_inner = _html_mod.escape(tpl_html, quote=True)
        safe_name = escape(tpl_name)
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>html,body{{margin:0;padding:0;height:100%;background:#f4f4f7;}}</style></head>
<body style="display:flex;flex-direction:column;height:100vh;">
<div style="background:#1a1d23;color:#fff;padding:8px 14px;font:600 12px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;flex:0 0 auto;border-bottom:2px solid #00C9A7;display:flex;align-items:center;gap:8px;">
<span style="background:#00C9A7;color:#0a1f1a;padding:2px 7px;border-radius:3px;font-size:10px;letter-spacing:0.4px;text-transform:uppercase;">Resend template</span>
<span style="color:#fff;">{safe_name}</span>
<span style="opacity:0.55;font-weight:400;">({safe_tid})</span>
<span style="margin-left:auto;opacity:0.6;font-weight:400;">Subject: {safe_subject}</span>
</div>
<iframe srcdoc="{safe_inner}" sandbox="allow-same-origin" style="flex:1 1 auto;width:100%;border:0;background:#fff;" title="Resend template preview"></iframe>
</body></html>"""
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    feature_id = data.get("feature_id", "") if request.method == "POST" else request.args.get("feature_id", "")
    feature_ids = data.get("feature_ids", None) if request.method == "POST" else None
    if isinstance(feature_ids, list) and feature_ids:
        videos = {}
        for fid in feature_ids:
            fid = str(fid).strip()
            if fid:
                videos.update(_build_video_map(fid))
    else:
        videos = _build_video_map(feature_id)
    html = render_email_html(subject, content, images=images, from_name=from_name, videos=videos)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/resend/templates", methods=["GET"])
def get_resend_templates():
    from integrations.sendgrid_client import list_resend_templates
    templates = list_resend_templates()
    result = []
    for t in templates:
        if isinstance(t, dict):
            result.append({"id": t.get("id", ""), "name": t.get("name", "")})
        else:
            result.append({"id": getattr(t, "id", ""), "name": getattr(t, "name", "")})
    return jsonify({"success": True, "templates": result}), 200


@app.route("/api/resend/audiences", methods=["GET"])
def get_resend_audiences():
    from integrations.sendgrid_client import list_resend_audiences
    audiences = list_resend_audiences()
    return jsonify({"success": True, "audiences": audiences}), 200


@app.route("/api/resend/audiences/<audience_id>/contacts", methods=["GET"])
def get_resend_contacts(audience_id):
    from integrations.sendgrid_client import list_resend_contacts
    contacts = list_resend_contacts(audience_id)
    subscribed = [{"email": c.get("email", "")} for c in contacts if not c.get("unsubscribed", False) and c.get("email")]
    return jsonify({"success": True, "contacts": subscribed, "total": len(subscribed)}), 200


@app.route("/api/publish/inapp", methods=["POST"])
def publish_inapp():
    from integrations.inapp_client import publish_announcement
    import re

    data = request.get_json() or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"success": False, "error": "content is required"}), 400

    feature_title = data.get("feature_title", "")
    feature_id = data.get("feature_id", "")
    category = data.get("category", "")

    title = feature_title
    body = content
    bold_match = re.match(r'^\*\*(.+?)\*\*\s*\n?', content)
    if bold_match:
        title = title or bold_match.group(1)
        body = content[bold_match.end():].strip()
    elif not title:
        lines = content.split('\n', 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""

    result = publish_announcement(title=title, body=body, feature_id=feature_id, category=category)
    if result.get("success") and feature_id:
        mark_published(feature_id, "inapp")
    status_code = 200 if result.get("success") else 500
    return jsonify(result), status_code


@app.route("/api/announcements", methods=["GET"])
def get_announcements_endpoint():
    from integrations.inapp_client import get_announcements

    limit = request.args.get("limit", 20, type=int)
    status = request.args.get("status", None)
    announcements = get_announcements(limit=limit, status=status)
    return jsonify({"announcements": announcements, "total": len(announcements)}), 200


@app.route("/api/announcements/<announcement_id>/dismiss", methods=["POST"])
def dismiss_announcement_endpoint(announcement_id):
    from integrations.inapp_client import dismiss_announcement

    result = dismiss_announcement(announcement_id)
    status_code = 200 if result.get("success") else 404
    return jsonify(result), status_code


@app.route("/api/announcements/widget", methods=["GET"])
def announcements_widget():
    from integrations.inapp_client import get_announcements
    import html as html_mod

    announcements = get_announcements(limit=5, status="active")
    cards_html = ""
    if not announcements:
        cards_html = '<div class="empty">No announcements yet</div>'
    else:
        for ann in announcements:
            cat = ann.get("category", "") or ""
            cat_badge = f'<span class="cat-badge">{html_mod.escape(cat.replace("_", " ").title())}</span>' if cat else ""
            ts = ann.get("published_at", "")[:16].replace("T", " ")
            body_lines = html_mod.escape(ann.get("body", "")).replace("\n", "<br>")
            cards_html += f'''<div class="ann-card" id="{html_mod.escape(ann["id"])}">
                <button class="dismiss" onclick="dismiss('{html_mod.escape(ann["id"])}')">&times;</button>
                <div class="ann-header">{cat_badge}<span class="ann-ts">{ts} UTC</span></div>
                <div class="ann-title">{html_mod.escape(ann.get("title", ""))}</div>
                <div class="ann-body">{body_lines}</div>
            </div>'''

    widget_html = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Chartmetric Announcements</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0f1923; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; min-height:100vh; display:flex; align-items:flex-end; justify-content:flex-end; padding:24px; }}
.widget {{ width:380px; max-height:90vh; overflow-y:auto; display:flex; flex-direction:column; gap:12px; }}
.ann-card {{
    background:#1a2332; border:1px solid #2a3a4a; border-left:3px solid #00C9A7;
    border-radius:10px; padding:16px 18px; position:relative;
    animation: slideIn 0.4s ease-out forwards;
    opacity:0; transform:translateX(40px);
}}
.ann-card:nth-child(1) {{ animation-delay:0.3s; }}
.ann-card:nth-child(2) {{ animation-delay:0.5s; }}
.ann-card:nth-child(3) {{ animation-delay:0.7s; }}
@keyframes slideIn {{ to {{ opacity:1; transform:translateX(0); }} }}
@keyframes pulse {{ 0%,100% {{ border-left-color:#00C9A7; }} 50% {{ border-left-color:#00e6be; }} }}
.ann-card:first-child {{ animation: slideIn 0.4s 0.3s ease-out forwards, pulse 3s 1s ease-in-out infinite; }}
.dismiss {{ position:absolute; top:10px; right:12px; background:none; border:none; color:#556677; font-size:18px; cursor:pointer; line-height:1; }}
.dismiss:hover {{ color:#ff6b6b; }}
.ann-header {{ display:flex; align-items:center; gap:8px; margin-bottom:8px; }}
.cat-badge {{ background:rgba(0,201,167,0.15); color:#00C9A7; font-size:11px; font-weight:600; padding:2px 8px; border-radius:4px; text-transform:uppercase; letter-spacing:0.5px; }}
.ann-ts {{ color:#556677; font-size:11px; }}
.ann-title {{ color:#e8edf2; font-size:15px; font-weight:700; margin-bottom:6px; line-height:1.4; }}
.ann-body {{ color:#99aabb; font-size:13px; line-height:1.6; }}
.empty {{ color:#556677; text-align:center; padding:40px; font-size:14px; }}
.widget-header {{ color:#e8edf2; font-size:13px; font-weight:600; display:flex; align-items:center; gap:8px; padding:0 4px; }}
.widget-header .dot {{ width:8px; height:8px; border-radius:50%; background:#00C9A7; animation: pulse-dot 2s infinite; }}
@keyframes pulse-dot {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.4; }} }}
</style></head>
<body>
<div class="widget">
    <div class="widget-header"><span class="dot"></span> Chartmetric Product Updates</div>
    {cards_html}
</div>
<script>
function dismiss(id) {{
    fetch('/api/announcements/' + id + '/dismiss', {{ method:'POST' }});
    var el = document.getElementById(id);
    if (el) {{ el.style.transition='all 0.3s'; el.style.opacity='0'; el.style.transform='translateX(40px)'; setTimeout(function(){{ el.remove(); }}, 300); }}
}}
</script>
</body></html>'''
    return widget_html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/publish/all", methods=["GET"])
def get_all_published_endpoint():
    return jsonify(get_all_published()), 200


@app.route("/api/publish/image", methods=["POST"])
def save_image_endpoint():
    data = request.get_json() or {}
    feature_id = data.get("feature_id", "")
    channel = data.get("channel", "")
    data_url = data.get("dataUrl", "")
    filename = data.get("name", "image.png")
    file_size = data.get("size", 0)
    if not feature_id or not data_url:
        return jsonify({"success": False, "error": "feature_id, dataUrl required"}), 400
    try:
        save_publish_image(feature_id, channel, data_url, filename, file_size)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True}), 200


@app.route("/api/publish/image/hosted/<img_id>")
def serve_hosted_image(img_id):
    import base64 as _b64
    import re as _re
    from flask import send_file
    from io import BytesIO
    from ai.publish_store import IMAGES_DIR
    safe_id = _re.sub(r'[^a-f0-9]', '', img_id)
    img_dir = os.path.join(IMAGES_DIR, f"_hosted_{safe_id}")
    img_dir = os.path.realpath(img_dir)
    if not img_dir.startswith(IMAGES_DIR):
        return "Not found", 404
    dat_path = os.path.join(img_dir, "image.dat")
    meta_path = os.path.join(img_dir, "meta.json")
    if not os.path.exists(dat_path) or not os.path.exists(meta_path):
        return "Not found", 404
    with open(dat_path, "r") as f:
        data_url = f.read()
    m = _re.match(r"data:image/(\w+);base64,(.+)", data_url)
    if not m:
        return "Invalid image", 500
    ext = m.group(1)
    raw = _b64.b64decode(m.group(2))
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
    return send_file(BytesIO(raw), mimetype=mime_map.get(ext, f"image/{ext}"))


@app.route("/api/publish/image/serve/<feature_id>")
def serve_feature_image(feature_id):
    import base64 as _b64
    import re as _re
    from flask import send_file
    from io import BytesIO
    img_data = get_publish_image(feature_id)
    if not img_data or not img_data.get("dataUrl"):
        return "Not found", 404
    data_url = img_data["dataUrl"]
    m = _re.match(r"data:image/(\w+);base64,(.+)", data_url)
    if not m:
        return "Invalid image data", 500
    ext = m.group(1)
    raw = _b64.b64decode(m.group(2))
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}
    return send_file(BytesIO(raw), mimetype=mime_map.get(ext, f"image/{ext}"), download_name=img_data.get("name", f"image.{ext}"))


@app.route("/api/publish/image", methods=["DELETE"])
def delete_image_endpoint():
    data = request.get_json() or {}
    feature_id = data.get("feature_id", "")
    channel = data.get("channel", "")
    if not feature_id:
        return jsonify({"success": False, "error": "feature_id required"}), 400
    try:
        remove_publish_image(feature_id, channel)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True}), 200


@app.route("/api/publish/video", methods=["POST"])
def save_video_endpoint():
    data = request.get_json() or {}
    feature_id = data.get("feature_id", "")
    data_url = data.get("dataUrl", "")
    filename = data.get("name", "video.mp4")
    if not feature_id or not data_url:
        return jsonify({"success": False, "error": "feature_id, dataUrl required"}), 400
    try:
        video_id = save_publish_video(feature_id, data_url, filename)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logging.error(f"[video] Upload failed: {e}")
        return jsonify({"success": False, "error": "Video upload failed"}), 500
    thumb_url = f"/api/videos/{video_id}/thumb"
    video_url = f"/api/videos/{video_id}"
    return jsonify({"success": True, "video_id": video_id, "thumb_url": thumb_url, "video_url": video_url}), 200


@app.route("/api/videos/<video_id>")
def serve_video(video_id):
    from flask import send_file
    try:
        video_path, meta = get_video_path(video_id)
    except ValueError:
        return "Not found", 404
    if not video_path:
        return "Not found", 404
    ext = meta.get("ext", ".mp4")
    mimetypes = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska"}
    return send_file(video_path, mimetype=mimetypes.get(ext, "video/mp4"))


@app.route("/api/videos/<video_id>/thumb")
def serve_video_thumb(video_id):
    from flask import send_file, redirect
    try:
        thumb_path = get_video_thumb_path(video_id)
    except ValueError:
        return "Not found", 404
    if not thumb_path:
        return redirect("https://via.placeholder.com/640x360/222222/999999?text=Video")
    return send_file(thumb_path, mimetype="image/jpeg")


@app.route("/api/videos/external-thumb/<key>")
def serve_external_video_thumb(key):
    from flask import send_file, redirect
    from integrations.video_thumb import get_cached_external_thumb_path
    path = get_cached_external_thumb_path(key)
    if not path:
        return redirect("https://via.placeholder.com/640x360/222222/999999?text=Video")
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/features/<feature_id>/videos")
def get_feature_videos(feature_id):
    vids = list_feature_videos(feature_id)
    base = os.environ.get("REPLIT_DEV_DOMAIN", "")
    scheme = "https" if base else "http"
    if not base:
        base = "localhost:5000"
    result = []
    for v in vids:
        vid_id = v.get("video_id", "")
        fname = v.get("filename", "")
        has_thumb = v.get("has_thumb", True)
        if vid_id and fname:
            result.append({
                "video_id": vid_id,
                "filename": fname,
                "thumb_url": f"{scheme}://{base}/api/videos/{vid_id}/thumb" if has_thumb else "https://via.placeholder.com/640x360/222222/999999?text=Video",
                "video_url": f"{scheme}://{base}/api/videos/{vid_id}",
            })
    return jsonify(result), 200


@app.route("/api/generate", methods=["POST"])
def generate_content_endpoint():
    """Generate content for one feature across multiple channels.

    Category: Content Generation

    Request Body:
    {
        "feature": {"id": "...", "title": "...", "description": "..."},
        "channels": ["twitter", "email_newsletter"],
        "custom_instructions": ""
    }

    Response:
    {
        "feature_id": "...",
        "feature_title": "...",
        "generated_content": {"twitter": {"content": "...", "char_count": 142, ...}},
        "generated_at": "2026-03-31T12:00:00Z"
    }
    """
    data = request.get_json() or {}
    feature = data.get("feature")
    if not feature or not isinstance(feature, dict):
        return jsonify({"error": "feature is required and must be a feature object"}), 400

    channels = data.get("channels")
    custom_instructions = data.get("custom_instructions", "")

    if channels is not None and (not isinstance(channels, list) or not all(isinstance(c, str) for c in channels)):
        return jsonify({"error": "channels must be a list of strings"}), 400

    if not channels:
        classification = feature.get("classification", {})
        channels = classification.get("recommended_channels")

    try:
        print(f"[generate] Generating content for '{feature.get('title', 'unknown')}' on channels: {channels}", flush=True)
        results = generate_all_channels(feature, channels=channels, custom_instructions=custom_instructions or None)
        _inc_generate(len(results) if results else 1)
        return jsonify({
            "feature_id": feature.get("id", ""),
            "feature_title": feature.get("title", ""),
            "generated_content": results,
            "classification": feature.get("classification"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Generate endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/share/slack", methods=["POST"])
def share_slack_endpoint():
    """Share content to a Slack webhook.

    Category: Sharing

    Request Body:
    {
        "webhook_url": "https://hooks.slack.com/services/...",
        "text": "The message to post",
        "feature_title": "Optional feature title"
    }
    """
    data = request.get_json() or {}
    webhook_url = data.get("webhook_url", "").strip()
    text = data.get("text", "").strip()

    if not webhook_url:
        return jsonify({"error": "webhook_url is required"}), 400
    if not text:
        return jsonify({"error": "text is required"}), 400

    from urllib.parse import urlparse
    parsed = urlparse(webhook_url)
    if (parsed.scheme != "https" or
        parsed.hostname != "hooks.slack.com" or
        not parsed.path.startswith("/services/") or
        parsed.username or parsed.password or parsed.port):
        return jsonify({"error": "Invalid Slack webhook URL"}), 400

    import requests as req_lib
    payload = {"text": text}
    try:
        resp = req_lib.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200 and resp.text == "ok":
            return jsonify({"success": True})
        else:
            return jsonify({"error": f"Slack returned {resp.status_code}: {resp.text}"}), 502
    except Exception as e:
        logger.error(f"Slack webhook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate/single", methods=["POST"])
def generate_single_endpoint():
    """Regenerate content for one channel, optionally with feedback on previous draft.

    Category: Content Generation

    Request Body:
    {
        "feature": {"id": "...", "title": "...", "description": "..."},
        "channel": "twitter",
        "custom_instructions": "",
        "feedback": "make it shorter, focus on the data angle"
    }

    Response: {"channel": "twitter", "content": "...", "char_count": 142, "success": true, ...}
    """
    data = request.get_json() or {}
    feature = data.get("feature")
    channel = data.get("channel")

    if not feature or not isinstance(feature, dict):
        return jsonify({"error": "feature is required and must be a feature object"}), 400
    if not channel or not isinstance(channel, str):
        return jsonify({"error": "channel is required (e.g. 'twitter')"}), 400

    from ai.channel_configs import CHANNEL_CONFIGS
    if channel not in CHANNEL_CONFIGS:
        return jsonify({"error": f"Unknown channel: '{channel}'. Valid channels: {list(CHANNEL_CONFIGS.keys())}"}), 400
    if not CHANNEL_CONFIGS[channel].get("enabled", False):
        return jsonify({"error": f"Channel '{channel}' is disabled"}), 400

    custom_instructions = data.get("custom_instructions", "")
    feedback = data.get("feedback", "")
    current_content = data.get("current_content", "")
    mode = data.get("mode") or None

    try:
        print(f"[generate/single] Regenerating '{feature.get('title', 'unknown')}' for channel '{channel}' (feedback: {bool(feedback)}, mode: {mode or 'default'})", flush=True)
        result = generate_for_channel(
            feature, channel,
            custom_instructions=custom_instructions or None,
            feedback=feedback or None,
            current_content=current_content or None,
            skip_cache=True,
            mode=mode,
        )
        _inc_generate(1)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Generate single endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate/batch", methods=["POST"])
def generate_batch_endpoint():
    """Bulk generate content for multiple features. Auto-classifies if needed.

    Category: Content Generation

    Request Body:
    {
        "features": [{"id": "...", "title": "...", "description": "..."}, ...],
        "channels": ["twitter", "email_newsletter"],
        "min_importance": 3
    }

    Response:
    {
        "results": [{...}],
        "total_features": 10,
        "filtered_features": 5,
        "skipped_features": 5,
        "generated_at": "..."
    }
    """
    data = request.get_json() or {}
    features = data.get("features")
    channels = data.get("channels")
    min_importance = data.get("min_importance", 3)

    if not features or not isinstance(features, list):
        return jsonify({"error": "features is required and must be a list of feature objects"}), 400
    if not all(isinstance(f, dict) for f in features):
        return jsonify({"error": "Each feature must be a JSON object"}), 400
    if channels is not None and (not isinstance(channels, list) or not all(isinstance(c, str) for c in channels)):
        return jsonify({"error": "channels must be a list of strings"}), 400
    if not isinstance(min_importance, (int, float)):
        return jsonify({"error": "min_importance must be a number"}), 400
    min_importance = int(min_importance)

    try:
        print(f"[generate/batch] Processing {len(features)} features, min_importance={min_importance}", flush=True)

        needs_classification = [f for f in features if "classification" not in f]
        already_classified = [f for f in features if "classification" in f]

        if needs_classification:
            print(f"[generate/batch] Classifying {len(needs_classification)} unclassified features", flush=True)
            newly_classified = classify_features_batch(needs_classification)
            already_classified.extend(newly_classified)

        all_features = apply_manual_overrides(already_classified)
        total_features = len(all_features)

        filtered = [
            f for f in all_features
            if f.get("classification", {}).get("importance_score", 0) >= min_importance
        ]
        skipped = total_features - len(filtered)

        print(f"[generate/batch] {len(filtered)} features passed importance filter (skipped {skipped})", flush=True)

        results = []
        for f in filtered:
            feature_channels = channels
            if not feature_channels:
                feature_channels = f.get("classification", {}).get("recommended_channels")

            content = generate_all_channels(f, channels=feature_channels)
            _inc_generate(len(content) if content else 1)
            results.append({
                "feature_id": f.get("id", ""),
                "feature_title": f.get("title", ""),
                "generated_content": content,
                "classification": f.get("classification"),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            })

        return jsonify({
            "results": results,
            "total_features": total_features,
            "filtered_features": len(filtered),
            "skipped_features": skipped,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Generate batch endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate/email-subject", methods=["POST"])
def generate_email_subject_endpoint():
    """Generate a generic combined-email subject line from multiple features.

    Request: {"features": [{"title": "...", "description": "..."}, ...], "channel": "email_newsletter"}
    Response: {"subject": "Exciting Updates on Chartmetric: Ultra plan, Richer Social Data, and more"}
    """
    from ai.claude_client import generate_content

    data = request.get_json() or {}
    features = data.get("features") or []
    if not isinstance(features, list) or not features:
        return jsonify({"error": "features is required"}), 400

    if len(features) == 1:
        title = (features[0].get("title") or "").strip()
        return jsonify({"subject": title or "Chartmetric Product Update"})

    lines = []
    for i, f in enumerate(features[:8], 1):
        title = (f.get("title") or "").strip()
        desc = (f.get("description") or "").strip()
        if len(desc) > 240:
            desc = desc[:240] + "..."
        lines.append(f"{i}. {title}\n   {desc}" if desc else f"{i}. {title}")
    features_block = "\n".join(lines)

    system_prompt = (
        "You write concise email subject lines for product update announcements that bundle "
        "multiple features into one email. Output ONLY the subject line — no quotes, no preamble, "
        "no explanation."
    )
    user_prompt = (
        "Write a single email subject line that announces these "
        f"{len(features)} features bundled together. Use this exact format:\n\n"
        "Exciting Updates on Chartmetric: <Keyword1>, <Keyword2>, and more\n\n"
        "Rules:\n"
        "- Each <Keyword> is a SHORT 1–2 word abstraction of a feature (e.g., 'Richer Social Data' "
        "instead of 'YouTube Monthly Views Evolution chart').\n"
        "- Use 2 keywords if there are exactly 2 features (drop the 'and more').\n"
        "- Use exactly 2 keywords + 'and more' if there are 3 or more features. Pick the two most "
        "marketable / highest-impact features for the keywords.\n"
        "- Capitalize each keyword in title case.\n"
        "- Keep the whole subject under ~70 characters when possible.\n\n"
        f"Features:\n{features_block}\n\nReturn ONLY the subject line."
    )

    result = generate_content(system_prompt, user_prompt, max_tokens=120)
    if not result.get("success"):
        titles = [(f.get("title") or "").strip() for f in features if (f.get("title") or "").strip()]
        if len(titles) >= 3:
            fallback = f"Exciting Updates on Chartmetric: {titles[0]}, {titles[1]}, and more"
        elif len(titles) == 2:
            fallback = f"Exciting Updates on Chartmetric: {titles[0]} and {titles[1]}"
        else:
            fallback = "Chartmetric Product Update"
        return jsonify({"subject": fallback, "fallback": True, "error": result.get("error")})

    subject = (result.get("content") or "").strip().strip('"').strip("'").splitlines()[0].strip()
    return jsonify({"subject": subject})


@app.route("/api/generate/batch-single-channel", methods=["POST"])
def generate_batch_single_channel_endpoint():
    """Generate content for multiple features on a single channel.

    Category: Content Generation

    Request Body:
    {
        "features": [{"id": "...", "title": "...", "description": "..."}, ...],
        "channel": "notion_monthly",
        "custom_instructions": ""
    }

    Response:
    {
        "channel": "notion_monthly",
        "channel_display_name": "...",
        "results": [{"feature_id": "...", "feature_title": "...", "content": "...", "char_count": N, "success": true}],
        "total": 8,
        "succeeded": 8,
        "failed": 0
    }
    """
    data = request.get_json() or {}
    features = data.get("features")
    channel = data.get("channel")
    custom_instructions = data.get("custom_instructions", "")
    mode = data.get("mode") or None

    if not features or not isinstance(features, list):
        return jsonify({"error": "features is required and must be a list"}), 400
    if not channel or not isinstance(channel, str):
        return jsonify({"error": "channel is required and must be a string"}), 400

    from ai.channel_configs import CHANNEL_CONFIGS
    if channel not in CHANNEL_CONFIGS:
        return jsonify({"error": f"Unknown channel: {channel}"}), 400

    if mode is None and channel == "email_standalone" and len(features) >= 2:
        mode = "digest"

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        config = CHANNEL_CONFIGS[channel]
        print(f"[generate/batch-single] Generating {channel} for {len(features)} features (mode: {mode or 'default'})", flush=True)

        def gen_one(feature):
            result = generate_for_channel(feature, channel, custom_instructions=custom_instructions or None, mode=mode)
            result["feature_id"] = feature.get("id", "")
            result["feature_title"] = feature.get("title", "")
            return result

        results = []
        succeeded = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=min(len(features), 5)) as executor:
            future_map = {executor.submit(gen_one, f): i for i, f in enumerate(features)}
            result_by_idx = {}
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    r = future.result()
                    result_by_idx[idx] = r
                    if r.get("success"):
                        succeeded += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(f"[batch-single] Feature {idx} error: {e}")
                    result_by_idx[idx] = {
                        "feature_id": features[idx].get("id", ""),
                        "feature_title": features[idx].get("title", ""),
                        "channel": channel,
                        "content": "",
                        "char_count": 0,
                        "success": False,
                        "error": str(e),
                    }
                    failed += 1

            for i in range(len(features)):
                results.append(result_by_idx.get(i, {"success": False, "error": "Missing"}))

        _inc_generate(succeeded)
        return jsonify({
            "channel": channel,
            "channel_display_name": config["display_name"],
            "results": results,
            "total": len(features),
            "succeeded": succeeded,
            "failed": failed,
            "mode": mode or "default",
        })
    except Exception as e:
        logger.error(f"Generate batch-single-channel error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/metrics")
def get_metrics():
    """Return in-memory analytics metrics for the dashboard.

    Category: Analytics

    Response:
    {
        "generate_count": 47,
        "approve_count": 12,
        "edit_count": 8,
        "daily_generates": {"2026-04-01": 12, ...},
        "feedback_summary": {"twitter": 3, "email_newsletter": 2}
    }
    """
    all_fb = get_all_feedback()
    feedback_summary = {ch: len(records) for ch, records in all_fb.items()}
    with _metrics_lock:
        return jsonify({
            "generate_count": _metrics["generate_count"],
            "approve_count": _metrics["approve_count"],
            "edit_count": _metrics["edit_count"],
            "daily_generates": dict(_metrics["daily_generates"]),
            "feedback_summary": feedback_summary,
        })


@app.route("/api/content/combine", methods=["POST"])
def combine_content_endpoint():
    """Combine multiple content items into one formatted document for a channel.

    Category: Content Generation

    Request Body:
    {
        "channel": "notion_monthly",
        "items": [{"feature_title": "...", "content": "..."}, ...]
    }

    Response:
    {
        "combined_content": "...",
        "format": "markdown"
    }
    """
    data = request.get_json() or {}
    channel = data.get("channel", "")
    items = data.get("items", [])

    if not items or not isinstance(items, list):
        return jsonify({"error": "items is required and must be a list"}), 400

    combined_parts = []
    fmt = "markdown"

    if channel == "notion_monthly":
        combined_parts.append("# Product Updates - Monthly Meeting\n")
        for item in items:
            combined_parts.append(item.get("content", ""))
            combined_parts.append("\n---\n")
    elif channel == "email_newsletter":
        for i, item in enumerate(items):
            if i > 0:
                combined_parts.append("\n---\n")
            combined_parts.append(item.get("content", ""))
    elif channel in ("email_short", "email_medium", "email_long", "email_standalone"):
        for item in items:
            combined_parts.append(item.get("content", ""))
            combined_parts.append("\n\n")
        fmt = "plaintext"
    elif channel == "twitter":
        for i, item in enumerate(items):
            combined_parts.append(f"**Tweet {i+1}: {item.get('feature_title', '')}**\n")
            combined_parts.append(item.get("content", ""))
            combined_parts.append("\n\n")
    elif channel == "linkedin":
        for item in items:
            combined_parts.append(item.get("content", ""))
            combined_parts.append("\n\n---\n\n")
    elif channel == "inapp":
        for i, item in enumerate(items):
            combined_parts.append(f"**Announcement {i+1}**\n")
            combined_parts.append(item.get("content", ""))
            combined_parts.append("\n\n")
    elif channel == "slack_internal":
        for item in items:
            title = item.get("feature_title", "")
            combined_parts.append(f":rocket: *{title}*\n")
            combined_parts.append(item.get("content", ""))
            combined_parts.append("\n\n")
        fmt = "plaintext"
    elif channel == "article_hmc":
        for item in items:
            combined_parts.append(item.get("content", ""))
            combined_parts.append("\n\n---\n\n")
    else:
        for item in items:
            combined_parts.append(f"## {item.get('feature_title', '')}\n")
            combined_parts.append(item.get("content", ""))
            combined_parts.append("\n\n")

    combined = "".join(combined_parts).rstrip("\n- ")

    return jsonify({
        "combined_content": combined,
        "format": fmt,
    })


@app.route("/api/channels")
@app.route("/api/test/channels")
def test_channels():
    """List all channel configurations.

    Category: Channels

    Response:
    {
        "channels": [
            {"key": "twitter", "display_name": "X / Twitter", "description": "...", "max_chars": 600, "enabled": true}
        ],
        "total": 7
    }
    """
    from ai.channel_configs import CHANNEL_CONFIGS
    channels = [
        {
            "key": k,
            "display_name": v["display_name"],
            "description": v.get("description", ""),
            "max_chars": v.get("max_chars"),
            "enabled": v.get("enabled", False),
        }
        for k, v in CHANNEL_CONFIGS.items()
    ]
    return jsonify({"channels": channels, "total": len(channels)})


@app.route("/api/features/debug")
def features_debug():
    """Debug endpoint showing full pipeline diagnostics.

    Category: System

    Query Params: ?days=30&refresh=false

    Response: Pipeline stats including Slack messages, extracted features, Asana matches, parse errors.
    """
    days = request.args.get("days", default=30, type=int)
    force_refresh = request.args.get("refresh", default="false").lower() == "true"
    try:
        result = _get_slack_first_features(days=days, force_refresh=force_refresh)
        features = result["features"]
        debug = result["debug"]

        source_breakdown = {"slack+asana": 0, "slack_only": 0, "asana_only": 0}
        for f in features:
            src = f.get("source", "unknown")
            source_breakdown[src] = source_breakdown.get(src, 0) + 1

        return jsonify({
            "pipeline": "slack-first",
            "days": days,
            "total_slack_messages": debug.get("total_messages", 0),
            "total_features_extracted": debug.get("total_features", 0),
            "total_features_final": debug.get("total_features_final", 0),
            "source_breakdown": source_breakdown,
            "asana_matches": debug.get("asana_matches", {}),
            "asana_only_count": debug.get("asana_only_count", 0),
            "skipped_messages": debug.get("skipped_messages", 0),
            "parse_errors_count": debug.get("parse_errors", 0) if isinstance(debug.get("parse_errors"), int) else len(debug.get("parse_errors", [])),
            "skipped_details": debug.get("skipped", [])[:20],
            "parse_error_details": (debug.get("parse_errors", []) if isinstance(debug.get("parse_errors"), list) else [])[:20],
        })
    except Exception as e:
        logger.error(f"Debug endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug/slack-links")
def debug_slack_links():
    """Debug endpoint showing raw Slack message links.

    Category: System

    Response: Array of messages with URLs extracted.
    """
    slack_source = SOURCE_REGISTRY["slack"]
    try:
        from sources.slack_source import _extract_links, _clean_slack_text
        client = slack_source._get_client()
        result = client.conversations_history(
            channel=slack_source.channel_id,
            limit=50,
        )
        messages = []
        for msg in result.get("messages", []):
            raw_text = msg.get("text", "")
            urls = _extract_links(raw_text)
            messages.append({
                "message_ts": msg.get("ts", ""),
                "message_preview": _clean_slack_text(raw_text)[:100],
                "all_urls": urls,
            })
        return jsonify(messages)
    except Exception as e:
        logger.error(f"Debug slack-links error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/features/<source_type>/<feature_id>")
def unified_detail(source_type, feature_id):
    """Get detailed context for a specific feature from any source.

    Category: Sources

    Response: Feature context object with title, description, metadata.
    """
    if source_type not in SOURCE_REGISTRY:
        return jsonify({"error": f"Unknown source: {source_type}"}), 404
    source = SOURCE_REGISTRY[source_type]
    try:
        ctx = source.get_feature_context(feature_id)
        return jsonify(ctx.to_dict())
    except Exception as e:
        logger.error(f"{source_type} detail error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/classify")
def test_classify():
    """Classify a hardcoded sample feature (Audience Overlap Tool).

    Category: Testing

    Response: {"sample_feature": {...}, "classification": {...}}
    """
    sample = {
        "id": "test-classify-001",
        "title": "New Artist Audience Overlap Tool",
        "description": "We've built a new tool that lets artists and managers compare their audience demographics with other artists. This helps identify collaboration opportunities, understand fan crossover, and plan tour routing based on shared audience geography. Available in the Artist Profile section under the new Audience Insights tab.",
        "release_status": True,
        "release_date": "2026-03-28",
        "reactions_breakdown": {"rocket": 5, "fire": 3, "heart": 2},
        "total_reactions": 10,
        "urgency_score": None,
    }
    classification = classify_feature(sample)
    return jsonify({"sample_feature": sample, "classification": classification})


@app.route("/api/test/generate")
def test_generate_full():
    """Full pipeline test: classify a sample feature then generate content for all channels.

    Category: Testing

    Response: {"feature": {...}, "classification": {...}, "generated_content": {"twitter": {...}, ...}}
    """
    sample = {
        "id": "test-001",
        "title": "New Artist Audience Overlap Tool",
        "description": "We've built a new tool that lets artists and managers compare their audience demographics with other artists. This helps identify collaboration opportunities, understand fan crossover, and plan tour routing based on shared audience geography. Available in the Artist Profile section under the new 'Audience Insights' tab. The tool shows percentage overlap across Spotify listeners, Instagram followers, and YouTube subscribers, with geographic heatmaps for the top 20 shared cities.",
        "release_status": True,
        "release_date": "2026-03-28",
        "reactions_breakdown": [
            {"name": "rocket", "count": 5},
            {"name": "fire", "count": 3},
            {"name": "heart", "count": 2},
        ],
        "total_reactions": 10,
        "urgency_score": None,
    }
    try:
        print("[test/generate] Classifying sample feature...", flush=True)
        classification = classify_feature(sample)
        sample["classification"] = classification

        print(f"[test/generate] Classification: score={classification.get('importance_score')}, channels={classification.get('recommended_channels')}", flush=True)
        print("[test/generate] Generating content for all enabled channels...", flush=True)
        content = generate_all_channels(sample)

        return jsonify({
            "feature": sample,
            "classification": classification,
            "generated_content": content,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.error(f"Test generate error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/generate-twitter")
def test_generate_twitter():
    """Generate a sample tweet for the Audience Overlap Tool feature.

    Category: Testing

    Response: {"feature": {...}, "twitter_content": {"content": "...", "char_count": 142, ...}}
    """
    sample = {
        "id": "test-gen-001",
        "title": "New Artist Audience Overlap Tool",
        "description": "We've built a new tool that lets artists and managers compare their audience demographics with other artists. This helps identify collaboration opportunities, understand fan crossover, and plan tour routing based on shared audience geography. Available in the Artist Profile section under the new Audience Insights tab.",
        "release_status": True,
        "release_date": "2026-03-28",
        "reactions_breakdown": [
            {"name": "rocket", "count": 5},
            {"name": "fire", "count": 3},
            {"name": "heart", "count": 2},
        ],
        "total_reactions": 10,
        "urgency_score": None,
    }
    try:
        result = generate_for_channel(sample, "twitter")
        return jsonify({"feature": sample, "twitter_content": result})
    except Exception as e:
        logger.error(f"Test generate-twitter error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/generate-samples")
def test_generate_samples():
    """Generate content for 'Playlists to Pitch' across all enabled channels.

    Category: Testing

    Response: {"feature": {...}, "generated_content": {"twitter": {...}, "email_newsletter": {...}, ...}}
    """
    sample = {
        "id": "test-samples-001",
        "title": "Playlists to Pitch: Personalized Playlist Recommendations",
        "description": "A new feature on Track Pages that recommends playlists tailored to your specific track. Each recommendation includes a Fit Analysis explaining why the playlist is a strong match, key metrics like Added Reach and Added Streams, and direct links to reach out to playlist curators.",
        "release_status": True,
        "release_date": "2026-03-30",
        "reactions_breakdown": [
            {"name": "rocket", "count": 8},
            {"name": "fire", "count": 5},
            {"name": "heart", "count": 4},
        ],
        "total_reactions": 17,
        "urgency_score": None,
    }
    try:
        results = generate_all_channels(sample)
        return jsonify({"feature": sample, "generated_content": results})
    except Exception as e:
        logger.error(f"Test generate-samples error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/raw-fields")
def test_raw_fields():
    """Show all raw fields from Asana enriched with Slack data.

    Category: Testing

    Response: {"features": [{...}], "total": 356}
    """
    try:
        enriched = _get_enriched_features()
        return jsonify({"features": enriched, "total": len(enriched)})
    except Exception as e:
        logger.error(f"Test raw-fields error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/feedback-loop")
def test_feedback_loop():
    """Demo the feedback loop: generate, save feedback, generate again with learning.

    Category: Testing

    Response: {"first_generation": {...}, "feedback_saved": {...}, "second_generation_after_learning": {...}}
    """
    feature_1 = {
        "id": "test-gen-001",
        "title": "New Artist Audience Overlap Tool",
        "description": "We've built a new tool that lets artists and managers compare their audience demographics with other artists. This helps identify collaboration opportunities, understand fan crossover, and plan tour routing based on shared audience geography. Available in the Artist Profile section under the new Audience Insights tab.",
        "release_status": True,
        "release_date": "2026-03-28",
        "reactions_breakdown": [
            {"name": "rocket", "count": 5},
            {"name": "fire", "count": 3},
            {"name": "heart", "count": 2},
        ],
        "total_reactions": 10,
        "urgency_score": None,
    }
    feature_2 = {
        "id": "test-002",
        "title": "Playlist Placement Tracker",
        "description": "Track when and where your songs get added to editorial and algorithmic playlists across Spotify, Apple Music, and Deezer. See historical placement data and get alerts for new additions.",
        "release_status": True,
        "release_date": "2026-03-30",
    }

    try:
        first_draft = generate_for_channel(feature_1, "twitter")

        approved_text = "Compare your fanbase with any artist. Our new Audience Overlap tool shows listener crossover across Spotify, Instagram, and YouTube. #Chartmetric #AudienceData"
        feedback_record = save_feedback(
            channel="twitter",
            feature_title=feature_1["title"],
            original_draft=first_draft["content"],
            approved_draft=approved_text,
            feedback_note="Shorter, more direct, no questions, just state what it does clearly",
        )

        second_draft = generate_for_channel(feature_2, "twitter")

        return jsonify({
            "first_generation": first_draft,
            "feedback_saved": feedback_record,
            "second_generation_after_learning": second_draft,
        })
    except Exception as e:
        logger.error(f"Test feedback-loop error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/test/claude")
def test_claude():
    """Test Claude API connection with a simple prompt.

    Category: Testing

    Response: {"success": true, "content": "Hello! ...", "error": null}
    """
    from ai.claude_client import generate_content
    result = generate_content("You are a helpful assistant.", "Say hello in one sentence.", max_tokens=64)
    return jsonify(result)


@app.route("/test/review")
def test_review():
    """Content review page for testing generated marketing content.

    Category: Testing

    Response: HTML page with feature selector and content generation/review UI.
    """
    return render_template("review.html")


@app.route("/api/docs")
def api_docs():
    """Auto-generated API documentation page.

    Category: System

    Response: HTML page listing all endpoints grouped by category.
    """
    categories = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.rule.startswith("/static"):
            continue
        endpoint_func = app.view_functions.get(rule.endpoint)
        if not endpoint_func:
            continue

        docstring = endpoint_func.__doc__ or ""
        methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
        if not methods:
            continue

        category = "Other"
        description_lines = []
        body_lines = []
        in_body = False

        for line in docstring.split("\n"):
            stripped = line.strip()
            if stripped.startswith("Category:"):
                category = stripped.replace("Category:", "").strip()
                in_body = False
            elif stripped.startswith("Request Body:") or stripped.startswith("Response:") or stripped.startswith("Query Params:"):
                in_body = True
                body_lines.append(stripped)
            elif in_body:
                body_lines.append(line.rstrip())
            elif stripped and not in_body:
                description_lines.append(stripped)

        description = " ".join(description_lines).strip()
        body_block = "\n".join(body_lines).strip()

        if category not in categories:
            categories[category] = []

        categories[category].append({
            "methods": methods,
            "url": rule.rule,
            "description": description,
            "body_block": body_block,
        })

    category_order = ["System", "Sources", "Channels", "Content Generation", "Few-Shot Examples", "Feedback Loop", "Testing", "Other"]
    sorted_categories = []
    for cat in category_order:
        if cat in categories:
            sorted_categories.append((cat, categories[cat]))
    for cat in sorted(categories.keys()):
        if cat not in category_order:
            sorted_categories.append((cat, categories[cat]))

    html_parts = ["""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Amplify API Documentation</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #e1e4e8; line-height: 1.6; padding: 2rem; max-width: 960px; margin: 0 auto; }
h1 { font-size: 2rem; color: #fff; margin-bottom: 0.25rem; }
.subtitle { color: #8b949e; font-size: 1.1rem; margin-bottom: 2rem; }
h2 { font-size: 1.3rem; color: #58a6ff; margin: 2rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid #21262d; }
.endpoint { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 0.75rem; }
.endpoint-header { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
.method { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.75rem; font-weight: 700; padding: 0.2rem 0.5rem; border-radius: 4px; }
.method-GET { background: #1f6feb33; color: #58a6ff; }
.method-POST { background: #2ea04333; color: #3fb950; }
.method-DELETE { background: #f8514933; color: #f85149; }
.url { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.9rem; color: #f0f3f6; }
.url a { color: #f0f3f6; text-decoration: none; }
.url a:hover { text-decoration: underline; color: #58a6ff; }
.desc { color: #8b949e; font-size: 0.9rem; margin-top: 0.5rem; }
.body-block { background: #0d1117; border: 1px solid #21262d; border-radius: 6px; padding: 0.75rem 1rem; margin-top: 0.75rem; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.8rem; color: #c9d1d9; white-space: pre-wrap; overflow-x: auto; }
.count { color: #484f58; font-size: 0.85rem; margin-left: 0.5rem; }
</style>
</head>
<body>
<h1>Amplify API Documentation</h1>
<p class="subtitle">Product Marketing Autopilot for Chartmetric</p>
"""]

    for cat_name, endpoints in sorted_categories:
        html_parts.append(f'<h2>{html.escape(cat_name)} <span class="count">({len(endpoints)})</span></h2>')
        for ep in endpoints:
            method_badges = " ".join(
                f'<span class="method method-{m}">{m}</span>' for m in ep["methods"]
            )
            url_escaped = html.escape(ep["url"])
            is_get = ep["methods"] == ["GET"]
            url_display = f'<a href="{url_escaped}">{url_escaped}</a>' if is_get else url_escaped

            html_parts.append(f'<div class="endpoint">')
            html_parts.append(f'  <div class="endpoint-header">{method_badges} <span class="url">{url_display}</span></div>')
            if ep["description"]:
                html_parts.append(f'  <div class="desc">{html.escape(ep["description"])}</div>')
            if ep["body_block"]:
                html_parts.append(f'  <div class="body-block">{html.escape(ep["body_block"])}</div>')
            html_parts.append(f'</div>')

    html_parts.append("</body></html>")
    return "\n".join(html_parts)


if __name__ == "__main__":
    import threading
    import time

    _shutdown = False

    def handle_sigterm(*args):
        global _shutdown
        _shutdown = True
        logger.info("Received SIGTERM, shutting down gracefully")
        sys.stdout.flush()
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    def keep_alive():
        while not _shutdown:
            logger.info("Amplify heartbeat \u2014 server alive")
            sys.stdout.flush()
            time.sleep(30)

    heartbeat = threading.Thread(target=keep_alive, daemon=True)
    heartbeat.start()

    port = config.PORT
    logger.info(f"Amplify starting on port {port}")
    print(f"Amplify starting on port {port}", flush=True)

    print("\n=== Registered Routes ===", flush=True)
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        print(f"  {methods:8s} {rule.rule}", flush=True)
    print("=========================\n", flush=True)
    sys.stdout.flush()
    try:
        from waitress import serve
        logger.info(f"Starting waitress on port {port}")
        serve(
            app,
            host="0.0.0.0",
            port=port,
            _quiet=False,
            channel_timeout=300,
            recv_bytes=65536,
            threads=8,
        )
    except ImportError:
        logger.warning("waitress not available, falling back to Flask dev server")
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
