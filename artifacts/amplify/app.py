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
from ai.classification_overrides import save_override as save_classification_override, get_overrides as get_classification_overrides
from datetime import datetime, timezone

app = Flask(__name__, template_folder="templates")
app.secret_key = config.SESSION_SECRET

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

    save_feedback(channel, feature_title, original_draft, approved_draft, feedback_note)
    print(f"[approve] Approved draft for '{feature_title}' on channel '{channel}'", flush=True)
    return jsonify({"success": True, "message": "Approved and feedback saved for future learning"})


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

    try:
        print(f"[generate/single] Regenerating '{feature.get('title', 'unknown')}' for channel '{channel}' (feedback: {bool(feedback)})", flush=True)
        result = generate_for_channel(
            feature, channel,
            custom_instructions=custom_instructions or None,
            feedback=feedback or None,
            skip_cache=True,
        )
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

    if not features or not isinstance(features, list):
        return jsonify({"error": "features is required and must be a list"}), 400
    if not channel or not isinstance(channel, str):
        return jsonify({"error": "channel is required and must be a string"}), 400

    from ai.channel_configs import CHANNEL_CONFIGS
    if channel not in CHANNEL_CONFIGS:
        return jsonify({"error": f"Unknown channel: {channel}"}), 400

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        config = CHANNEL_CONFIGS[channel]
        print(f"[generate/batch-single] Generating {channel} for {len(features)} features", flush=True)

        def gen_one(feature):
            result = generate_for_channel(feature, channel, custom_instructions=custom_instructions or None)
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

        return jsonify({
            "channel": channel,
            "channel_display_name": config["display_name"],
            "results": results,
            "total": len(features),
            "succeeded": succeeded,
            "failed": failed,
        })
    except Exception as e:
        logger.error(f"Generate batch-single-channel error: {e}")
        return jsonify({"error": str(e)}), 500


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
    elif channel == "email_standalone":
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
    from waitress import serve
    serve(
        app,
        host="0.0.0.0",
        port=port,
        _quiet=False,
        channel_timeout=300,
        recv_bytes=65536,
        threads=8,
    )
