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
    has_sufficient_signal, _low_signal_result, _save_cache_to_disk,
    is_obviously_junk_title,
)
from ai.pre_filter import pre_filter_batch  # kept for backward compat, not used in main pipeline
from ai.generator import generate_for_channel, generate_all_channels, get_content_cache_index
from ai.few_shot_examples import FEW_SHOT_EXAMPLES
from ai.feedback_store import save_feedback, get_feedback_history, get_all_feedback, clear_feedback
from ai.publish_store import mark_published, save_image as save_publish_image, get_image as get_publish_image, remove_image as remove_publish_image, get_feature_state, get_all_published, save_video as save_publish_video, save_video_url as save_publish_video_url, get_video_path, get_video_thumb_path, list_feature_videos, delete_video as delete_publish_video, cleanup_orphan_videos
from ai.classification_overrides import save_override as save_classification_override, get_overrides as get_classification_overrides
from ai.feature_url_overrides import (
    save_url_override as save_feature_url_override,
    get_url_overrides as get_feature_url_overrides,
    get_url_override_for_feature,
    get_url_override_for_title,
)
from ai.feature_sets import save_set as save_feature_set, get_sets as get_feature_sets, delete_set as delete_feature_set
from datetime import datetime, timezone

_app_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(_app_dir, "templates"), static_folder=os.path.join(_app_dir, "static"))
app.secret_key = config.SESSION_SECRET
app.config["MAX_CONTENT_LENGTH"] = 75 * 1024 * 1024

# In-app announcements admin (Task #91): registers /announcements page +
# /api/admin/announcement* endpoints. Imported lazily to avoid disturbing the
# rest of the import graph if the blueprint module fails to load.
try:
    from announcements_routes import register as _register_announcements_admin
    _register_announcements_admin(app)
except Exception as _e:  # pragma: no cover - defensive
    print(f"[startup] announcements admin blueprint failed to register: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("amplify")


@app.errorhandler(Exception)
def _global_exception_handler(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    try:
        _payload_preview = ""
        if request.is_json:
            _j = request.get_json(silent=True) or {}
            _payload_preview = ", ".join(f"{k}={type(v).__name__}({len(v) if hasattr(v,'__len__') else v})" for k, v in list(_j.items())[:8])
    except Exception:
        _payload_preview = "<unreadable>"
    logger.exception(
        f"[unhandled] {request.method} {request.path} type={type(e).__name__} repr={e!r} "
        f"args={dict(request.args)} payload_keys={_payload_preview}"
    )
    return jsonify({"success": False, "error": f"Server error: {type(e).__name__}: {e}"}), 500

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
    return render_template("dashboard.html", artifact_id="")


@app.route("/artifact/<artifact_id>")
def dashboard_artifact(artifact_id: str):
    """Dashboard with a specific artifact (draft or published) deep-linked.

    The frontend reads `window._initialArtifactId` and auto-opens that
    artifact in the prep workflow. If the id doesn't exist, the frontend
    falls back to the dashboard home and shows a toast.
    """
    safe = "".join(ch for ch in (artifact_id or "") if ch.isalnum())[:64]
    return render_template("dashboard.html", artifact_id=safe)


@app.route("/__startup_log")
def startup_log():
    """Return contents of the production bootstrap log for deploy diagnostics."""
    path = os.environ.get("AMPLIFY_STARTUP_LOG", "/tmp/amplify_startup.log")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        return content, 200, {"Content-Type": "text/plain; charset=utf-8"}
    except FileNotFoundError:
        return f"no startup log at {path}", 404, {"Content-Type": "text/plain; charset=utf-8"}
    except Exception as e:
        return f"error reading {path}: {e}", 500, {"Content-Type": "text/plain; charset=utf-8"}


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

    # Manual entry path: caller provides explicit title (and optional description).
    # We accept this on the same endpoint so the frontend has a single ingestion
    # path regardless of whether the feature came from a URL or was hand-typed.
    manual_title = (data.get("title") or "").strip()
    if manual_title:
        from sources.manual_source import validate_manual_title
        title_error = validate_manual_title(manual_title)
        if title_error:
            return jsonify({"error": title_error}), 400
        manual_desc = (data.get("description") or "").strip()
        if len(manual_title) > 300:
            manual_title = manual_title[:300]
        if len(manual_desc) > 4000:
            manual_desc = manual_desc[:4000]
        feature = {
            "id": f"manual-{__import__('uuid').uuid4().hex[:12]}",
            "title": manual_title,
            "description": manual_desc,
            "source": "manual",
            "released": False,
            "asana_linked": False,
        }
        return jsonify({"source": "manual", "feature": feature})

    raw_input = (data.get("url") or "").strip()
    if not raw_input:
        return jsonify({"error": "url, text, or title is required"}), 400

    inputs = [line.strip() for line in raw_input.replace(",", "\n").split("\n") if line.strip()]
    results = []

    from sources.manual_source import validate_manual_title, LOW_QUALITY_TITLE_MESSAGE

    for item in inputs:
        try:
            result = _extract_feature_from_input(item)
            # Apply the low-quality title gate to any extracted feature, no
            # matter the source. Slack/Asana/GitHub URLs can still resolve to
            # tasks with garbage titles like "[Duplicate] xyz" or ",etc.", and
            # the same is true for plain-text fallbacks.
            if (
                isinstance(result, dict)
                and result.get("feature")
                and validate_manual_title(result["feature"].get("title", ""))
            ):
                results.append({
                    "source": "error",
                    "error": LOW_QUALITY_TITLE_MESSAGE,
                    "input": item[:200],
                })
                continue
            results.append(result)
        except Exception as e:
            logger.error(f"from-url extraction error for '{item[:80]}': {e}")
            results.append({"source": "error", "error": str(e), "input": item[:200]})

    if len(results) == 1:
        single = results[0]
        if single.get("source") == "error" and single.get("error") == LOW_QUALITY_TITLE_MESSAGE:
            return jsonify({"error": LOW_QUALITY_TITLE_MESSAGE}), 400
        return jsonify(single)
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
# Per-days-bucket condition vars used to coalesce simultaneous SYNC fetches
# of the same window. Without this, N concurrent requests for an uncached
# window each fire their own _run_pipeline_fetch in parallel, exhausting the
# Asana HTTP connection pool (max 40) and dragging cold loads from ~30s to
# ~5min.
_pipeline_inflight_lock = threading.Lock()
_pipeline_inflight_events: dict = {}
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

    # COALESCE: if another thread is already running the pipeline for this
    # bucket, wait for it instead of firing a parallel fetch (which would
    # multiply Asana load and exhaust the connection pool).
    with _pipeline_inflight_lock:
        ev = _pipeline_inflight_events.get(cache_key)
        is_leader = ev is None
        if is_leader:
            ev = threading.Event()
            _pipeline_inflight_events[cache_key] = ev

    if not is_leader:
        # Wait up to 6 minutes for the leader to finish, then return whatever
        # ended up in the cache. If nothing did, fall through and fetch.
        ev.wait(timeout=360)
        cached = _pipeline_cache.get(cache_key)
        if cached is not None:
            return {"features": cached["features"], "debug": cached["debug"]}
        # Leader failed and cache is still empty; do a fresh attempt ourselves.
        return _run_pipeline_fetch(days=days)

    try:
        return _run_pipeline_fetch(days=days)
    finally:
        with _pipeline_inflight_lock:
            _pipeline_inflight_events.pop(cache_key, None)
        ev.set()


def _get_enriched_features():
    result = _get_slack_first_features()
    return result["features"]


def _apply_feature_url_overrides(features: list) -> None:
    """For each feature, if a marketer has previously corrected its
    Chartmetric URL, replace `chartmetric_url` with the corrected value
    and attach a `chartmetric_url_override` block so the frontend can
    render the human-corrected indicator + reason tooltip."""
    if not features:
        return
    for f in features:
        try:
            fid = f.get("id", "")
            entry = get_url_override_for_feature(fid)
            if not entry:
                # Fall back to a title-match override so brand-new feature
                # IDs still benefit from prior corrections of the same feature.
                entry = get_url_override_for_title(f.get("title", ""))
            if not entry or not entry.get("new_url"):
                continue
            original_url = f.get("chartmetric_url") or entry.get("original_url") or ""
            f["chartmetric_url"] = entry["new_url"]
            f["chartmetric_url_override"] = {
                "original_url": original_url,
                "new_url": entry["new_url"],
                "reason": entry.get("reason", ""),
                "timestamp": entry.get("timestamp", ""),
                "matched_by": "feature_id" if entry.get("feature_id") == fid else "title",
            }
        except Exception as e:
            logger.warning(f"[url-override] failed to apply override to feature {f.get('id')}: {e}")


# ---------------------------------------------------------------------------
# Email draft / artifact store.
#
# Persists in Postgres (DATABASE_URL) so artifacts survive deploy restarts on
# Replit (the container filesystem is wiped on redeploy). Falls back to the
# legacy on-disk JSON file when no DATABASE_URL is configured (local dev) and
# performs a one-time migration from JSON -> DB on first DB write so existing
# drafts aren't lost.
# ---------------------------------------------------------------------------
_email_drafts_lock = threading.RLock()  # re-entrant: mark-downloaded holds it across load+_upsert_email_draft (which re-acquires for the JSON write)
_EMAIL_DRAFTS_PATH = os.path.join(_FEATURES_CACHE_DIR, ".email_drafts.json")
_EMAIL_DRAFT_MAX_BYTES = 8 * 1024 * 1024  # 8 MB safety cap per draft
_EMAIL_DRAFTS_TOTAL_MAX_BYTES = 64 * 1024 * 1024  # 64 MB total store cap

# Daily on-disk JSON snapshots of the full email_drafts table. Written next to
# `_EMAIL_DRAFTS_PATH` (e.g. `.email_drafts.snapshot-YYYYMMDD.json`) so that a
# future accidental wipe of the Postgres table is recoverable from disk
# without needing Neon PITR. Triggered lazily on the first successful
# `_load_email_drafts()` of the day; older snapshots beyond the retention
# window are pruned best-effort.
_EMAIL_DRAFTS_SNAPSHOT_PREFIX = ".email_drafts.snapshot-"
_EMAIL_DRAFTS_SNAPSHOT_RETENTION_DAYS = 14

_DRAFTS_DB_URL = os.environ.get("DATABASE_URL", "").strip()
_drafts_db_initialized = False


class EmailDraftsUnavailable(RuntimeError):
    """Raised when the configured Postgres drafts store is temporarily unreachable.

    Routes catch this and respond with HTTP 503 instead of letting the
    underlying read silently look like "the table is empty" (which previously
    let downstream "save the full list" calls wipe every other row).
    """


def _drafts_db_conn():
    """Open a short-lived psycopg2 connection. Returns None if unavailable."""
    if not _DRAFTS_DB_URL:
        return None
    try:
        import psycopg2  # type: ignore
        return psycopg2.connect(_DRAFTS_DB_URL, connect_timeout=5)
    except Exception as e:
        logger.warning(f"[email-drafts] DB connect failed: {e}")
        return None


def _ensure_drafts_table(conn) -> bool:
    """Create the email_drafts table on first use; idempotent."""
    global _drafts_db_initialized
    if _drafts_db_initialized:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS email_drafts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    ts DOUBLE PRECISION NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    last_published_ts DOUBLE PRECISION DEFAULT 0,
                    last_recipient_count INTEGER DEFAULT 0,
                    snapshot JSONB NOT NULL
                )
                """
            )
            # `category` is added separately so existing deployments pick it
            # up without a manual migration. Nullable on purpose: the save UI
            # doesn't expose it yet (a future task), but the column round-
            # trips end-to-end so writers can start populating it later
            # without another schema change.
            cur.execute(
                "ALTER TABLE email_drafts ADD COLUMN IF NOT EXISTS category TEXT"
            )
            # `downloaded_ts` records the most recent time the user clicked
            # Download HTML for this draft. NULL when never downloaded so the
            # Downloaded tab in the My Content modal can filter on
            # `IS NOT NULL` cheaply.
            cur.execute(
                "ALTER TABLE email_drafts ADD COLUMN IF NOT EXISTS downloaded_ts DOUBLE PRECISION"
            )
            conn.commit()
        _drafts_db_initialized = True
        # One-time migration from on-disk JSON -> DB so any drafts saved
        # before we moved to Postgres are preserved.
        try:
            if os.path.exists(_EMAIL_DRAFTS_PATH):
                with open(_EMAIL_DRAFTS_PATH, "r") as f:
                    legacy = json.load(f)
                drafts = (legacy or {}).get("drafts") or []
                if drafts:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM email_drafts")
                        (existing,) = cur.fetchone()
                    if existing == 0:
                        with conn.cursor() as cur:
                            for d in drafts:
                                _legacy_dl_ts = d.get("downloaded_ts")
                                cur.execute(
                                    """
                                    INSERT INTO email_drafts (id, name, ts, status, last_published_ts, last_recipient_count, snapshot, category, downloaded_ts)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                    ON CONFLICT (id) DO NOTHING
                                    """,
                                    (
                                        d.get("id"),
                                        d.get("name") or "Untitled draft",
                                        float(d.get("ts") or 0),
                                        d.get("status") or "draft",
                                        float(d.get("last_published_ts") or 0),
                                        int(d.get("last_recipient_count") or 0),
                                        json.dumps(d.get("snapshot") or {}),
                                        d.get("category") or None,
                                        float(_legacy_dl_ts) if _legacy_dl_ts else None,
                                    ),
                                )
                            conn.commit()
                        logger.info(f"[email-drafts] Migrated {len(drafts)} legacy drafts JSON -> Postgres")
        except Exception as e:
            logger.warning(f"[email-drafts] Legacy JSON migration skipped: {e}")
        return True
    except Exception as e:
        logger.warning(f"[email-drafts] CREATE TABLE failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _row_to_draft(row) -> dict:
    """Map an email_drafts row tuple to the on-disk-style draft dict."""
    snap = row[6]
    if isinstance(snap, (bytes, str)):
        try:
            snap = json.loads(snap)
        except Exception:
            snap = {}
    # `category` and `downloaded_ts` may be missing on rows written before
    # those columns existed, so guard each index lookup.
    category = row[7] if len(row) > 7 else None
    raw_dl_ts = row[8] if len(row) > 8 else None
    return {
        "id": row[0],
        "name": row[1],
        "ts": float(row[2] or 0),
        "status": row[3] or "draft",
        "last_published_ts": float(row[4] or 0),
        "last_recipient_count": int(row[5] or 0),
        "snapshot": snap or {},
        "category": category,
        "downloaded_ts": float(raw_dl_ts) if raw_dl_ts else None,
    }


def _list_existing_drafts_snapshots() -> list:
    """Return on-disk snapshot filenames sorted newest-first (YYYYMMDD).

    Snapshot filenames embed the date in `YYYYMMDD` form, which sorts
    the same lexicographically and chronologically. We use this both
    for snapshot-based recovery and for retention pruning.
    """
    try:
        existing = [
            fn for fn in os.listdir(_FEATURES_CACHE_DIR)
            if fn.startswith(_EMAIL_DRAFTS_SNAPSHOT_PREFIX) and fn.endswith(".json")
        ]
    except Exception:
        return []
    existing.sort(reverse=True)
    return existing


def _read_drafts_snapshot_file(fn: str) -> list:
    """Best-effort read of one snapshot file. Returns the drafts list or []."""
    try:
        path = os.path.join(_FEATURES_CACHE_DIR, fn)
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("drafts"), list):
            return data["drafts"]
    except Exception as e:
        logger.warning(f"[email-drafts] snapshot read failed for {fn}: {e}")
    return []


def _find_latest_nonempty_snapshot() -> tuple:
    """Return `(filename, drafts_list)` for the newest on-disk snapshot
    that actually contains drafts, or `(None, [])` if none exist.

    This is the recovery source when Postgres comes back with zero rows
    after an accidental wipe (drizzle push --force, manual TRUNCATE,
    botched migration, etc.) — see `_load_email_drafts` below.
    """
    for fn in _list_existing_drafts_snapshots():
        drafts = _read_drafts_snapshot_file(fn)
        if drafts:
            return fn, drafts
    return None, []


def _maybe_write_daily_drafts_snapshot(data: dict) -> None:
    """Write today's full-table drafts snapshot to disk if it is safe to do so.

    Lazy trigger on the first successful `_load_email_drafts()` of the day.
    Best-effort: failures are logged but never propagate to the caller (we
    must not block a normal load on snapshot I/O). Older snapshot files
    beyond `_EMAIL_DRAFTS_SNAPSHOT_RETENTION_DAYS` are pruned in the same
    pass so disk usage stays bounded.

    Refusal rules (the original wipe was made unrecoverable by writing an
    empty same-day snapshot RIGHT AFTER the table got dropped, poisoning
    the only on-disk recovery source):

    1. Never overwrite a non-empty same-day snapshot with an empty one.
    2. Never write the very first snapshot of the day if the payload is
       empty AND any prior non-empty snapshot exists on disk — that
       would leave a stale poison-pill snapshot tomorrow once the older
       ones get pruned.
    3. Writing an empty snapshot is fine when there is genuinely no
       prior data anywhere (fresh install).
    """
    try:
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        snap_path = os.path.join(
            _FEATURES_CACHE_DIR, f"{_EMAIL_DRAFTS_SNAPSHOT_PREFIX}{date_str}.json"
        )
        new_drafts = (data or {}).get("drafts") or []
        if os.path.exists(snap_path):
            existing_drafts = _read_drafts_snapshot_file(
                os.path.basename(snap_path)
            )
            # Rule 1a: never replace a non-empty same-day snapshot with
            # an empty payload (this is exactly how the original wipe
            # destroyed the only on-disk recovery source).
            if existing_drafts and not new_drafts:
                logger.warning(
                    "[email-drafts] refusing to overwrite non-empty same-day "
                    "snapshot with empty payload"
                )
                return
            # Rule 1b: same-day file already captures non-empty data and
            # the new payload is also non-empty -- preserve the existing
            # one. The daily file is meant to capture the first
            # successful load of the day; subsequent loads don't need to
            # rewrite it (and `_upsert_email_draft` already keeps
            # Postgres up to date for individual changes).
            if existing_drafts and new_drafts:
                return
            # Rule 1c: existing snapshot is empty AND new payload is
            # also empty -- nothing to do. Refusing here also avoids a
            # pointless I/O round-trip.
            if not existing_drafts and not new_drafts:
                return
            # Fall through: existing snapshot is the empty poison-pill
            # left over from a previous load that ran while the table
            # was wiped, AND we now have real drafts. Overwrite it so
            # tomorrow's recovery has something to read from.
        # Rule 2: don't write today's empty snapshot when older non-empty
        # snapshots exist; if we did, retention pruning could eventually
        # leave only empty snapshots and break recovery.
        if not new_drafts:
            for fn in _list_existing_drafts_snapshots():
                if _read_drafts_snapshot_file(fn):
                    logger.warning(
                        "[email-drafts] refusing to write empty snapshot "
                        "while older non-empty snapshot %s still exists",
                        fn,
                    )
                    return
        try:
            os.makedirs(_FEATURES_CACHE_DIR, exist_ok=True)
        except Exception:
            pass
        import uuid as _uuid
        payload = {"drafts": new_drafts}
        tmp = snap_path + f".{_uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(tmp, snap_path)
        # Prune older snapshots beyond retention. Filenames sort
        # lexicographically the same way they sort by date because we use
        # YYYYMMDD, so a reverse sort puts the newest first.
        try:
            existing = _list_existing_drafts_snapshots()
            for old_fn in existing[_EMAIL_DRAFTS_SNAPSHOT_RETENTION_DAYS:]:
                try:
                    os.unlink(os.path.join(_FEATURES_CACHE_DIR, old_fn))
                except OSError:
                    pass
        except Exception as e:
            logger.warning(f"[email-drafts] snapshot prune failed: {e}")
    except Exception as e:
        logger.warning(f"[email-drafts] snapshot write skipped: {e}")


def _restore_drafts_from_snapshot(snap_drafts: list) -> list:
    """Re-insert snapshot drafts into Postgres after an accidental wipe.

    Uses `ON CONFLICT (id) DO NOTHING` so we never clobber a row that
    was legitimately re-saved between the wipe and this recovery (e.g.
    the user created a brand-new draft on a fresh table before the
    snapshot read fired). Returns the resulting full draft list (newest
    first) on success, or the in-memory snapshot list on failure so the
    caller can still render something instead of a blank My Content.
    """
    if not snap_drafts:
        return []
    conn = _drafts_db_conn()
    if conn is None:
        return list(snap_drafts)
    try:
        if not _ensure_drafts_table(conn):
            return list(snap_drafts)
        with conn.cursor() as cur:
            for d in snap_drafts:
                if not isinstance(d, dict):
                    continue
                _dl_ts = d.get("downloaded_ts")
                cur.execute(
                    """
                    INSERT INTO email_drafts (id, name, ts, status, last_published_ts, last_recipient_count, snapshot, category, downloaded_ts)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        d.get("id"),
                        d.get("name") or "Untitled draft",
                        float(d.get("ts") or 0),
                        d.get("status") or "draft",
                        float(d.get("last_published_ts") or 0),
                        int(d.get("last_recipient_count") or 0),
                        json.dumps(d.get("snapshot") or {}),
                        d.get("category") or None,
                        float(_dl_ts) if _dl_ts else None,
                    ),
                )
            conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, ts, status, last_published_ts, last_recipient_count, snapshot, category, downloaded_ts FROM email_drafts ORDER BY ts DESC"
            )
            rows = cur.fetchall()
        return [_row_to_draft(r) for r in rows]
    except Exception as e:
        logger.error(f"[email-drafts] snapshot restore failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return list(snap_drafts)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_email_drafts() -> dict:
    """Load all drafts. Postgres-first; JSON fallback only when no DB is configured.

    Raises `EmailDraftsUnavailable` when `DATABASE_URL` is set but the
    Postgres store is unreachable (connect timeout, `_ensure_drafts_table`
    failure, or a `SELECT` exception). Routes catch that and return 503 so a
    transient DB blip can never silently look like "the table is empty" --
    which is exactly the failure mode that let prior versions of this code
    wipe every draft via downstream "save the full list" calls.

    Snapshot-based recovery: if the table is reachable but returns ZERO
    rows AND we have a non-empty on-disk snapshot, we treat that as an
    accidental wipe (drizzle push --force dropping the table is the
    historical culprit -- see `scripts/post-merge.sh`) and restore the
    snapshot back into Postgres before returning. This makes
    `_maybe_write_daily_drafts_snapshot` actually useful: previously it
    wrote snapshots that nothing ever read.
    """
    if _DRAFTS_DB_URL:
        conn = _drafts_db_conn()
        if conn is None:
            raise EmailDraftsUnavailable("Postgres drafts store: connect failed")
        try:
            if not _ensure_drafts_table(conn):
                raise EmailDraftsUnavailable("Postgres drafts store: CREATE TABLE failed")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, ts, status, last_published_ts, last_recipient_count, snapshot, category, downloaded_ts FROM email_drafts ORDER BY ts DESC"
                )
                rows = cur.fetchall()
            result = {"drafts": [_row_to_draft(r) for r in rows]}
        except EmailDraftsUnavailable:
            raise
        except Exception as e:
            logger.error(f"[email-drafts] DB load failed: {e}")
            raise EmailDraftsUnavailable(f"Postgres drafts store: {e}") from e
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not result["drafts"]:
            # Postgres came back empty. If we have a non-empty snapshot
            # on disk, the table was almost certainly wiped (the only
            # legitimate "no rows" case is a brand-new install, which
            # also has no snapshot). Restore from the most-recent
            # non-empty snapshot, persist it back into Postgres so a
            # subsequent crash doesn't lose it again, and return the
            # restored draftss to the caller as if nothing happened.
            try:
                snap_fn, snap_drafts = _find_latest_nonempty_snapshot()
            except Exception as _e:
                snap_fn, snap_drafts = None, []
            if snap_drafts:
                logger.warning(
                    "[email-drafts] table came back empty; restoring %d draft(s) "
                    "from snapshot %s",
                    len(snap_drafts), snap_fn,
                )
                restored = _restore_drafts_from_snapshot(snap_drafts)
                result = {"drafts": restored}
        _maybe_write_daily_drafts_snapshot(result)
        return result
    # JSON fallback (Postgres genuinely not configured for this environment).
    result = {"drafts": []}
    try:
        if os.path.exists(_EMAIL_DRAFTS_PATH):
            with open(_EMAIL_DRAFTS_PATH, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("drafts"), list):
                # Normalize: ensure every record exposes `category` and
                # `downloaded_ts` (as null when missing) so callers don't
                # have to special-case records that predate those columns.
                for d in data["drafts"]:
                    if isinstance(d, dict) and "category" not in d:
                        d["category"] = None
                    if isinstance(d, dict) and "downloaded_ts" not in d:
                        d["downloaded_ts"] = None
                result = data
    except Exception as e:
        logger.warning(f"[email-drafts] JSON load failed: {e}")
    _maybe_write_daily_drafts_snapshot(result)
    return result


def _write_drafts_json_atomic(data: dict) -> None:
    """Atomically write the full drafts JSON file under the per-store lock."""
    import uuid as _uuid
    with _email_drafts_lock:
        try:
            try:
                os.makedirs(os.path.dirname(_EMAIL_DRAFTS_PATH), exist_ok=True)
            except Exception:
                pass
            tmp = _EMAIL_DRAFTS_PATH + f".{_uuid.uuid4().hex[:8]}.tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, _EMAIL_DRAFTS_PATH)
        except Exception as e:
            logger.warning(f"[email-drafts] JSON save failed: {e}")
            raise


def _upsert_email_draft(draft: dict) -> None:
    """Insert-or-update a single draft row.

    Postgres path: one `INSERT ... ON CONFLICT (id) DO UPDATE` against just
    this draft. JSON fallback: read the file, swap or append the matching
    record, write it back atomically. **Never** issues a bulk `DELETE`, so
    a single stray call cannot wipe other drafts.

    Raises `EmailDraftsUnavailable` when the configured Postgres store is
    unreachable; callers should surface a 503.
    """
    if not isinstance(draft, dict) or not draft.get("id"):
        raise ValueError("draft must be a dict with a non-empty id")
    if _DRAFTS_DB_URL:
        conn = _drafts_db_conn()
        if conn is None:
            raise EmailDraftsUnavailable("Postgres drafts store: connect failed")
        try:
            if not _ensure_drafts_table(conn):
                raise EmailDraftsUnavailable("Postgres drafts store: CREATE TABLE failed")
            _dl_ts = draft.get("downloaded_ts")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO email_drafts (id, name, ts, status, last_published_ts, last_recipient_count, snapshot, category, downloaded_ts)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        ts = EXCLUDED.ts,
                        status = EXCLUDED.status,
                        last_published_ts = EXCLUDED.last_published_ts,
                        last_recipient_count = EXCLUDED.last_recipient_count,
                        snapshot = EXCLUDED.snapshot,
                        category = EXCLUDED.category,
                        downloaded_ts = EXCLUDED.downloaded_ts
                    """,
                    (
                        draft.get("id"),
                        draft.get("name") or "Untitled draft",
                        float(draft.get("ts") or 0),
                        draft.get("status") or "draft",
                        float(draft.get("last_published_ts") or 0),
                        int(draft.get("last_recipient_count") or 0),
                        json.dumps(draft.get("snapshot") or {}),
                        draft.get("category") or None,
                        float(_dl_ts) if _dl_ts else None,
                    ),
                )
                conn.commit()
            return
        except EmailDraftsUnavailable:
            raise
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"[email-drafts] DB upsert failed: {e}")
            raise EmailDraftsUnavailable(f"Postgres drafts store: {e}") from e
        finally:
            try:
                conn.close()
            except Exception:
                pass
    # JSON fallback (no Postgres configured): swap-or-append this row only.
    with _email_drafts_lock:
        existing = {"drafts": []}
        try:
            if os.path.exists(_EMAIL_DRAFTS_PATH):
                with open(_EMAIL_DRAFTS_PATH, "r") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("drafts"), list):
                    existing = loaded
        except Exception as e:
            logger.warning(f"[email-drafts] JSON read failed during upsert: {e}")
        drafts = existing.get("drafts") or []
        idx = next(
            (
                i for i, d in enumerate(drafts)
                if isinstance(d, dict) and d.get("id") == draft.get("id")
            ),
            -1,
        )
        if idx >= 0:
            drafts[idx] = draft
        else:
            drafts.append(draft)
        existing["drafts"] = drafts
        _write_drafts_json_atomic(existing)


def _delete_email_draft_by_id(draft_id: str) -> bool:
    """Delete a single draft by id. Returns True if a row was removed.

    Postgres path: `DELETE FROM email_drafts WHERE id = %s`. JSON fallback:
    load, drop the matching record, write back atomically. Never inverts
    "keep these" -- always names the exact id to drop.

    Raises `EmailDraftsUnavailable` when the configured Postgres store is
    unreachable; callers should surface a 503.
    """
    if not draft_id or not isinstance(draft_id, str):
        return False
    if _DRAFTS_DB_URL:
        conn = _drafts_db_conn()
        if conn is None:
            raise EmailDraftsUnavailable("Postgres drafts store: connect failed")
        try:
            if not _ensure_drafts_table(conn):
                raise EmailDraftsUnavailable("Postgres drafts store: CREATE TABLE failed")
            with conn.cursor() as cur:
                cur.execute("DELETE FROM email_drafts WHERE id = %s", (draft_id,))
                deleted = cur.rowcount
                conn.commit()
            return deleted > 0
        except EmailDraftsUnavailable:
            raise
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"[email-drafts] DB delete failed: {e}")
            raise EmailDraftsUnavailable(f"Postgres drafts store: {e}") from e
        finally:
            try:
                conn.close()
            except Exception:
                pass
    # JSON fallback.
    with _email_drafts_lock:
        existing = {"drafts": []}
        try:
            if os.path.exists(_EMAIL_DRAFTS_PATH):
                with open(_EMAIL_DRAFTS_PATH, "r") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("drafts"), list):
                    existing = loaded
        except Exception as e:
            logger.warning(f"[email-drafts] JSON read failed during delete: {e}")
            return False
        before = len(existing.get("drafts") or [])
        existing["drafts"] = [
            d for d in (existing.get("drafts") or [])
            if not (isinstance(d, dict) and d.get("id") == draft_id)
        ]
        if len(existing["drafts"]) == before:
            return False
        _write_drafts_json_atomic(existing)
        return True


def _evict_email_drafts_by_ids(ids) -> int:
    """Bulk-delete drafts by an explicit ID list. Logs WARNING with count + ids.

    Used by the per-store size cap eviction in the save endpoint. Always
    names the exact ids to drop (`DELETE WHERE id = ANY(%s)`) -- never
    inverts "keep these" -- so a caller bug or miscount cannot wipe the
    table. Returns the number of rows actually removed.

    Raises `EmailDraftsUnavailable` when the configured Postgres store is
    unreachable.
    """
    ids = [i for i in (ids or []) if isinstance(i, str) and i]
    if not ids:
        return 0
    logger.warning(
        f"[email-drafts] eviction: dropping {len(ids)} drafts by explicit id list: {ids}"
    )
    if _DRAFTS_DB_URL:
        conn = _drafts_db_conn()
        if conn is None:
            raise EmailDraftsUnavailable("Postgres drafts store: connect failed")
        try:
            if not _ensure_drafts_table(conn):
                raise EmailDraftsUnavailable("Postgres drafts store: CREATE TABLE failed")
            with conn.cursor() as cur:
                cur.execute("DELETE FROM email_drafts WHERE id = ANY(%s)", (ids,))
                deleted = cur.rowcount
                conn.commit()
            logger.warning(
                f"[email-drafts] eviction: actually removed {deleted} of {len(ids)} requested rows from Postgres"
            )
            return deleted
        except EmailDraftsUnavailable:
            raise
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error(f"[email-drafts] DB eviction failed: {e}")
            raise EmailDraftsUnavailable(f"Postgres drafts store: {e}") from e
        finally:
            try:
                conn.close()
            except Exception:
                pass
    # JSON fallback.
    with _email_drafts_lock:
        existing = {"drafts": []}
        try:
            if os.path.exists(_EMAIL_DRAFTS_PATH):
                with open(_EMAIL_DRAFTS_PATH, "r") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("drafts"), list):
                    existing = loaded
        except Exception as e:
            logger.warning(f"[email-drafts] JSON read failed during eviction: {e}")
            return 0
        ids_set = set(ids)
        before = len(existing.get("drafts") or [])
        existing["drafts"] = [
            d for d in (existing.get("drafts") or [])
            if not (isinstance(d, dict) and d.get("id") in ids_set)
        ]
        deleted = before - len(existing["drafts"])
        if deleted > 0:
            _write_drafts_json_atomic(existing)
        logger.warning(
            f"[email-drafts] eviction: actually removed {deleted} of {len(ids)} requested rows from JSON fallback"
        )
        return deleted


def _save_email_drafts(data: dict) -> None:
    """Per-row upsert wrapper for callers that still pass the full drafts list.

    Issues only `INSERT ... ON CONFLICT (id) DO UPDATE` per row -- **never**
    a bulk `DELETE`. An empty drafts list is rejected to prevent the
    historical "wipe everything" footgun: removals must go through
    `_delete_email_draft_by_id` (single row) or `_evict_email_drafts_by_ids`
    (explicit id list).
    """
    drafts = (data or {}).get("drafts") or []
    if not drafts:
        raise ValueError(
            "_save_email_drafts requires a non-empty drafts list; "
            "use _delete_email_draft_by_id or _evict_email_drafts_by_ids for removals"
        )
    for d in drafts:
        if isinstance(d, dict) and d.get("id"):
            _upsert_email_draft(d)


def _draft_summary(d: dict) -> dict:
    snap = d.get("snapshot") or {}
    combined = snap.get("combined") or {}
    # `category` is intentionally exposed even when null so the frontend
    # can rely on the key being present (the My Content grouping seam
    # reads it directly).
    return {
        "id": d.get("id"),
        "name": d.get("name") or "Untitled draft",
        "ts": d.get("ts") or 0,
        "channel": snap.get("channel") or "",
        "feature_count": len(snap.get("featureIds") or []),
        "subject": combined.get("subject") or "",
        "audience_label": combined.get("audienceLabel") or "",
        "status": d.get("status") or "draft",
        "last_published_ts": d.get("last_published_ts") or 0,
        "last_recipient_count": d.get("last_recipient_count") or 0,
        "category": d.get("category") or None,
        # Null when the user has never clicked Download HTML for this draft.
        # The Downloaded tab in My Content filters on this being non-null.
        "downloaded_ts": d.get("downloaded_ts") or None,
    }


@app.route("/api/email-drafts", methods=["GET"])
def list_email_drafts():
    try:
        try:
            data = _load_email_drafts()
        except EmailDraftsUnavailable as e:
            logger.error(f"[email-drafts] list: store unavailable: {e}")
            return jsonify({
                "error": "drafts store temporarily unavailable",
                "detail": str(e),
            }), 503
        drafts = sorted(
            (_draft_summary(d) for d in data.get("drafts", [])),
            key=lambda d: d.get("ts", 0),
            reverse=True,
        )
        return jsonify({"drafts": list(drafts)})
    except Exception as e:
        logger.error(f"[email-drafts] list error: {e}")
        return jsonify({"error": str(e)}), 500


_DRAFT_ID_RE = re_module.compile(r"^[A-Za-z0-9]{1,64}$")


def _normalize_draft_id(raw) -> str:
    """Coerce caller-supplied draft IDs into [A-Za-z0-9]{1,64} or generate one.

    Mirrors the /artifact/<id> route's character whitelist so deep links
    always round-trip.
    """
    import uuid as _uuid
    if raw and isinstance(raw, str) and _DRAFT_ID_RE.match(raw):
        return raw
    return _uuid.uuid4().hex[:12]


@app.route("/api/email-drafts", methods=["POST"])
def save_email_draft():
    try:
        body = request.get_json(force=True) or {}
        name = (body.get("name") or "").strip() or "Untitled draft"
        snapshot = body.get("snapshot")
        draft_id = _normalize_draft_id(body.get("id"))
        if not isinstance(snapshot, dict):
            return jsonify({"error": "snapshot must be an object"}), 400
        # Rough size guard.
        try:
            approx_bytes = len(json.dumps(snapshot))
            if approx_bytes > _EMAIL_DRAFT_MAX_BYTES:
                return jsonify({
                    "error": f"Draft too large ({approx_bytes // 1024} KB). Limit is {_EMAIL_DRAFT_MAX_BYTES // 1024} KB.",
                }), 413
        except Exception:
            pass
        try:
            data = _load_email_drafts()
        except EmailDraftsUnavailable as e:
            # Refuse to write when we couldn't read the current state -- a
            # transient blip here used to look like "the table is empty" and
            # let the (now removed) bulk reinsert wipe every other draft.
            logger.error(f"[email-drafts] save: store unavailable, refusing to write: {e}")
            return jsonify({
                "error": "drafts store temporarily unavailable",
                "detail": str(e),
            }), 503
        drafts = data.get("drafts", [])
        # Snapshot the pre-write id set so we can compute the exact eviction
        # list below (instead of inverting "keep these").
        existing_ids_before = {d.get("id") for d in drafts if isinstance(d, dict) and d.get("id")}
        now = time.time()
        # Upsert by id.
        existing_idx = next((i for i, d in enumerate(drafts) if d.get("id") == draft_id), -1)
        prev = drafts[existing_idx] if existing_idx >= 0 else {}
        # Status is sticky: once "published", stays published unless caller
        # explicitly downgrades it. Keeps Resend visible across edits.
        incoming_status = (body.get("status") or "").strip().lower()
        if incoming_status not in ("draft", "published"):
            incoming_status = prev.get("status") or "draft"
        # `category` is optional and currently nullable. The save UI does
        # not surface it yet (a future task will), but accept it here so
        # programmatic writers and future UI changes can populate it
        # without another round of plumbing.
        if "category" in body:
            raw_category = body.get("category")
            if raw_category is None:
                category = None
            else:
                category = (str(raw_category) or "").strip() or None
        else:
            category = prev.get("category") or None
        record = {
            "id": draft_id,
            "name": name,
            "ts": now,
            "snapshot": snapshot,
            "status": incoming_status,
            "last_published_ts": prev.get("last_published_ts") or 0,
            "last_recipient_count": prev.get("last_recipient_count") or 0,
            "category": category,
            # Carry forward any prior download stamp so re-saving an edited
            # draft does not erase its presence in the Downloaded tab.
            "downloaded_ts": prev.get("downloaded_ts") or None,
        }
        if existing_idx >= 0:
            drafts[existing_idx] = record
        else:
            drafts.append(record)
        # Cap to last 50 drafts to avoid unbounded growth.
        if len(drafts) > 50:
            drafts = sorted(drafts, key=lambda d: d.get("ts", 0), reverse=True)[:50]
        # Total-store size cap: drop oldest drafts (other than the one just
        # saved) until we're under the cap. Keeps disk usage bounded even when
        # snapshots embed base64 images/videos.
        drafts = sorted(drafts, key=lambda d: d.get("ts", 0), reverse=True)
        while len(drafts) > 1 and len(json.dumps({"drafts": drafts})) > _EMAIL_DRAFTS_TOTAL_MAX_BYTES:
            # Drop the oldest, but never the one we just saved.
            for j in range(len(drafts) - 1, -1, -1):
                if drafts[j].get("id") != draft_id:
                    drafts.pop(j)
                    break
            else:
                break
        # Compute the explicit eviction id list: anything that was present
        # before this call but is no longer in the desired post-cap list,
        # excluding the draft we're about to upsert. This is the only path
        # that bulk-removes drafts -- it always names the exact ids and
        # logs at WARNING.
        final_ids = {d.get("id") for d in drafts if isinstance(d, dict) and d.get("id")}
        to_evict = sorted(existing_ids_before - final_ids - {draft_id})
        if to_evict:
            try:
                _evict_email_drafts_by_ids(to_evict)
            except EmailDraftsUnavailable as e:
                logger.error(f"[email-drafts] save: eviction failed, refusing to write: {e}")
                return jsonify({
                    "error": "drafts store temporarily unavailable",
                    "detail": str(e),
                }), 503
        try:
            _upsert_email_draft(record)
        except EmailDraftsUnavailable as e:
            logger.error(f"[email-drafts] save: upsert failed: {e}")
            return jsonify({
                "error": "drafts store temporarily unavailable",
                "detail": str(e),
            }), 503
        return jsonify({"success": True, "id": draft_id, "summary": _draft_summary(record)})
    except Exception as e:
        logger.error(f"[email-drafts] save error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/email-drafts/<draft_id>", methods=["GET"])
def get_email_draft(draft_id: str):
    try:
        try:
            data = _load_email_drafts()
        except EmailDraftsUnavailable as e:
            logger.error(f"[email-drafts] get: store unavailable: {e}")
            return jsonify({
                "error": "drafts store temporarily unavailable",
                "detail": str(e),
            }), 503
        for d in data.get("drafts", []):
            if d.get("id") == draft_id:
                return jsonify(d)
        return jsonify({"error": "not found"}), 404
    except Exception as e:
        logger.error(f"[email-drafts] get error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/email-drafts/<draft_id>/mark-published", methods=["POST"])
def mark_email_draft_published(draft_id: str):
    """Flip a draft to status='published' and stamp last_published_ts.

    Called by the frontend (and by `/api/publish/email` when the request
    includes a draft_id) so My Artifacts can show what's been sent.
    """
    try:
        body = request.get_json(silent=True) or {}
        recipient_count = int(body.get("recipient_count") or 0)
        try:
            data = _load_email_drafts()
        except EmailDraftsUnavailable as e:
            logger.error(f"[email-drafts] mark-published: store unavailable, refusing to write: {e}")
            return jsonify({
                "error": "drafts store temporarily unavailable",
                "detail": str(e),
            }), 503
        drafts = data.get("drafts", [])
        for d in drafts:
            if d.get("id") == draft_id:
                d["status"] = "published"
                d["last_published_ts"] = time.time()
                if recipient_count > 0:
                    d["last_recipient_count"] = recipient_count
                # Per-row write only -- never re-saves the rest of the list,
                # so a stale in-memory snapshot can't clobber other drafts.
                try:
                    _upsert_email_draft(d)
                except EmailDraftsUnavailable as e:
                    logger.error(f"[email-drafts] mark-published: upsert failed: {e}")
                    return jsonify({
                        "error": "drafts store temporarily unavailable",
                        "detail": str(e),
                    }), 503
                return jsonify({"success": True, "summary": _draft_summary(d)})
        return jsonify({"error": "not found"}), 404
    except Exception as e:
        logger.error(f"[email-drafts] mark-published error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/email-drafts/<draft_id>/mark-downloaded", methods=["POST"])
def mark_email_draft_downloaded(draft_id: str):
    """Stamp `downloaded_ts` on a draft so it shows up in the Downloaded tab.

    The frontend calls this after the user clicks Download HTML and the blob
    has been delivered. Independent of `status`: a draft can be a Draft, an
    Artifact (Sent), AND appear in Downloaded simultaneously, since each tab
    answers a different question ("what am I working on?", "what did I send?",
    "what did I export to a file?").

    Implementation note: writes the new timestamp with a targeted single-row
    UPDATE (Postgres) or a per-row upsert via `_upsert_email_draft` (JSON
    fallback), so a concurrent mark-published or save call can't clobber the
    stamp -- and a transient DB blip returns 503 instead of silently falling
    through to the JSON file (which would create a stale on-disk record
    diverging from the Postgres source of truth).
    """
    try:
        now = time.time()
        if _DRAFTS_DB_URL:
            # Postgres-configured path: single-row UPDATE; on any failure
            # return 503 -- never silently fall back to JSON.
            conn = _drafts_db_conn()
            if conn is None:
                logger.error("[email-drafts] mark-downloaded: DB connect failed; refusing JSON fallback")
                return jsonify({
                    "error": "drafts store temporarily unavailable",
                    "detail": "Postgres drafts store: connect failed",
                }), 503
            try:
                if not _ensure_drafts_table(conn):
                    return jsonify({
                        "error": "drafts store temporarily unavailable",
                        "detail": "Postgres drafts store: CREATE TABLE failed",
                    }), 503
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE email_drafts SET downloaded_ts = %s WHERE id = %s",
                        (now, draft_id),
                    )
                    updated = cur.rowcount
                    conn.commit()
                if updated == 0:
                    return jsonify({"error": "not found"}), 404
                # Return the freshly-loaded summary so the client gets
                # the canonical record (status, downloaded_ts, etc.)
                # without a second round trip.
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, name, ts, status, last_published_ts, last_recipient_count, snapshot, category, downloaded_ts FROM email_drafts WHERE id = %s",
                        (draft_id,),
                    )
                    row = cur.fetchone()
                if row is None:
                    return jsonify({"error": "not found"}), 404
                return jsonify({"success": True, "summary": _draft_summary(_row_to_draft(row))})
            except Exception as e:
                logger.error(f"[email-drafts] DB mark-downloaded failed: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                return jsonify({
                    "error": "drafts store temporarily unavailable",
                    "detail": str(e),
                }), 503
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        # JSON fallback (no Postgres configured): hold the per-store lock
        # around the read-modify-write so concurrent writers serialize and
        # don't lose this stamp. The RLock lets `_upsert_email_draft`
        # re-acquire safely for its own JSON write.
        with _email_drafts_lock:
            try:
                data = _load_email_drafts()
            except EmailDraftsUnavailable as e:
                # Belt and suspenders: load shouldn't raise here because we
                # already gated on _DRAFTS_DB_URL above, but be defensive.
                return jsonify({
                    "error": "drafts store temporarily unavailable",
                    "detail": str(e),
                }), 503
            drafts = data.get("drafts", [])
            for d in drafts:
                if d.get("id") == draft_id:
                    d["downloaded_ts"] = now
                    _upsert_email_draft(d)
                    return jsonify({"success": True, "summary": _draft_summary(d)})
            return jsonify({"error": "not found"}), 404
    except Exception as e:
        logger.error(f"[email-drafts] mark-downloaded error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/email-drafts/<draft_id>", methods=["DELETE"])
def delete_email_draft(draft_id: str):
    try:
        # Per-row delete: never inverts "keep these," so a stale
        # in-memory snapshot can't take down the rest of the table.
        try:
            removed = _delete_email_draft_by_id(draft_id)
        except EmailDraftsUnavailable as e:
            logger.error(f"[email-drafts] delete: store unavailable: {e}")
            return jsonify({
                "error": "drafts store temporarily unavailable",
                "detail": str(e),
            }), 503
        if not removed:
            return jsonify({"error": "not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"[email-drafts] delete error: {e}")
        return jsonify({"error": str(e)}), 500


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
    _apply_feature_url_overrides(all_features)

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

    Response: {"features": [...], "total": N, "days": N}
    """
    days = request.args.get("days", default=30, type=int)
    if days not in (30, 60, 90):
        days = 30
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

    _apply_feature_url_overrides(features)

    cache_key = f"days_{days}"
    cached_entry = _pipeline_cache.get(cache_key)
    last_refreshed = cached_entry["timestamp"] if cached_entry else time.time()

    return jsonify({
        "features": features,
        "total": len(features),
        "days": days,
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


def _check_admin_auth():
    """Gate admin endpoints behind an AMPLIFY_ADMIN_TOKEN env var.
    Returns None if authorized, or a (response, status) tuple to return.
    Token may be provided via X-Admin-Token header or ?admin_token= query.
    If AMPLIFY_ADMIN_TOKEN is unset, the endpoint is locked (returns 503).
    """
    expected = os.environ.get("AMPLIFY_ADMIN_TOKEN", "")
    if not expected:
        return jsonify({
            "error": "admin_disabled",
            "message": "Admin endpoints are disabled. Set AMPLIFY_ADMIN_TOKEN to enable.",
        }), 503
    provided = (
        request.headers.get("X-Admin-Token", "")
        or request.args.get("admin_token", "")
        or (request.get_json(silent=True) or {}).get("admin_token", "")
    )
    if not provided or provided != expected:
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.route("/api/admin/backfill-low-signal-classifications", methods=["POST"])
def backfill_low_signal_classifications():
    """Walk the classification cache and rewrite any entry whose TITLE alone
    is obviously junk (',etc.', '[Duplicate] ...', 'tbd', 'v16 -> v17').
    Useful for one-shot cleanup of historical rows (like the ',etc.' bug)
    that were classified before the guardrail existed.

    Scope note: this uses a TITLE-ONLY heuristic (is_obviously_junk_title)
    rather than the full has_sufficient_signal(title, description) check
    used at classification time. Cached rows do not preserve the original
    source description, so a title-only check avoids false positives on
    legitimate short titles whose descriptions we no longer have.
    Quick-keyword rows are also intentionally skipped — they are already
    score 1 and not affected by the hallucination bug class this addresses.

    Category: Admin (gated by AMPLIFY_ADMIN_TOKEN)

    Query/body params:
    - dry_run (bool, default false): preview without mutating the cache.
    - admin_token: passed via header X-Admin-Token, query, or body.

    Response: {"scanned": N, "downgraded": N, "samples": [{feature_id, old_score, title}, ...]}
    """
    auth_err = _check_admin_auth()
    if auth_err is not None:
        return auth_err

    def _parse_bool(v):
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    data = request.get_json(silent=True) or {}
    dry_run = _parse_bool(data.get("dry_run", request.args.get("dry_run")))

    scanned = 0
    downgraded = 0
    samples = []

    # Use the title-only check: cache rows don't preserve the original source
    # description, so we can't reliably re-run `has_sufficient_signal`. We only
    # downgrade rows whose TITLE alone is so degenerate that no description
    # could rescue it (e.g. ',etc.', 'tbd', 'v16 -> v17').
    for fid, cl in list(CLASSIFICATION_CACHE.items()):
        scanned += 1
        method = cl.get("classification_method", "")
        if method in ("quick_keyword", "guardrail_low_signal"):
            continue
        title = cl.get("title", "") or ""
        if not is_obviously_junk_title(title):
            continue
        old_score = cl.get("importance_score", 0)
        if len(samples) < 50:
            samples.append({
                "feature_id": fid,
                "title": title[:120],
                "old_score": old_score,
                "old_method": method,
            })
        if not dry_run:
            CLASSIFICATION_CACHE[fid] = _low_signal_result(fid, title)
        downgraded += 1

    if not dry_run and downgraded > 0:
        # Force a flush; _save_cache_to_disk only saves when dirty, so mark it.
        from ai import classifier as _cl_mod
        _cl_mod._cache_dirty = True
        _save_cache_to_disk()

    logger.info(f"[backfill] scanned={scanned} downgraded={downgraded} dry_run={dry_run}")
    return jsonify({
        "scanned": scanned,
        "downgraded": downgraded,
        "dry_run": dry_run,
        "samples": samples,
    })


def _collect_known_feature_ids() -> set:
    """Gather every feature_id that could legitimately own attached media.

    Pulls from the in-memory pipeline cache and every features-cache JSON on
    disk so the cleanup endpoint doesn't accidentally delete videos for a
    feature that simply isn't loaded into the current process.
    """
    ids: set = set()
    try:
        for entry in _pipeline_cache.values():
            for f in (entry or {}).get("features", []) or []:
                fid = f.get("id") if isinstance(f, dict) else None
                if fid:
                    ids.add(fid)
    except Exception as e:
        logger.warning(f"[cleanup] failed to read pipeline cache: {e}")

    try:
        for name in os.listdir(_FEATURES_CACHE_DIR):
            if not (name.startswith(".features_cache_days") and name.endswith(".json")):
                continue
            path = os.path.join(_FEATURES_CACHE_DIR, name)
            try:
                with open(path) as f:
                    data = json.load(f)
                for feat in data.get("features", []) or []:
                    fid = feat.get("id") if isinstance(feat, dict) else None
                    if fid:
                        ids.add(fid)
            except Exception as e:
                logger.warning(f"[cleanup] failed to read {name}: {e}")
    except Exception as e:
        logger.warning(f"[cleanup] failed to scan features cache dir: {e}")

    return ids


@app.route("/api/admin/cleanup-orphan-videos", methods=["POST"])
def cleanup_orphan_videos_endpoint():
    """One-shot maintenance: remove orphan / duplicate videos from disk.

    Pre-existing videos accumulated under .publish_videos/ before the
    server-side delete fix landed (task #42). This endpoint walks every
    meta.json and removes:
    - videos whose owning feature_id no longer exists in any features cache
    - exact duplicates per feature (same filename + size, keeping the newest)
    - directories with missing/unreadable meta.json

    Safe to re-run; logs every removal.

    Category: Admin (gated by AMPLIFY_ADMIN_TOKEN)

    Query/body params:
    - dry_run (bool, default false): preview without deleting.
    - admin_token: passed via header X-Admin-Token, query, or body.

    Response: {"scanned", "removed_orphan", "removed_duplicate",
               "removed_unreadable", "dry_run", "known_feature_count"}
    """
    auth_err = _check_admin_auth()
    if auth_err is not None:
        return auth_err

    def _parse_bool(v):
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    body = request.get_json(silent=True) or {}
    dry_run = _parse_bool(body.get("dry_run", request.args.get("dry_run")))

    known = _collect_known_feature_ids()
    if not known:
        logger.warning("[cleanup] No known feature ids found; skipping orphan-by-feature pass to avoid mass deletion. Run after the pipeline has loaded features.")
        report = cleanup_orphan_videos(known_feature_ids=None, dry_run=dry_run)
        report["known_feature_count"] = 0
        report["note"] = "Skipped orphan-by-feature deletion because no features are loaded. Refresh the pipeline and re-run."
        return jsonify(report)

    report = cleanup_orphan_videos(known_feature_ids=known, dry_run=dry_run)
    report["known_feature_count"] = len(known)
    return jsonify(report)


# ---------------------------------------------------------------------------
# Attachment storage admin (Task #99)
# ---------------------------------------------------------------------------

def _attachments_pending_counts() -> dict:
    """Best-effort count of items not yet on S3 by kind.

    Used by the status panel and backfill endpoint to show how much work
    remains. All counters are clamped to small reads — this is an
    operator panel, not an analytics warehouse.
    """
    out = {
        "feature-images": 0,
        "videos": 0,
        "video-thumbs": 0,
        "external-thumbs": 0,
        "hosted-emails": 0,
        "announcements": 0,
    }
    # Feature images on disk: flat ``<feature_id>.meta.json`` files in
    # IMAGES_DIR. ``_hosted_<id>`` subdirs are counted under hosted-emails
    # below, not here.
    try:
        from ai.publish_store import IMAGES_DIR as _IMG_DIR
        if os.path.isdir(_IMG_DIR):
            n = 0
            for fn in os.listdir(_IMG_DIR):
                if not fn.endswith(".meta.json"):
                    continue
                full = os.path.join(_IMG_DIR, fn)
                if not os.path.isfile(full):
                    continue
                try:
                    with open(full) as f:
                        m = json.load(f)
                    if not m.get("s3_key"):
                        n += 1
                except Exception:
                    continue
            out["feature-images"] = n
    except Exception:
        pass
    # Videos & thumbs on disk
    try:
        from ai.publish_store import VIDEOS_DIR as _V_DIR
        if os.path.isdir(_V_DIR):
            nv = 0
            nt = 0
            for fn in os.listdir(_V_DIR):
                vdir = os.path.join(_V_DIR, fn)
                if not os.path.isdir(vdir) or fn.startswith("_"):
                    continue
                meta_path = os.path.join(vdir, "meta.json")
                try:
                    if os.path.exists(meta_path):
                        with open(meta_path) as f:
                            m = json.load(f)
                        if not m.get("s3_key"):
                            nv += 1
                        if not m.get("s3_thumb_key"):
                            nt += 1
                except Exception:
                    continue
            out["videos"] = nv
            out["video-thumbs"] = nt
    except Exception:
        pass
    # Hosted email images: rows missing s3_key (DB) + _hosted_* disk
    # fallback subdirs that haven't been mirrored yet (rare path).
    try:
        n = 0
        conn = _drafts_db_conn()
        if conn is not None:
            try:
                if _ensure_hosted_images_table(conn):
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT COUNT(1) FROM email_hosted_images WHERE s3_key IS NULL OR s3_key = ''"
                        )
                        row = cur.fetchone()
                        n += int(row[0]) if row else 0
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            from ai.publish_store import IMAGES_DIR as _IMG_DIR
            if os.path.isdir(_IMG_DIR):
                for fn in os.listdir(_IMG_DIR):
                    if not fn.startswith("_hosted_"):
                        continue
                    fdir = os.path.join(_IMG_DIR, fn)
                    if os.path.isdir(fdir) and not os.path.isfile(os.path.join(fdir, ".s3")):
                        n += 1
        except Exception:
            pass
        out["hosted-emails"] = n
    except Exception:
        pass
    # Announcement uploads: files without sidecar
    try:
        from announcements_routes import UPLOAD_DIR as _ANN_DIR
        if os.path.isdir(_ANN_DIR):
            n = 0
            for fn in os.listdir(_ANN_DIR):
                if fn.endswith(".s3"):
                    continue
                full = os.path.join(_ANN_DIR, fn)
                if not os.path.isfile(full):
                    continue
                if not os.path.isfile(full + ".s3"):
                    n += 1
            out["announcements"] = n
    except Exception:
        pass
    # External thumbs cache: count cached jpgs that don't yet have a
    # ``.s3`` sidecar marker recording the mirror.
    try:
        from integrations.video_thumb import _CACHE_DIR as _XT_DIR  # type: ignore
        if os.path.isdir(_XT_DIR):
            n = 0
            for fn in os.listdir(_XT_DIR):
                if not fn.endswith(".jpg"):
                    continue
                full = os.path.join(_XT_DIR, fn)
                if not os.path.isfile(full):
                    continue
                if os.path.isfile(full + ".s3"):
                    continue
                n += 1
            out["external-thumbs"] = n
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# Background attachment-backfill sweep (Task #104).
#
# When S3 is enabled we run a daemon thread that walks each kind in small
# batches via the existing ``_backfill_*`` helpers, so historical
# attachments migrate to S3 without an operator having to click the
# "Backfill 50" buttons over and over. Knobs are exposed via env vars so
# operators can tune the cadence without code changes; the manual buttons
# in the dashboard panel keep working untouched.
# ---------------------------------------------------------------------------

_BACKFILL_SWEEP_LOCK = threading.Lock()
_BACKFILL_SWEEP_STATE: dict = {
    "started": False,
    "running": False,
    "enabled": False,
    "interval_seconds": 0,
    "batch_size": 0,
    "max_cycle_seconds": 0,
    "initial_delay_seconds": 0,
    "cycle_count": 0,
    "last_started_at": None,
    "last_finished_at": None,
    "last_duration_seconds": None,
    "last_report": None,
    "last_totals": None,
    "next_run_at": None,
    "last_error": None,
}


def _backfill_sweep_env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except Exception:
        return default
    return max(lo, min(hi, v))


def _backfill_sweep_settings() -> dict:
    """Resolve env-var knobs for the background sweep."""
    return {
        "interval_seconds": _backfill_sweep_env_int(
            "AMPLIFY_BACKFILL_INTERVAL_SECONDS", 600, 30, 24 * 3600),
        "batch_size": _backfill_sweep_env_int(
            "AMPLIFY_BACKFILL_BATCH_SIZE", 100, 1, 500),
        "max_cycle_seconds": _backfill_sweep_env_int(
            "AMPLIFY_BACKFILL_MAX_CYCLE_SECONDS", 540, 10, 24 * 3600),
        "initial_delay_seconds": _backfill_sweep_env_int(
            "AMPLIFY_BACKFILL_INITIAL_DELAY_SECONDS", 60, 0, 24 * 3600),
    }


def _backfill_sweep_auto_enabled() -> bool:
    """Master toggle. Defaults to on; set ``AMPLIFY_BACKFILL_AUTO=0`` to disable."""
    raw = (os.environ.get("AMPLIFY_BACKFILL_AUTO") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _backfill_runners() -> dict:
    """Mirror the dispatch table used by the manual endpoint."""
    return {
        "feature-images": _backfill_feature_images,
        "videos": lambda n: _backfill_videos(n, want_thumbs=False),
        "video-thumbs": lambda n: _backfill_videos(n, want_thumbs=True),
        "external-thumbs": _backfill_external_thumbs,
        "hosted-emails": _backfill_hosted_emails,
        "announcements": _backfill_announcements,
    }


def _backfill_one_item(kind: str, item_id: str) -> dict:
    """Re-run the relevant backfill helper scoped to a single item.

    Reuses the existing per-kind helpers (no duplicate upload logic) by
    passing the ``only_id`` filter. Returns the helper's report dict.
    """
    if kind == "feature-images":
        return _backfill_feature_images(1, only_id=item_id)
    if kind == "videos":
        return _backfill_videos(1, want_thumbs=False, only_id=item_id)
    if kind == "video-thumbs":
        return _backfill_videos(1, want_thumbs=True, only_id=item_id)
    if kind == "external-thumbs":
        return _backfill_external_thumbs(1, only_id=item_id)
    if kind == "hosted-emails":
        return _backfill_hosted_emails(1, only_id=item_id)
    if kind == "announcements":
        return _backfill_announcements(1, only_id=item_id)
    raise ValueError(f"unknown kind: {kind}")


def _run_backfill_sweep_cycle(batch_size: int, deadline_ts: float) -> dict:
    """Walk every kind once, honouring the ``deadline_ts`` between kinds.

    The deadline is a *best-effort* guard, checked between kinds — once a
    runner is in flight we let it finish its current batch (the
    ``_backfill_*`` helpers already cap themselves at ``batch_size``
    items, so wall-clock cost per kind is bounded by S3 latency × batch
    size). If the deadline passes mid-cycle, every remaining kind is
    skipped with ``{"skipped": "deadline_reached"}`` and the next cycle
    picks up where this one left off. We deliberately do not interrupt
    a running runner because S3 PUTs are not safely cancellable.

    Returns a per-kind report plus aggregate totals.
    """
    report: dict = {}
    totals = {"scanned": 0, "uploaded": 0, "errors": 0}
    for kind, fn in _backfill_runners().items():
        if time.time() >= deadline_ts:
            report[kind] = {"skipped": "deadline_reached"}
            continue
        try:
            r = fn(batch_size) or {}
        except Exception as e:
            logger.exception(f"[backfill-sweep] kind={kind} crashed")
            report[kind] = {"error": str(e)}
            totals["errors"] += 1
            continue
        report[kind] = r
        # ``_backfill_videos`` reports both ``uploaded`` (videos) and
        # ``thumbs_uploaded``; collapse both into the aggregate uploaded
        # counter so operators get one headline number.
        totals["scanned"] += int(r.get("scanned") or 0)
        totals["uploaded"] += int(r.get("uploaded") or 0)
        totals["uploaded"] += int(r.get("thumbs_uploaded") or 0)
        totals["errors"] += int(r.get("errors") or 0)
    return {"report": report, "totals": totals}


def _backfill_sweep_loop() -> None:
    """Daemon-thread loop: cycle, sleep, repeat. Self-pauses if S3 is disabled."""
    settings = _backfill_sweep_settings()
    with _BACKFILL_SWEEP_LOCK:
        _BACKFILL_SWEEP_STATE.update(settings)
    initial_delay = settings["initial_delay_seconds"]
    interval = settings["interval_seconds"]
    batch_size = settings["batch_size"]
    max_cycle = settings["max_cycle_seconds"]
    if initial_delay > 0:
        time.sleep(initial_delay)
    while True:
        try:
            from integrations import attachment_store as _astore
            enabled = _astore.s3_enabled() and _backfill_sweep_auto_enabled()
            with _BACKFILL_SWEEP_LOCK:
                _BACKFILL_SWEEP_STATE["enabled"] = enabled
            if not enabled:
                # S3 was turned off (or AMPLIFY_BACKFILL_AUTO unset/0); sit
                # tight and re-check next interval.
                time.sleep(interval)
                continue
            with _BACKFILL_SWEEP_LOCK:
                _BACKFILL_SWEEP_STATE["running"] = True
                _BACKFILL_SWEEP_STATE["last_started_at"] = time.time()
                _BACKFILL_SWEEP_STATE["last_error"] = None
            t0 = time.time()
            deadline = t0 + max_cycle
            try:
                result = _run_backfill_sweep_cycle(batch_size, deadline)
            except Exception as e:
                logger.exception("[backfill-sweep] cycle crashed")
                with _BACKFILL_SWEEP_LOCK:
                    _BACKFILL_SWEEP_STATE["last_error"] = str(e)
                result = {"report": {}, "totals": {"scanned": 0, "uploaded": 0, "errors": 1}}
            t1 = time.time()
            totals = result["totals"]
            logger.info(
                "[backfill-sweep] cycle done in %.1fs scanned=%d uploaded=%d errors=%d",
                t1 - t0, totals["scanned"], totals["uploaded"], totals["errors"],
            )
            with _BACKFILL_SWEEP_LOCK:
                _BACKFILL_SWEEP_STATE["running"] = False
                _BACKFILL_SWEEP_STATE["last_finished_at"] = t1
                _BACKFILL_SWEEP_STATE["last_duration_seconds"] = round(t1 - t0, 3)
                _BACKFILL_SWEEP_STATE["last_report"] = result["report"]
                _BACKFILL_SWEEP_STATE["last_totals"] = totals
                _BACKFILL_SWEEP_STATE["cycle_count"] += 1
                _BACKFILL_SWEEP_STATE["next_run_at"] = t1 + interval
        except Exception as e:
            # The outer try keeps the daemon alive across any surprises.
            logger.exception("[backfill-sweep] loop iteration failed")
            with _BACKFILL_SWEEP_LOCK:
                _BACKFILL_SWEEP_STATE["running"] = False
                _BACKFILL_SWEEP_STATE["last_error"] = str(e)
        time.sleep(interval)


def _start_background_attachment_backfill() -> bool:
    """Start the daemon thread if it's not already running and S3 is enabled.

    Idempotent — safe to call multiple times. Returns True if a thread was
    started this call, False if skipped (already running, S3 disabled, or
    auto-sweep turned off).
    """
    with _BACKFILL_SWEEP_LOCK:
        if _BACKFILL_SWEEP_STATE.get("started"):
            return False
        if not _backfill_sweep_auto_enabled():
            logger.info("[backfill-sweep] disabled via AMPLIFY_BACKFILL_AUTO=0")
            return False
        try:
            from integrations import attachment_store as _astore
            if not _astore.s3_enabled():
                logger.info(
                    "[backfill-sweep] not starting — S3 backend not enabled "
                    "(AMPLIFY_IMAGE_STORAGE_BACKEND or S3_* secrets unset)"
                )
                return False
        except Exception:
            logger.exception("[backfill-sweep] could not query attachment_store; skipping start")
            return False
        _BACKFILL_SWEEP_STATE["started"] = True
        settings = _backfill_sweep_settings()
        _BACKFILL_SWEEP_STATE.update(settings)
        _BACKFILL_SWEEP_STATE["enabled"] = True
        _BACKFILL_SWEEP_STATE["next_run_at"] = (
            time.time() + settings["initial_delay_seconds"]
        )
    t = threading.Thread(
        target=_backfill_sweep_loop,
        name="attachment-backfill-sweep",
        daemon=True,
    )
    t.start()
    logger.info(
        "[backfill-sweep] started: every %ds, batch=%d, max_cycle=%ds, initial_delay=%ds",
        settings["interval_seconds"], settings["batch_size"],
        settings["max_cycle_seconds"], settings["initial_delay_seconds"],
    )
    return True


def _backfill_sweep_status_snapshot() -> dict:
    """Lock-protected copy of the sweep state for the admin status panel."""
    with _BACKFILL_SWEEP_LOCK:
        return dict(_BACKFILL_SWEEP_STATE)


_ATTACHMENT_KINDS = (
    "feature-images", "videos", "video-thumbs",
    "external-thumbs", "hosted-emails", "announcements",
)


def _attachment_issue_lists(per_kind_limit: int = 25) -> dict:
    """Group recent per-item outcomes into ``failures`` and ``skips`` per kind.

    "Healthy" skip reasons (currently just ``already-mirrored``) are
    omitted from the skips list so admins only see items that need
    attention. The returned shape is::

        {kind: {"failures": [outcome, ...], "skips": [outcome, ...]}}

    Both lists are newest-first and capped at ``per_kind_limit``.
    """
    from integrations import attachment_store as _astore
    out: dict = {}
    for kind in _ATTACHMENT_KINDS:
        out[kind] = {
            "failures": _astore.recent_outcomes(
                kind=kind, outcomes=("error",), limit=per_kind_limit,
            ),
            "skips": _astore.recent_outcomes(
                kind=kind, outcomes=("skipped",),
                exclude_reasons=_astore.HEALTHY_SKIP_REASONS,
                limit=per_kind_limit,
            ),
        }
    return out


@app.route("/api/admin/attachments/status", methods=["GET"])
def attachments_status_endpoint():
    """Return the current attachment-storage configuration + per-kind pending counts.

    Category: Admin (gated by AMPLIFY_ADMIN_TOKEN)

    Response shape::

        {
            "backend": "s3"|"local",
            "s3_enabled": bool,
            "secrets_present": {S3_Bucket_name: bool, ...},
            "pending": {feature-images: N, videos: N, ...},
            "recent": [{ts, backend, kind, key, bytes, ok, error}, ...],
            # Task #112 — per-item diagnostics: every backfill helper
            # now records a structured outcome (uploaded / skipped /
            # error) for each item it visits. ``issues`` exposes recent
            # failures and non-healthy skips per kind so admins can
            # diagnose without opening server logs. Healthy skips
            # (``already-mirrored``) are omitted from this list.
            "issues": {kind: {"failures": [{ts, item_id, reason, ...}],
                              "skips": [{ts, item_id, reason, ...}]}},
            "skip_reasons": [...],   # the categorical vocabulary
            "auto_sweep": {started, enabled, running, cycle_count,
                           interval_seconds, batch_size, max_cycle_seconds,
                           last_started_at, last_finished_at,
                           last_duration_seconds, last_totals, last_report,
                           next_run_at, last_error}
        }
    """
    auth_err = _check_admin_auth()
    if auth_err is not None:
        return auth_err
    from integrations import attachment_store as _astore
    # ``direct_s3_url_usable`` reports whether ``s3_public_url`` can mint
    # the long-lived virtual-hosted-style URL the email-HTML rewrite
    # (Task #115) embeds into downloaded ``.html`` files. It's a cheap
    # config-only probe — bucket + region must both be set, otherwise
    # the rewrite has nothing to rewrite TO and silently leaves
    # /api/... URLs in place. Surfacing this lets ops notice "S3 is on
    # but downloads are still going through the app" without grepping
    # logs for the rewrite-s3 warnings.
    probe_url = _astore.s3_public_url("status-probe-key")
    return jsonify({
        "success": True,
        "backend": _astore.get_backend_name(),
        "s3_enabled": _astore.s3_enabled(),
        "secrets_present": _astore.secrets_present(),
        "direct_s3_url_usable": bool(probe_url),
        "pending": _attachments_pending_counts(),
        "recent": _astore.recent_uploads(limit=25),
        "issues": _attachment_issue_lists(per_kind_limit=25),
        "skip_reasons": list(_astore.SKIP_REASONS),
        "auto_sweep": _backfill_sweep_status_snapshot(),
    })


def _backfill_feature_images(limit: int, only_id: str = "") -> dict:
    """Upload local feature image bytes to S3 for rows missing s3_key.

    Feature images live as flat ``<feature_id>.img`` (data URL) +
    ``<feature_id>.meta.json`` pairs in IMAGES_DIR. We decode the data
    URL, push the raw bytes via the attachment seam, and persist the
    resulting S3 key back into the meta JSON so the serve endpoint can
    302-redirect on subsequent reads.

    When ``only_id`` is supplied, only that single feature id is processed
    (used by the per-item retry endpoint).
    """
    import base64 as _b64
    import re as _re
    from ai.publish_store import IMAGES_DIR as _IMG_DIR
    from integrations import attachment_store as _astore
    scanned = uploaded = errors = 0
    if not os.path.isdir(_IMG_DIR):
        return {"scanned": 0, "uploaded": 0, "errors": 0}
    for fn in sorted(os.listdir(_IMG_DIR)):
        if uploaded >= limit:
            break
        if not fn.endswith(".meta.json"):
            continue
        feature_id = fn[: -len(".meta.json")]
        if only_id and feature_id != only_id:
            continue
        meta_path = os.path.join(_IMG_DIR, fn)
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception as e:
            _astore.record_outcome(
                "feature-images", feature_id, "error",
                reason=f"meta-read-failed: {e}",
            )
            errors += 1
            continue
        scanned += 1
        if meta.get("s3_key"):
            _astore.record_outcome(
                "feature-images", feature_id, "skipped",
                reason="already-mirrored", key=meta.get("s3_key"),
            )
            continue
        img_path = os.path.join(_IMG_DIR, feature_id + ".img")
        if not os.path.exists(img_path):
            _astore.record_outcome(
                "feature-images", feature_id, "skipped",
                reason="source-missing",
            )
            continue
        try:
            with open(img_path) as f:
                data_url = f.read()
            m = _re.match(r"data:image/(\w+);base64,(.+)", data_url, _re.DOTALL)
            if not m:
                logger.warning(f"[backfill] feature-images {feature_id} not a data URL")
                _astore.record_outcome(
                    "feature-images", feature_id, "error",
                    reason="not-a-data-url",
                )
                errors += 1
                continue
            ext = m.group(1).lower()
            raw = _b64.b64decode(m.group(2), validate=False)
            ctype = "image/gif" if (meta.get("is_gif") or ext == "gif") else f"image/{ext}"
            res = _astore.put(
                kind="feature-images",
                key_hint=f"{feature_id}.{ext}",
                raw_bytes=raw,
                content_type=ctype,
            )
            if res.get("backend") == "s3" and res.get("key"):
                meta["s3_key"] = res["key"]
                meta["s3_url"] = res.get("url") or ""
                meta["s3_content_type"] = ctype
                tmp = meta_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(meta, f)
                os.replace(tmp, meta_path)
                uploaded += 1
                _astore.record_outcome(
                    "feature-images", feature_id, "uploaded",
                    key=res["key"], bytes=len(raw),
                )
            else:
                errors += 1
                _astore.record_outcome(
                    "feature-images", feature_id, "error",
                    reason=res.get("error") or "s3-put-failed",
                )
        except Exception as e:
            logger.warning(f"[backfill] feature-images {feature_id} failed: {e}")
            errors += 1
            _astore.record_outcome(
                "feature-images", feature_id, "error", reason=repr(e),
            )
    return {"scanned": scanned, "uploaded": uploaded, "errors": errors}


def _backfill_videos(limit: int, want_thumbs: bool = True, only_id: str = "") -> dict:
    from ai.publish_store import VIDEOS_DIR as _V_DIR
    from integrations import attachment_store as _astore
    # The kind label used for outcome recording when ``want_thumbs`` is
    # the only thing being processed (the dispatch table maps the
    # ``video-thumbs`` kind to a thumbs-only run); for the combined
    # videos run we still tag video-bytes outcomes as ``videos``.
    scanned = uploaded = thumbs_uploaded = errors = 0
    if not os.path.isdir(_V_DIR):
        return {"scanned": 0, "uploaded": 0, "thumbs_uploaded": 0, "errors": 0}
    for fn in os.listdir(_V_DIR):
        if uploaded + thumbs_uploaded >= limit:
            break
        if only_id and fn != only_id:
            continue
        vdir = os.path.join(_V_DIR, fn)
        if not os.path.isdir(vdir) or fn.startswith("_"):
            continue
        meta_path = os.path.join(vdir, "meta.json")
        if not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception as e:
            _astore.record_outcome(
                "videos" if not want_thumbs else "video-thumbs",
                fn, "error", reason=f"meta-read-failed: {e}",
            )
            errors += 1
            continue
        scanned += 1
        meta_changed = False
        # Video bytes (always attempted — the dispatch table also calls
        # us with want_thumbs=True for ``video-thumbs``, but historically
        # that path also picked up missing video bytes; preserved here).
        if meta.get("s3_key"):
            _astore.record_outcome(
                "videos", fn, "skipped",
                reason="already-mirrored", key=meta.get("s3_key"),
            )
        else:
            ext = meta.get("ext") or ".mp4"
            video_path = os.path.join(vdir, "video" + ext)
            if not os.path.exists(video_path):
                _astore.record_outcome(
                    "videos", fn, "skipped", reason="source-missing",
                )
            else:
                try:
                    with open(video_path, "rb") as f:
                        raw = f.read()
                    ext_clean = ext.lstrip(".").lower() or "mp4"
                    ctype = {"mp4": "video/mp4", "mov": "video/quicktime",
                             "webm": "video/webm", "avi": "video/x-msvideo",
                             "mkv": "video/x-matroska"}.get(ext_clean, "video/mp4")
                    res = _astore.put(
                        kind="videos",
                        key_hint=f"videos/{fn}/video{ext}",
                        raw_bytes=raw,
                        content_type=ctype,
                    )
                    if res.get("backend") == "s3" and res.get("key"):
                        meta["s3_key"] = res["key"]
                        meta["s3_url"] = res.get("url") or ""
                        meta["s3_content_type"] = ctype
                        meta_changed = True
                        uploaded += 1
                        _astore.record_outcome(
                            "videos", fn, "uploaded",
                            key=res["key"], bytes=len(raw),
                        )
                    else:
                        errors += 1
                        _astore.record_outcome(
                            "videos", fn, "error",
                            reason=res.get("error") or "s3-put-failed",
                        )
                except Exception as e:
                    logger.warning(f"[backfill] videos {fn} failed: {e}")
                    errors += 1
                    _astore.record_outcome(
                        "videos", fn, "error", reason=repr(e),
                    )
        # Thumb
        if want_thumbs:
            if meta.get("s3_thumb_key"):
                _astore.record_outcome(
                    "video-thumbs", fn, "skipped",
                    reason="already-mirrored", key=meta.get("s3_thumb_key"),
                )
            else:
                thumb_path = os.path.join(vdir, "thumb.jpg")
                if not os.path.exists(thumb_path):
                    _astore.record_outcome(
                        "video-thumbs", fn, "skipped", reason="source-missing",
                    )
                else:
                    try:
                        with open(thumb_path, "rb") as f:
                            raw = f.read()
                        res = _astore.put(
                            kind="video-thumbs",
                            key_hint=f"videos/{fn}/thumb.jpg",
                            raw_bytes=raw,
                            content_type="image/jpeg",
                        )
                        if res.get("backend") == "s3" and res.get("key"):
                            meta["s3_thumb_key"] = res["key"]
                            meta["s3_thumb_url"] = res.get("url") or ""
                            meta_changed = True
                            thumbs_uploaded += 1
                            _astore.record_outcome(
                                "video-thumbs", fn, "uploaded",
                                key=res["key"], bytes=len(raw),
                            )
                        else:
                            errors += 1
                            _astore.record_outcome(
                                "video-thumbs", fn, "error",
                                reason=res.get("error") or "s3-put-failed",
                            )
                    except Exception as e:
                        logger.warning(f"[backfill] video-thumbs {fn} failed: {e}")
                        errors += 1
                        _astore.record_outcome(
                            "video-thumbs", fn, "error", reason=repr(e),
                        )
        if meta_changed:
            try:
                tmp = meta_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(meta, f)
                os.replace(tmp, meta_path)
                # Re-upsert to DB so the S3 cols persist there too.
                try:
                    from ai.publish_store import _db_upsert_video as _upsert_v  # type: ignore
                    _upsert_v(meta, video_bytes=None, thumb_bytes=None)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[backfill] meta write failed for {fn}: {e}")
    return {"scanned": scanned, "uploaded": uploaded,
            "thumbs_uploaded": thumbs_uploaded, "errors": errors}


def _backfill_hosted_emails(limit: int, only_id: str = "") -> dict:
    """Mirror hosted-email images from Postgres + ``_hosted_*`` disk fallback to S3."""
    import base64 as _b64
    import re as _re
    from integrations import attachment_store as _astore
    scanned = uploaded = errors = 0
    # 1) DB rows missing s3_key.
    conn = _drafts_db_conn()
    if conn is not None:
        try:
            if _ensure_hosted_images_table(conn):
                with conn.cursor() as cur:
                    if only_id:
                        cur.execute(
                            """SELECT id, ext, name, data FROM email_hosted_images
                               WHERE id = %s""",
                            (only_id,),
                        )
                    else:
                        cur.execute(
                            """SELECT id, ext, name, data FROM email_hosted_images
                               WHERE s3_key IS NULL OR s3_key = ''
                               ORDER BY created_at DESC LIMIT %s""",
                            (int(limit),),
                        )
                    rows = cur.fetchall() or []
                for row in rows:
                    if uploaded >= limit:
                        break
                    scanned += 1
                    img_id, ext, name, data = row[0], row[1], row[2], row[3]
                    item_id = str(img_id)
                    if hasattr(data, "tobytes"):
                        data = data.tobytes()
                    if not data:
                        _astore.record_outcome(
                            "hosted-emails", item_id, "skipped",
                            reason="source-missing",
                        )
                        continue
                    ext_clean = (ext or "png").lstrip(".").lower() or "png"
                    ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                             "gif": "image/gif", "webp": "image/webp"}.get(ext_clean, "image/png")
                    try:
                        res = _astore.put(
                            kind="hosted-emails",
                            key_hint=f"hosted-emails/{img_id}.{ext_clean}",
                            raw_bytes=bytes(data),
                            content_type=ctype,
                        )
                        if res.get("backend") == "s3" and res.get("key"):
                            if _set_hosted_image_s3(str(img_id), res["key"], res.get("url") or ""):
                                uploaded += 1
                                _astore.record_outcome(
                                    "hosted-emails", item_id, "uploaded",
                                    key=res["key"], bytes=len(bytes(data)),
                                )
                            else:
                                errors += 1
                                _astore.record_outcome(
                                    "hosted-emails", item_id, "error",
                                    reason="db-update-failed", key=res["key"],
                                )
                        else:
                            errors += 1
                            _astore.record_outcome(
                                "hosted-emails", item_id, "error",
                                reason=res.get("error") or "s3-put-failed",
                            )
                    except Exception as e:
                        logger.warning(f"[backfill] hosted-emails db {img_id} failed: {e}")
                        errors += 1
                        _astore.record_outcome(
                            "hosted-emails", item_id, "error", reason=repr(e),
                        )
        except Exception as e:
            logger.warning(f"[backfill] hosted-emails query failed: {e}")
            errors += 1
            _astore.record_outcome(
                "hosted-emails", only_id or "(query)", "error",
                reason=f"db-query-failed: {e}",
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    # 2) ``_hosted_<id>`` disk-fallback subdirs (rare path, used when DB is
    # unavailable). Sidecar marker ``.s3`` records that we mirrored already.
    try:
        from ai.publish_store import IMAGES_DIR as _IMG_DIR
        if os.path.isdir(_IMG_DIR):
            for fn in sorted(os.listdir(_IMG_DIR)):
                if uploaded >= limit:
                    break
                if not fn.startswith("_hosted_"):
                    continue
                disk_id = fn[len("_hosted_"):]
                if only_id and only_id not in (fn, disk_id):
                    continue
                fdir = os.path.join(_IMG_DIR, fn)
                if not os.path.isdir(fdir):
                    continue
                marker = os.path.join(fdir, ".s3")
                if os.path.isfile(marker):
                    _astore.record_outcome(
                        "hosted-emails", disk_id, "skipped",
                        reason="already-mirrored",
                    )
                    continue
                meta_path = os.path.join(fdir, "meta.json")
                data_path = os.path.join(fdir, "image.dat")
                if not (os.path.exists(meta_path) and os.path.exists(data_path)):
                    _astore.record_outcome(
                        "hosted-emails", disk_id, "skipped",
                        reason="source-missing",
                    )
                    continue
                scanned += 1
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                    with open(data_path) as f:
                        data_url = f.read()
                    m = _re.match(r"data:image/(\w+);base64,(.+)", data_url, _re.DOTALL)
                    if not m:
                        errors += 1
                        _astore.record_outcome(
                            "hosted-emails", disk_id, "error",
                            reason="not-a-data-url",
                        )
                        continue
                    ext = m.group(1).lower()
                    raw = _b64.b64decode(m.group(2), validate=False)
                    ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                             "gif": "image/gif", "webp": "image/webp"}.get(ext, f"image/{ext}")
                    img_id = (meta.get("id") or disk_id)
                    res = _astore.put(
                        kind="hosted-emails",
                        key_hint=f"hosted-emails/{img_id}.{ext}",
                        raw_bytes=raw,
                        content_type=ctype,
                    )
                    if res.get("backend") == "s3" and res.get("key"):
                        # Update DB row if present so serve route redirects.
                        _set_hosted_image_s3(str(img_id), res["key"], res.get("url") or "")
                        try:
                            with open(marker, "w") as f:
                                f.write(f"{res['key']}\n{res.get('url') or ''}\n")
                        except Exception:
                            pass
                        uploaded += 1
                        _astore.record_outcome(
                            "hosted-emails", str(img_id), "uploaded",
                            key=res["key"], bytes=len(raw),
                        )
                    else:
                        errors += 1
                        _astore.record_outcome(
                            "hosted-emails", str(img_id), "error",
                            reason=res.get("error") or "s3-put-failed",
                        )
                except Exception as e:
                    logger.warning(f"[backfill] hosted-emails disk {fn} failed: {e}")
                    errors += 1
                    _astore.record_outcome(
                        "hosted-emails", disk_id, "error", reason=repr(e),
                    )
    except Exception:
        pass
    return {"scanned": scanned, "uploaded": uploaded, "errors": errors}


def _backfill_announcements(limit: int, only_id: str = "") -> dict:
    from announcements_routes import UPLOAD_DIR as _ANN_DIR
    from integrations import attachment_store as _astore
    scanned = uploaded = errors = 0
    if not os.path.isdir(_ANN_DIR):
        return {"scanned": 0, "uploaded": 0, "errors": 0}
    for fn in sorted(os.listdir(_ANN_DIR)):
        if uploaded >= limit:
            break
        if fn.endswith(".s3"):
            continue
        if only_id and fn != only_id:
            continue
        full = os.path.join(_ANN_DIR, fn)
        if not os.path.isfile(full):
            if only_id:
                _astore.record_outcome(
                    "announcements", fn, "skipped", reason="source-missing",
                )
            continue
        if os.path.isfile(full + ".s3"):
            _astore.record_outcome(
                "announcements", fn, "skipped", reason="already-mirrored",
            )
            continue
        scanned += 1
        try:
            with open(full, "rb") as f:
                raw = f.read()
            import mimetypes as _m
            ctype = _m.guess_type(fn)[0] or "application/octet-stream"
            res = _astore.put(
                kind="announcements",
                key_hint=f"announcements/{fn}",
                raw_bytes=raw,
                content_type=ctype,
            )
            if res.get("backend") == "s3" and res.get("key"):
                try:
                    with open(full + ".s3", "w") as sc:
                        sc.write(f"{res['key']}\n{res.get('url') or ''}\n")
                    uploaded += 1
                    _astore.record_outcome(
                        "announcements", fn, "uploaded",
                        key=res["key"], bytes=len(raw),
                    )
                except Exception as e:
                    logger.warning(f"[backfill] announcements sidecar write {fn} failed: {e}")
                    errors += 1
                    _astore.record_outcome(
                        "announcements", fn, "error",
                        reason=f"sidecar-write-failed: {e}",
                        key=res.get("key"),
                    )
            else:
                errors += 1
                _astore.record_outcome(
                    "announcements", fn, "error",
                    reason=res.get("error") or "s3-put-failed",
                )
        except Exception as e:
            logger.warning(f"[backfill] announcements {fn} failed: {e}")
            errors += 1
            _astore.record_outcome(
                "announcements", fn, "error", reason=repr(e),
            )
    return {"scanned": scanned, "uploaded": uploaded, "errors": errors}


def _backfill_external_thumbs(limit: int, only_id: str = "") -> dict:
    from integrations.video_thumb import _CACHE_DIR as _XT_DIR  # type: ignore
    from integrations import attachment_store as _astore
    scanned = uploaded = errors = 0
    if not os.path.isdir(_XT_DIR):
        return {"scanned": 0, "uploaded": 0, "errors": 0}
    for fn in sorted(os.listdir(_XT_DIR)):
        if uploaded >= limit:
            break
        if not fn.endswith(".jpg"):
            continue
        if only_id and fn != only_id:
            continue
        full = os.path.join(_XT_DIR, fn)
        if not os.path.isfile(full):
            if only_id:
                _astore.record_outcome(
                    "external-thumbs", fn, "skipped", reason="source-missing",
                )
            continue
        # Sidecar marker so the auto-sweep doesn't re-upload bytes we've
        # already mirrored. Mirrors the announcements pattern.
        if os.path.isfile(full + ".s3"):
            _astore.record_outcome(
                "external-thumbs", fn, "skipped", reason="already-mirrored",
            )
            continue
        scanned += 1
        try:
            with open(full, "rb") as f:
                raw = f.read()
            res = _astore.put(
                kind="external-thumbs",
                key_hint=f"external-thumbs/{fn}",
                raw_bytes=raw,
                content_type="image/jpeg",
            )
            if res.get("backend") == "s3" and res.get("key"):
                # Match the announcements pattern: only count as uploaded
                # once the sidecar marker is durably written. Otherwise
                # we'd count success but the next sweep would re-upload
                # the same bytes (since the pending count keys off the
                # sidecar).
                try:
                    with open(full + ".s3", "w") as sc:
                        sc.write(f"{res['key']}\n{res.get('url') or ''}\n")
                    uploaded += 1
                    _astore.record_outcome(
                        "external-thumbs", fn, "uploaded",
                        key=res["key"], bytes=len(raw),
                    )
                except Exception as e:
                    logger.warning(f"[backfill] external-thumbs sidecar write {fn} failed: {e}")
                    errors += 1
                    _astore.record_outcome(
                        "external-thumbs", fn, "error",
                        reason=f"sidecar-write-failed: {e}",
                        key=res.get("key"),
                    )
            else:
                errors += 1
                _astore.record_outcome(
                    "external-thumbs", fn, "error",
                    reason=res.get("error") or "s3-put-failed",
                )
        except Exception as e:
            logger.warning(f"[backfill] external-thumbs {fn} failed: {e}")
            errors += 1
            _astore.record_outcome(
                "external-thumbs", fn, "error", reason=repr(e),
            )
    return {"scanned": scanned, "uploaded": uploaded, "errors": errors}


@app.route("/api/admin/attachments/backfill", methods=["POST"])
def attachments_backfill_endpoint():
    """Walk a single attachment kind and upload everything still on local disk
    to S3, recording the S3 key alongside the row.

    Category: Admin (gated by AMPLIFY_ADMIN_TOKEN)

    Query/body params:
    - kind (required): one of feature-images, videos, video-thumbs,
      external-thumbs, hosted-emails, announcements, all.
    - limit (int, default 50): max items per call. Re-run until pending=0.
    - admin_token: passed via header X-Admin-Token, query, or body.

    Response (per-kind report)::

        {"success": true, "kind": "feature-images",
         "scanned": N, "uploaded": N, "errors": N}
    """
    auth_err = _check_admin_auth()
    if auth_err is not None:
        return auth_err
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or request.args.get("kind") or "").strip().lower()
    try:
        limit = int(body.get("limit") or request.args.get("limit") or 50)
    except Exception:
        limit = 50
    limit = max(1, min(limit, 500))

    from integrations import attachment_store as _astore
    if not _astore.s3_enabled():
        return jsonify({
            "success": False,
            "error": "s3_disabled",
            "message": "Set AMPLIFY_IMAGE_STORAGE_BACKEND=s3 and the four S3_* secrets first.",
            "secrets_present": _astore.secrets_present(),
        }), 503

    runners = _backfill_runners()
    if kind == "all":
        report = {}
        for k, fn in runners.items():
            try:
                report[k] = fn(limit)
            except Exception as e:
                logger.exception(f"[backfill] kind={k} crashed")
                report[k] = {"error": str(e)}
        return jsonify({"success": True, "kind": "all", "report": report})

    runner = runners.get(kind)
    if runner is None:
        return jsonify({
            "success": False,
            "error": "unknown_kind",
            "kind": kind,
            "valid": list(runners.keys()) + ["all"],
        }), 400
    try:
        report = runner(limit)
    except Exception as e:
        logger.exception(f"[backfill] kind={kind} crashed")
        return jsonify({"success": False, "kind": kind, "error": str(e)}), 500
    return jsonify({"success": True, "kind": kind, **report})


@app.route("/api/admin/attachments/retry", methods=["POST"])
def attachments_retry_endpoint():
    """Re-run the per-kind backfill helper for a single item (Task #112).

    Category: Admin (gated by AMPLIFY_ADMIN_TOKEN)

    Body / query params:
    - ``kind``: one of feature-images, videos, video-thumbs,
      external-thumbs, hosted-emails, announcements.
    - ``item_id``: the item identifier as it appears in the issues list
      (feature_id, video folder name, hosted-image id, filename, etc).

    Returns the helper's report plus the freshest recorded outcome for
    that (kind, item_id) so the dashboard can update the row in place::

        {"success": true, "kind": ..., "item_id": ...,
         "report": {scanned, uploaded, errors, ...},
         "outcome": {ts, outcome, reason, key?, ...} | null}
    """
    auth_err = _check_admin_auth()
    if auth_err is not None:
        return auth_err
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or request.args.get("kind") or "").strip().lower()
    item_id = (body.get("item_id") or request.args.get("item_id") or "").strip()
    if not kind or kind not in _ATTACHMENT_KINDS:
        return jsonify({
            "success": False,
            "error": "unknown_kind",
            "kind": kind,
            "valid": list(_ATTACHMENT_KINDS),
        }), 400
    if not item_id:
        return jsonify({"success": False, "error": "missing_item_id"}), 400

    from integrations import attachment_store as _astore
    if not _astore.s3_enabled():
        return jsonify({
            "success": False,
            "error": "s3_disabled",
            "message": "Set AMPLIFY_IMAGE_STORAGE_BACKEND=s3 and the four S3_* secrets first.",
            "secrets_present": _astore.secrets_present(),
        }), 503

    try:
        report = _backfill_one_item(kind, item_id)
    except Exception as e:
        logger.exception(f"[backfill-retry] kind={kind} item={item_id} crashed")
        _astore.record_outcome(kind, item_id, "error", reason=repr(e))
        return jsonify({
            "success": False, "kind": kind, "item_id": item_id,
            "error": str(e),
            "outcome": _astore.latest_outcome_for(kind, item_id),
        }), 500
    return jsonify({
        "success": True,
        "kind": kind,
        "item_id": item_id,
        "report": report,
        "outcome": _astore.latest_outcome_for(kind, item_id),
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


@app.route("/api/feature-url/override", methods=["POST"])
def add_feature_url_override():
    """Save a human correction to a feature's Chartmetric URL so future
    inference can learn from it. The new URL becomes the source of truth
    for this feature across users (it is no longer just a per-user
    localStorage value).

    Category: Feedback Loop

    Body:
    {
        "feature_id": "abc123",
        "feature_title": "Genius Charts page",
        "original_url": "https://app.chartmetric.com/charts/spotify",
        "new_url": "https://app.chartmetric.com/charts/genius/top-tracks",
        "reason": "Feature is about Genius charts, not Spotify"
    }

    Response: {"success": true, "entry": {...}}
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    feature_id = (data.get("feature_id") or "").strip()
    feature_title = (data.get("feature_title") or "").strip()
    original_url = (data.get("original_url") or "").strip()
    new_url = (data.get("new_url") or "").strip()
    reason = (data.get("reason") or "").strip()

    if not feature_id:
        return jsonify({"error": "feature_id is required"}), 400
    if not new_url:
        return jsonify({"error": "new_url is required"}), 400

    entry = save_feature_url_override(feature_id, feature_title, original_url, new_url, reason)
    return jsonify({
        "success": True,
        "message": "Feature URL correction saved and will improve future URL inference",
        "entry": entry,
    })


@app.route("/api/feature-url/overrides")
def list_feature_url_overrides():
    """List all feature URL override history, most recent first.

    Category: Feedback Loop

    Response: {"overrides": [...], "count": 5}
    """
    overrides = get_feature_url_overrides()
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

    _bg_raw = data.get("bypass_guardrail", False)
    if isinstance(_bg_raw, bool):
        bypass_guardrail = _bg_raw
    else:
        bypass_guardrail = str(_bg_raw).strip().lower() in ("1", "true", "yes", "on")

    if feature_id in CLASSIFICATION_CACHE:
        del CLASSIFICATION_CACHE[feature_id]

    remove_manual_override(feature_id)

    classification = classify_feature(feature_data, force_claude=True, bypass_guardrail=bypass_guardrail)
    return jsonify({
        "success": True,
        "classification": classification,
        "method": "claude",
        "bypass_guardrail": bypass_guardrail,
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
    try:
        result = publish_tweet(content, image_base64=image_base64)
    except Exception as _e:
        logger.exception(f"[publish/twitter] UNCAUGHT type={type(_e).__name__} repr={_e!r}")
        return jsonify({"success": False, "error": f"Server error: {type(_e).__name__}: {_e}"}), 500
    if result.get("success") and result.get("method") == "api" and feature_id:
        mark_published(feature_id, "twitter", tweet_url=result.get("tweet_url"))
    status_code = 200 if result.get("success") else 500
    if result.get("success"):
        logger.info(f"[publish/twitter] OK method={result.get('method')!r} tweet_url={result.get('tweet_url','')!r}")
    else:
        logger.error(f"[publish/twitter] FAIL status={status_code} error={result.get('error')!r} full_result={result}")
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


def _filter_videos_to_body_refs(video_map: dict, body: str) -> dict:
    """Restrict the video map to those whose ``[video: name]`` marker appears
    in the email body. Without this, any video saved against a feature gets
    silently MIME-attached to every send, even after the user removed the
    marker from the body — so a video uploaded once would haunt every email
    for that feature.

    Matching is fuzzy (see ``integrations.sendgrid_client._normalize_video_key``):
    case-insensitive, whitespace-collapsed, and tolerant of browser
    ``(N)`` dedup suffixes. A strict exact match here would drop the video
    from the map before the renderer ever saw it, surfacing as a
    "not attached" line even when the upload landed cleanly under a
    near-identical filename.
    """
    if not video_map:
        return {}
    if not body:
        return {}
    import re as _re
    from integrations.sendgrid_client import _normalize_video_key
    refs = set(
        m.group(1).strip()
        for m in _re.finditer(r'\[video:\s*([^\]]+)\]', body or "", flags=_re.IGNORECASE)
    )
    if not refs:
        return {}
    norm_refs = {_normalize_video_key(r) for r in refs if r}
    return {
        name: info
        for name, info in video_map.items()
        if name in refs or _normalize_video_key(name) in norm_refs
    }


def _build_video_map(feature_id):
    if not feature_id:
        return {}
    vids = list_feature_videos(feature_id)
    if not vids:
        return {}
    # Use the same base-URL resolution as the email renderer so links in
    # delivered emails always point at the public production host. Falling
    # back to `localhost:5000` here would silently ship a broken video to
    # every recipient — log loudly if that ever happens so we notice.
    from integrations.sendgrid_client import _get_base_url
    base_url = _get_base_url().rstrip("/")
    if base_url.startswith("http://localhost"):
        logger.warning(
            "[publish/video] _build_video_map: no REPLIT_DEPLOYMENT_URL or "
            "REPLIT_DEV_DOMAIN set — outgoing video links will point at "
            f"{base_url!r} and will not be reachable from recipients."
        )
    fallback_thumb = f"{base_url}/api/placeholder/video-thumb"
    video_map = {}
    for v in vids:
        vid_id = v.get("video_id", "")
        fname = v.get("filename", "")
        if vid_id and fname:
            has_thumb = v.get("has_thumb", True)
            if v.get("is_url"):
                ext_thumb = v.get("external_thumb_url") or ""
                video_map[fname] = {
                    "thumb_url": ext_thumb or fallback_thumb,
                    "video_url": v.get("external_url") or "",
                }
            else:
                video_map[fname] = {
                    "thumb_url": f"{base_url}/api/videos/{vid_id}/thumb" if has_thumb else fallback_thumb,
                    "video_url": f"{base_url}/api/videos/{vid_id}",
                }
    return video_map


@app.route("/api/publish/email", methods=["POST"])
def publish_email():
    from integrations.sendgrid_client import send_email
    import time as _time
    _t0 = _time.time()

    data = request.get_json() or {}
    content = data.get("content", "").strip()
    _to_dbg = (data.get("to_email", "") or "").strip()
    _audience_dbg = data.get("audience_id", "") or data.get("audienceId", "")
    _tpl_dbg = (data.get("template_id", "") or "").strip()
    _imgs_dbg = data.get("images") or {}
    _fids_dbg = data.get("feature_ids") if isinstance(data.get("feature_ids"), list) else ([data.get("feature_id")] if data.get("feature_id") else [])
    logger.info(
        f"[publish/email] REQ channel={data.get('channel','')!r} is_test={data.get('is_test', True)} "
        f"content_len={len(content)} subject_len={len((data.get('subject','') or '').strip())} "
        f"to_email_len={len(_to_dbg)} recipients_count={len([e for e in _to_dbg.split(',') if e.strip()])} "
        f"audience_id={_audience_dbg!r} template_id={_tpl_dbg!r} "
        f"images={len(_imgs_dbg) if isinstance(_imgs_dbg, dict) else 'n/a'} feature_ids={_fids_dbg}"
    )
    if not content:
        logger.warning("[publish/email] REJECT empty content")
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
    # Forward the selected Resend audience so the send loop can mint a
    # personal, signed unsubscribe URL for each recipient and add the
    # List-Unsubscribe header. Empty / missing means "test send" or
    # "custom typed-in addresses" — no unsubscribe link rendered.
    audience_id = (data.get("audience_id") or data.get("audienceId") or "").strip() or None
    # Per-topic opt-out (Task #77). Required when sending to an
    # audience so the unsubscribe link/button has a topic to flip and
    # so we can drop topic-opt-outs before dispatch. Test sends and
    # custom one-off addresses ignore topic_id (they don't render a
    # link in the first place).
    topic_id = (data.get("topic_id") or data.get("topicId") or "").strip() or None
    if audience_id and not topic_id:
        logger.warning("[publish/email] REJECT audience send without topic_id (per-topic opt-out is required)")
        return jsonify({
            "success": False,
            "error": "Pick a topic for this audience send. Recipients use the topic to opt out of one type of email without losing the rest.",
        }), 400

    try:
        if feature_ids:
            videos = {}
            for fid in feature_ids:
                videos.update(_build_video_map(fid))
        else:
            videos = _build_video_map(feature_id)
        # Only attach videos that the body actually references; otherwise a
        # previously-uploaded-then-removed video silently re-attaches to
        # every send.
        videos_before = len(videos)
        videos = _filter_videos_to_body_refs(videos, content)
        if videos_before and len(videos) < videos_before:
            logger.info(
                f"[publish/email] Filtered out {videos_before - len(videos)} unreferenced video(s) "
                f"(kept {len(videos)} referenced in body)"
            )
        logger.info(f"[publish/email] videos_attached={len(videos)} keys={list(videos.keys())[:5]}")
        result = send_email(subject=subject, body=content, to_email=to_email, is_test=is_test, images=images, from_name=from_name, template_id=template_id, videos=videos, bcc_email=bcc_email, audience_id=audience_id, topic_id=topic_id)
    except Exception as _e:
        logger.exception(f"[publish/email] UNCAUGHT exception during send: type={type(_e).__name__} repr={_e!r}")
        return jsonify({"success": False, "error": f"Server error: {type(_e).__name__}: {_e}"}), 500

    if result.get("success") and result.get("method") in ("sendgrid", "resend"):
        if feature_ids:
            for fid in feature_ids:
                mark_published(fid, channel)
        elif feature_id:
            mark_published(feature_id, channel)
    if not result.get("success") and (result.get("missing_images") or result.get("missing_videos")):
        # Unresolved [image:]/[video:] markers — return 400 so the UI
        # treats it as a user-fixable validation error, not a server
        # crash, and surfaces the names of the offending markers.
        status_code = 400
    elif not result.get("success") and (result.get("topic_filtered") or result.get("topic_filter_error")):
        # Per-topic opt-out outcomes are handled business outcomes
        # (everyone opted out, or we couldn't verify subscription
        # state and fail-safed). They are not server crashes — 400
        # so the UI shows the message inline instead of a generic
        # "server error" toast.
        status_code = 400
    else:
        status_code = 200 if result.get("success") else 500
    _dt = (_time.time() - _t0) * 1000
    if result.get("success"):
        logger.info(f"[publish/email] OK method={result.get('method')!r} count={result.get('count')} id={result.get('message_id','')!r} dt={_dt:.0f}ms")
    else:
        logger.error(f"[publish/email] FAIL status={status_code} error={result.get('error')!r} method={result.get('method','')!r} dt={_dt:.0f}ms full_result={result}")
    return jsonify(result), status_code


@app.route("/api/publish/email/preview", methods=["GET", "POST"])
def preview_email():
    from integrations.sendgrid_client import render_email_html, _build_hosted_image_map

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

    # Note: when a Resend template_id is selected, the frontend fetches the
    # template HTML directly from /api/resend/templates/<id>/preview (JSON)
    # and renders its own iframe + loading/error UI. We still keep this
    # endpoint focused on locally-rendered email previews and ignore
    # template_id here on purpose.

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
    # Match the send path: only show videos the body actually references.
    videos = _filter_videos_to_body_refs(videos, content)
    # Mint a token + persist the preview so the footer's "View in
    # browser" link works from the in-app preview too. The actual send
    # path mints a separate token, so test previews don't collide with
    # real sent messages.
    import secrets as _secrets
    from integrations.sendgrid_client import _save_hosted_email, _build_view_in_browser_url
    view_token = _secrets.token_urlsafe(16)
    view_url = _build_view_in_browser_url(view_token)
    # Convert inline data: image payloads to hosted URLs via the same
    # storage seam the send path uses. Without this step the rendered
    # preview HTML (and the "View in browser" snapshot we save below)
    # would embed every image as base64, blowing past the 1 MB limit
    # that downstream tools (Resend, Litmus, manual copy-paste) impose
    # on email HTML.
    hosted_images = _build_hosted_image_map(images) if images else None

    # Show the "Unsubscribe" link in the preview footer so the marketer
    # can verify it's there before sending. Real sends substitute a
    # signed per-recipient URL into UNSUBSCRIBE_PLACEHOLDER; for preview
    # we point at "#" so the link renders but clicking it does nothing
    # (the preview has no recipient context to unsubscribe).
    html = render_email_html(
        subject,
        content,
        images=hosted_images,
        from_name=from_name,
        videos=videos,
        view_url=view_url,
        unsubscribe_placeholder="#",
    )
    _save_hosted_email(view_token, html)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/email/view/<token>", methods=["GET"])
def view_hosted_email(token):
    """Serve the rendered HTML of a previously-sent (or previewed) email.

    Tokens are random base64url strings minted in `send_email` /
    `preview_email`. They're unguessable, so we don't require auth, but
    we also don't expose any listing endpoint.
    """
    from integrations.sendgrid_client import load_hosted_email
    html = load_hosted_email(token)
    if html is None:
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>Email not available</title></head>"
            "<body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
            "max-width:520px;margin:80px auto;padding:0 24px;color:#333;line-height:1.6;\">"
            "<h2 style='margin:0 0 12px 0;color:#1a1d23;'>This email is no longer available</h2>"
            "<p style='margin:0;color:#666;'>The link may have expired or the message was never sent. "
            "If you believe this is a mistake, contact the sender.</p>"
            "</body></html>",
            404,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


def _render_unsubscribe_page(title: str, heading: str, body_html: str, status: int = 200):
    """Render a minimal Resend-style hosted page for unsubscribe flows."""
    page = f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1.0'>
<title>{title}</title></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1a1d23;">
<table width='100%' cellpadding='0' cellspacing='0' style='padding:48px 16px;'><tr><td align='center'>
<table width='480' cellpadding='0' cellspacing='0' style='max-width:480px;width:100%;background:#ffffff;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,0.06);'>
<tr><td style='padding:32px 32px 12px 32px;text-align:left;'>
<div style='font-size:13px;color:#7a7f8a;letter-spacing:0.4px;text-transform:uppercase;font-weight:700;margin-bottom:12px;'>Chartmetric</div>
<h1 style='margin:0 0 12px 0;font-size:22px;line-height:1.3;color:#1a1d23;'>{heading}</h1>
</td></tr>
<tr><td style='padding:0 32px 32px 32px;color:#3a3f4a;font-size:15px;line-height:1.6;'>
{body_html}
</td></tr>
</table>
</td></tr></table>
</body></html>"""
    return page, status, {"Content-Type": "text/html; charset=utf-8"}


def _mask_email_for_display(email: str) -> str:
    """Show "j****@example.com" so the page confirms identity without
    exposing the full address to a casual onlooker.
    """
    if not email or "@" not in email:
        return email or ""
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"{local}***@{domain}"
    return f"{local[0]}{'*' * max(3, len(local) - 1)}@{domain}"


@app.route("/email/unsubscribe", methods=["GET", "POST"])
def email_unsubscribe():
    """Hosted unsubscribe flow.

    GET  -> confirmation page with a single "Confirm unsubscribe" button.
    POST -> performs the unsubscribe via Resend's contact API. Also serves
            the RFC 8058 one-click POST from mail clients (Gmail / Apple
            Mail), which arrives without a session and without a CSRF
            token. Auth is the signed token in the URL itself; nothing
            else is required.
    """
    from integrations.sendgrid_client import (
        verify_unsubscribe_token,
        unsubscribe_resend_contact,
        unsubscribe_resend_topic,
        get_resend_topic,
    )
    token = (request.values.get("token") or "").strip()
    token_dbg = f"<{token[:4]}...>" if token else "<empty>"
    payload = verify_unsubscribe_token(token)
    if not payload:
        logger.warning(f"[unsubscribe] {request.method} invalid/tampered token={token_dbg}")
        return _render_unsubscribe_page(
            "Unsubscribe link not valid",
            "This unsubscribe link is not valid",
            "<p style='margin:0;'>The link may have been altered, copied incorrectly, or is from an old test message. "
            "If you keep receiving emails you do not want, reply to the sender directly.</p>",
            status=400,
        )

    email = payload["email"]
    audience_id = payload["audience_id"]
    topic_id = payload.get("topic_id", "") or ""
    masked = _mask_email_for_display(email)

    # Look the topic up once (per request) so both GET and POST can show
    # the topic name. Fail soft: if the topic was deleted in Resend or
    # the API is briefly down, we still let the unsubscribe proceed
    # using a generic "this type of email" wording.
    topic_name = ""
    if topic_id:
        topic = get_resend_topic(topic_id)
        if topic:
            topic_name = (topic.get("name") or "").strip()

    if request.method == "GET":
        action_url = f"/email/unsubscribe?token={token}"
        if topic_id:
            scope_label = f"<strong>{html.escape(topic_name)}</strong> emails" if topic_name else "this type of email"
            heading = html.escape(f"Confirm unsubscribe from {topic_name} emails") if topic_name else "Confirm unsubscribe"
            intro = (
                f"<p style='margin:0 0 18px 0;'>You are about to unsubscribe <strong>{masked}</strong> "
                f"from {scope_label} from Chartmetric. You will keep receiving other Chartmetric emails "
                "you have signed up for.</p>"
            )
        else:
            # Legacy token (audience-wide unsubscribe). Old links in old
            # inboxes still work and still flip the audience flag below.
            heading = "Confirm your unsubscribe"
            intro = (
                f"<p style='margin:0 0 18px 0;'>You are about to unsubscribe <strong>{masked}</strong> "
                "from this Chartmetric mailing list. You will not receive future emails from this list once you confirm.</p>"
            )
        body_html = (
            intro
            + f"<form method='POST' action='{action_url}' style='margin:0;'>"
            "<button type='submit' style='display:inline-block;background:#1a1d23;color:#ffffff;border:none;"
            "padding:12px 22px;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer;'>"
            "Confirm unsubscribe</button>"
            "</form>"
            "<p style='margin:18px 0 0 0;color:#7a7f8a;font-size:13px;'>"
            "Changed your mind? Just close this page.</p>"
        )
        return _render_unsubscribe_page(
            "Confirm unsubscribe",
            heading,
            body_html,
        )

    # POST — perform the unsubscribe.
    # Per-topic path (current). When the token carries a topic id we flip
    # the contact's subscription for that topic to opt_out. State is
    # workspace-level in Resend, so the same topic in any audience now
    # excludes this contact at our send-time filter.
    if topic_id:
        result = unsubscribe_resend_topic(email, topic_id)
        if result.get("success"):
            logger.info(
                f"[unsubscribe] OK (topic) token={token_dbg} topic=<{topic_id[:8]}...> "
                f"email_prefix={email[:3]!r}"
            )
            scope_label = f"<strong>{html.escape(topic_name)}</strong> emails" if topic_name else "these emails"
            return _render_unsubscribe_page(
                "You have been unsubscribed",
                "You have been unsubscribed",
                f"<p style='margin:0 0 12px 0;'><strong>{masked}</strong> will no longer receive {scope_label} from Chartmetric.</p>"
                "<p style='margin:0;color:#7a7f8a;font-size:14px;'>You can close this page now.</p>",
            )
        status = result.get("status", "update_failed")
        logger.warning(
            f"[unsubscribe] FAIL (topic) token={token_dbg} topic=<{topic_id[:8]}...> "
            f"email_prefix={email[:3]!r} status={status} error={result.get('error')!r}"
        )
        # Topic-path errors share the legacy error pages below by
        # falling through to the same status handling.
    elif not audience_id:
        # Tokens minted for a test/custom send that should not have
        # produced a link, or somehow stripped of both fields.
        logger.warning(f"[unsubscribe] POST token={token_dbg} has no audience_id and no topic_id; nothing to update")
        return _render_unsubscribe_page(
            "You have been unsubscribed",
            "You have been unsubscribed",
            "<p style='margin:0;'>This was a one-off message and is not part of an audience. "
            "We will not send you any more emails from this thread.</p>",
        )
    else:
        # Legacy fallback: tokens minted before Task #77 only carry
        # audience_id. Honor them by flipping the audience-wide
        # unsubscribed flag exactly like before.
        result = unsubscribe_resend_contact(audience_id, email)
        if result.get("success"):
            logger.info(
                f"[unsubscribe] OK (legacy audience) token={token_dbg} audience=<{audience_id[:8]}...> "
                f"email_prefix={email[:3]!r}"
            )
            return _render_unsubscribe_page(
                "You have been unsubscribed",
                "You have been unsubscribed",
                f"<p style='margin:0 0 12px 0;'><strong>{masked}</strong> has been removed from this mailing list.</p>"
                "<p style='margin:0;color:#7a7f8a;font-size:14px;'>You can close this page now.</p>",
            )
        status = result.get("status", "update_failed")
        logger.warning(
            f"[unsubscribe] FAIL (legacy audience) token={token_dbg} audience=<{audience_id[:8]}...> "
            f"email_prefix={email[:3]!r} status={status} error={result.get('error')!r}"
        )
    if status == "not_found":
        # Already removed from the audience — treat as success from the
        # recipient's perspective so they don't keep retrying.
        return _render_unsubscribe_page(
            "You are already unsubscribed",
            "You are already unsubscribed",
            f"<p style='margin:0;'>We do not have <strong>{masked}</strong> on this list anymore. "
            "No further action is needed.</p>",
        )
    if status == "not_configured":
        return _render_unsubscribe_page(
            "Unsubscribe temporarily unavailable",
            "We could not process your unsubscribe right now",
            "<p style='margin:0;'>Our email provider is not reachable. Please try again in a few minutes "
            "or reply to the sender directly to be removed.</p>",
            status=503,
        )
    return _render_unsubscribe_page(
        "Unsubscribe failed",
        "Something went wrong",
        "<p style='margin:0;'>We could not unsubscribe you right now. Please try again in a few minutes "
        "or reply to the sender directly to be removed.</p>",
        status=500,
    )


@app.route("/api/resend/templates/<template_id>/preview", methods=["GET"])
def get_resend_template_preview(template_id):
    """Read-only: fetch a Resend template's name and HTML body.

    Success: {"success": true, "id": "...", "name": "...", "html": "..."}
    Failure: {"success": false, "error": {"status": "<kind>", "message": "..."}}
    Always 200 so the frontend can render its own error card; errors are in the body.
    """
    from integrations.sendgrid_client import get_resend_template
    tid = (template_id or "").strip()
    if not tid:
        return jsonify({"success": False, "error": {"status": "invalid_request", "message": "Template ID is required"}}), 200
    result = get_resend_template(tid)
    if result.get("success"):
        return jsonify({"success": True, "id": result.get("id", tid), "name": result.get("name", "") or tid, "html": result.get("html", "") or ""}), 200
    err_msg = (result.get("error") or "").lower()
    if "not configured" in err_msg or "missing" in err_msg:
        kind = "not_configured"
    elif "not found" in err_msg or "404" in err_msg:
        kind = "not_found"
    else:
        kind = "fetch_failed"
    return jsonify({"success": False, "id": tid, "error": {"status": kind, "message": result.get("error", "Unknown error")}}), 200


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


@app.route("/api/resend/topics", methods=["GET"])
def get_resend_topics():
    """Return workspace-level Resend topics for the dashboard's topic
    picker. Topics are managed in the Resend dashboard; this endpoint is
    read-only.
    """
    from integrations.sendgrid_client import list_resend_topics
    topics = list_resend_topics()
    return jsonify({"success": True, "topics": topics}), 200


@app.route("/api/resend/audiences/<audience_id>/contacts", methods=["GET"])
def get_resend_contacts(audience_id):
    from integrations.sendgrid_client import (
        list_resend_contacts,
        filter_emails_by_topic_subscription,
    )
    contacts = list_resend_contacts(audience_id)
    # First filter: drop audience-wide unsubscribes. The audience flag
    # remains a hard kill switch even after Task #77 introduced
    # per-topic opt-out — if a contact has both, either one excludes
    # them. The flag is flipped by the legacy unsubscribe path (old
    # tokens still in inboxes from before Task #77).
    subscribed = [c for c in contacts if not c.get("unsubscribed", False) and c.get("email")]
    # Second filter (optional, per topic): when the dashboard passes
    # `?topic_id=...`, also drop anyone who has explicitly opted out of
    # that topic. Without it, callers (for example the BCC segment
    # picker) get the unfiltered audience-eligible list.
    topic_id = (request.args.get("topic_id") or "").strip()
    if topic_id:
        emails = [c.get("email", "") for c in subscribed]
        topic_filter = filter_emails_by_topic_subscription(emails, topic_id)
        if not topic_filter.get("ok"):
            # Fail-safe: don't show a count we can't trust. The badge
            # surfaces the error and the send button will block.
            logger.warning(
                f"[contacts] Topic filter could not resolve all subscribers "
                f"for audience=<{audience_id[:8]}...> topic=<{topic_id[:8]}...>: "
                f"errors={topic_filter.get('errors')}"
            )
            return jsonify({
                "success": False,
                "error": topic_filter.get("error") or "Could not verify topic subscriptions.",
                "topic_filter_error": True,
            }), 502
        kept_set = set(topic_filter.get("kept") or [])
        subscribed = [c for c in subscribed if c.get("email", "") in kept_set]
    out = [{"email": c.get("email", "")} for c in subscribed]
    return jsonify({"success": True, "contacts": out, "total": len(out)}), 200


@app.route("/api/publish/inapp", methods=["POST"])
def publish_inapp():
    from integrations.inapp_client import publish_announcement
    import re
    import time as _time
    _t0 = _time.time()

    data = request.get_json() or {}
    content = data.get("content", "").strip()
    logger.info(
        f"[publish/inapp] REQ feature_id={data.get('feature_id','')!r} category={data.get('category','')!r} "
        f"feature_title_len={len(data.get('feature_title','') or '')} content_len={len(content)}"
    )
    if not content:
        logger.warning("[publish/inapp] REJECT empty content")
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

    try:
        result = publish_announcement(title=title, body=body, feature_id=feature_id, category=category)
    except Exception as _e:
        logger.exception(f"[publish/inapp] UNCAUGHT exception: type={type(_e).__name__} repr={_e!r}")
        return jsonify({"success": False, "error": f"Server error: {type(_e).__name__}: {_e}"}), 500

    if result.get("success") and feature_id:
        mark_published(feature_id, "inapp")
    status_code = 200 if result.get("success") else 500
    _dt = (_time.time() - _t0) * 1000
    if result.get("success"):
        logger.info(f"[publish/inapp] OK ann_id={result.get('id','')!r} dt={_dt:.0f}ms")
    else:
        logger.error(f"[publish/inapp] FAIL status={status_code} error={result.get('error')!r} dt={_dt:.0f}ms full_result={result}")
    return jsonify(result), status_code


@app.route("/api/publish/notion", methods=["POST"])
def publish_notion_endpoint():
    """Publish content to Notion (Marketing Newsletter or All Hands page).

    Request body:
        {
            "channel": "email_newsletter" | "notion_monthly",
            "content": "...",
            "feature_id": "...",
            "feature_title": "...",
            "feature_url": "https://app.chartmetric.com/..."  // optional
        }
    """
    from integrations.notion_client import publish_to_notion
    import time as _time
    _t0 = _time.time()

    data = request.get_json() or {}
    channel = (data.get("channel") or "").strip()
    content = (data.get("content") or "").strip()
    feature_id = data.get("feature_id", "")
    feature_title = data.get("feature_title", "")
    feature_url = data.get("feature_url") or None

    logger.info(
        f"[publish/notion] REQ channel={channel!r} feature_id={feature_id!r} "
        f"feature_title_len={len(feature_title or '')} content_len={len(content)}"
    )
    if not channel or channel not in ("email_newsletter", "notion_monthly"):
        return jsonify({"success": False, "error": f"Unsupported channel: {channel!r}"}), 400
    if not content:
        logger.warning("[publish/notion] REJECT empty content")
        return jsonify({"success": False, "error": "content is required"}), 400

    try:
        result = publish_to_notion(
            content=content,
            channel=channel,
            feature_title=feature_title,
            feature_url=feature_url,
        )
    except Exception as _e:
        logger.exception(f"[publish/notion] UNCAUGHT exception: type={type(_e).__name__} repr={_e!r}")
        return jsonify({"success": False, "error": f"Server error: {type(_e).__name__}: {_e}"}), 500

    if result.get("success") and feature_id:
        mark_published(feature_id, channel, page_url=result.get("page_url"))

    status_code = 200 if result.get("success") else 500
    _dt = (_time.time() - _t0) * 1000
    if result.get("success"):
        logger.info(
            f"[publish/notion] OK channel={channel!r} dest={result.get('destination')!r} "
            f"page_id={result.get('page_id')!r} blocks={result.get('block_count')} dt={_dt:.0f}ms"
        )
    else:
        logger.error(
            f"[publish/notion] FAIL channel={channel!r} status={status_code} "
            f"error={result.get('error')!r} dt={_dt:.0f}ms"
        )
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
    import re as _re

    announcements = get_announcements(limit=5, status="active")
    cards_html = ""
    if not announcements:
        cards_html = '<div class="empty">No announcements yet</div>'
    else:
        for ann in announcements:
            cat = ann.get("category", "") or ""
            cat_badge = f'<span class="cat-badge">{html_mod.escape(cat.replace("_", " ").title())}</span>' if cat else ""
            ts = ann.get("published_at", "")[:16].replace("T", " ")
            # Escape first (safe), then convert markdown bold **...** to
            # <strong> so the bolded subtitle line and any inline emphasis
            # render as bold instead of showing literal asterisks. Asterisks
            # are not HTML metacharacters, so escape leaves them intact for
            # this regex to match.
            body_escaped = html_mod.escape(ann.get("body", ""))
            body_escaped = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body_escaped)
            body_lines = body_escaped.replace("\n", "<br>")
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


@app.route("/api/content/cache-index", methods=["GET"])
def get_content_cache_index_endpoint():
    return jsonify(get_content_cache_index()), 200


@app.route("/api/publish/image", methods=["POST"])
def save_image_endpoint():
    data = request.get_json() or {}
    feature_id = data.get("feature_id", "")
    channel = data.get("channel", "")
    data_url = data.get("dataUrl", "")
    image_url = data.get("url", "")
    is_url = bool(data.get("isUrl")) or (image_url and not data_url)
    is_gif = bool(data.get("isGif"))
    filename = data.get("name", "image.png")
    file_size = data.get("size", 0)
    if not feature_id:
        return jsonify({"success": False, "error": "feature_id required"}), 400
    if is_url and image_url:
        try:
            save_publish_image(feature_id, channel, image_url, filename or image_url, file_size, is_gif=is_gif)
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        return jsonify({"success": True, "kind": "url"}), 200
    if not data_url:
        return jsonify({"success": False, "error": "dataUrl or url required"}), 400
    try:
        save_publish_image(feature_id, channel, data_url, filename, file_size, is_gif=is_gif)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "kind": "data"}), 200


_hosted_images_db_initialized = False


def _ensure_hosted_images_table(conn) -> bool:
    """Create the email_hosted_images table on first use; idempotent."""
    global _hosted_images_db_initialized
    if _hosted_images_db_initialized:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS email_hosted_images (
                    id TEXT PRIMARY KEY,
                    ext TEXT NOT NULL,
                    name TEXT,
                    data BYTEA NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            # S3 columns added by Task #99 so the serve route can 302 to
            # the bucket and the backfill can mark which rows it migrated.
            cur.execute("ALTER TABLE email_hosted_images ADD COLUMN IF NOT EXISTS s3_key TEXT")
            cur.execute("ALTER TABLE email_hosted_images ADD COLUMN IF NOT EXISTS s3_url TEXT")
            conn.commit()
        _hosted_images_db_initialized = True
        return True
    except Exception as e:
        logger.warning(f"[hosted-images] CREATE TABLE failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def save_hosted_image_db(img_id: str, ext: str, name: str, raw: bytes) -> bool:
    """Persist a hosted image to Postgres. Returns True on success.

    Imported by ``integrations.sendgrid_client._build_hosted_image_map`` so
    that hosted email images survive deploys (the container disk does not).
    """
    import time as _time
    if not img_id or not raw:
        return False
    conn = _drafts_db_conn()
    if conn is None:
        return False
    try:
        if not _ensure_hosted_images_table(conn):
            return False
        with conn.cursor() as cur:
            import psycopg2 as _pg2  # type: ignore
            cur.execute(
                """
                INSERT INTO email_hosted_images (id, ext, name, data, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    ext = EXCLUDED.ext,
                    name = EXCLUDED.name,
                    data = EXCLUDED.data
                """,
                (str(img_id), str(ext or "png"), str(name or "")[:200],
                 _pg2.Binary(raw), float(_time.time())),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.warning(f"[hosted-images] DB save failed for {img_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_hosted_image_db(img_id: str):
    """Return ``(ext, raw_bytes)`` for a stored image, or None if missing."""
    conn = _drafts_db_conn()
    if conn is None:
        return None
    try:
        if not _ensure_hosted_images_table(conn):
            return None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ext, data FROM email_hosted_images WHERE id = %s",
                (str(img_id),),
            )
            row = cur.fetchone()
        if not row:
            return None
        ext, data = row[0], row[1]
        if hasattr(data, "tobytes"):
            data = data.tobytes()
        return (ext or "png", bytes(data))
    except Exception as e:
        logger.warning(f"[hosted-images] DB load failed for {img_id}: {e}")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_hosted_image_s3_meta(img_id: str):
    """Return ``(s3_key, s3_url, ext)`` for a stored image, or None.

    Used by ``serve_hosted_image`` to 302 to the bucket when the row was
    backfilled to S3.
    """
    conn = _drafts_db_conn()
    if conn is None:
        return None
    try:
        if not _ensure_hosted_images_table(conn):
            return None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s3_key, s3_url, ext FROM email_hosted_images WHERE id = %s",
                (str(img_id),),
            )
            row = cur.fetchone()
        if not row:
            return None
        return (row[0] or "", row[1] or "", row[2] or "png")
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _set_hosted_image_s3(img_id: str, s3_key: str, s3_url: str) -> bool:
    """Record an S3 key/url against an existing email_hosted_images row."""
    conn = _drafts_db_conn()
    if conn is None:
        return False
    try:
        if not _ensure_hosted_images_table(conn):
            return False
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE email_hosted_images SET s3_key = %s, s3_url = %s WHERE id = %s",
                (s3_key or None, s3_url or None, str(img_id)),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.warning(f"[hosted-images] DB update s3 failed for {img_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.route("/api/publish/image/hosted/<img_id>")
def serve_hosted_image(img_id):
    import base64 as _b64
    import re as _re
    from flask import send_file, redirect
    from io import BytesIO
    from ai.publish_store import IMAGES_DIR
    ua = request.headers.get("User-Agent", "")[:120]
    referer = request.headers.get("Referer", "")[:160]
    via = request.headers.get("Via", "")[:120]
    fwd = request.headers.get("X-Forwarded-For", "")[:120]
    safe_id = _re.sub(r'[^a-f0-9]', '', img_id)
    if not safe_id:
        logger.warning(f"[hosted-images] serve REJECT: img_id={img_id!r} sanitized to empty (ua={ua!r})")
        return "Not found", 404
    if safe_id != img_id:
        logger.info(f"[hosted-images] serve sanitized img_id {img_id!r} -> {safe_id!r}")
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
    # If the row was backfilled to S3, redirect there first (Task #99).
    # We always re-mint a presigned URL via attachment_store.s3_serve_url
    # rather than reusing the stored s3_url: the stored URL is the
    # virtual-hosted public form, which 403s on private buckets and
    # would silently break every email image.
    s3_meta = _load_hosted_image_s3_meta(safe_id)
    if s3_meta:
        s3_key, _s3_url_unused, _ext = s3_meta
        if s3_key:
            try:
                from integrations import attachment_store as _astore
                target = _astore.s3_serve_url(s3_key)
                if target:
                    logger.info(f"[hosted-images] serve REDIRECT(s3) id={safe_id} key={s3_key}")
                    return redirect(target, code=302)
            except Exception:
                pass
    # Try Postgres first — durable across deploys.
    db_hit = _load_hosted_image_db(safe_id)
    if db_hit is not None:
        ext, raw = db_hit
        logger.info(
            f"[hosted-images] serve HIT(db) id={safe_id} ext={ext} bytes={len(raw)} "
            f"ua={ua!r} ref={referer!r} via={via!r} xff={fwd!r}"
        )
        return send_file(BytesIO(raw), mimetype=mime_map.get(ext, f"image/{ext}"))
    # Disk fallback (legacy / local dev).
    img_dir = os.path.join(IMAGES_DIR, f"_hosted_{safe_id}")
    img_dir = os.path.realpath(img_dir)
    if not img_dir.startswith(IMAGES_DIR):
        logger.warning(f"[hosted-images] serve REJECT: path traversal id={safe_id} dir={img_dir}")
        return "Not found", 404
    dat_path = os.path.join(img_dir, "image.dat")
    meta_path = os.path.join(img_dir, "meta.json")
    if not os.path.exists(dat_path) or not os.path.exists(meta_path):
        logger.warning(
            f"[hosted-images] serve MISS id={safe_id} (db=miss disk=miss) "
            f"ua={ua!r} ref={referer!r} via={via!r} xff={fwd!r}"
        )
        return "Not found", 404
    with open(dat_path, "r") as f:
        data_url = f.read()
    m = _re.match(r"data:image/(\w+);base64,(.+)", data_url)
    if not m:
        logger.warning(f"[hosted-images] serve INVALID id={safe_id}: disk file not a data URL")
        return "Invalid image", 500
    ext = m.group(1)
    raw = _b64.b64decode(m.group(2))
    logger.info(
        f"[hosted-images] serve HIT(disk) id={safe_id} ext={ext} bytes={len(raw)} "
        f"ua={ua!r} ref={referer!r} via={via!r} xff={fwd!r}"
    )
    return send_file(BytesIO(raw), mimetype=mime_map.get(ext, f"image/{ext}"))


@app.route("/api/publish/image/meta/<feature_id>")
def publish_image_meta(feature_id):
    """Return JSON metadata + dataUrl for the feature image (used by batch view to restore when localStorage missed it)."""
    img = get_publish_image(feature_id)
    if not img or not img.get("dataUrl"):
        return jsonify({"exists": False}), 404
    return jsonify({
        "exists": True,
        "name": img.get("name", "image"),
        "dataUrl": img.get("dataUrl"),
        "isGif": bool(img.get("is_gif", False)),
    })


@app.route("/api/publish/image/serve/<feature_id>")
def serve_feature_image(feature_id):
    import base64 as _b64
    import re as _re
    from flask import send_file, redirect
    from io import BytesIO
    img_data = get_publish_image(feature_id)
    if not img_data:
        return "Not found", 404
    # Prefer S3 when we recorded a key (Task #99). 302 keeps the URL the
    # frontend uses unchanged but offloads bandwidth to the bucket. We
    # mint a fresh presigned URL each time (see s3_serve_url docs) so a
    # private bucket still serves correctly.
    s3_key = img_data.get("s3_key") or ""
    if s3_key:
        try:
            from integrations import attachment_store as _astore
            target = _astore.s3_serve_url(s3_key)
            if target:
                return redirect(target, code=302)
        except Exception:
            pass
    if not img_data.get("dataUrl"):
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


@app.route("/api/intro/regenerate", methods=["POST"])
def regenerate_intro_endpoint():
    """Generate a fresh 1-2 sentence intro paragraph for the combined
    standalone email banner using the list of feature titles/summaries
    the user is currently digesting.
    """
    from ai.claude_client import generate_content

    data = request.get_json() or {}
    banner_title = (data.get("banner_title") or "Product Updates").strip()[:120]
    banner_month = (data.get("banner_month") or "").strip()[:60]
    instructions = (data.get("instructions") or "").strip()[:500]
    items = data.get("items") or []
    if not isinstance(items, list):
        items = []

    cleaned = []
    for it in items[:25]:
        if not isinstance(it, dict):
            continue
        t = (it.get("title") or "").strip()
        if not t:
            continue
        s = (it.get("summary") or "").strip()
        cleaned.append({"title": t[:200], "summary": s[:300]})
    if not cleaned:
        return jsonify({"success": False, "error": "No feature titles to summarize."}), 400

    bullets = "\n".join(
        "- " + c["title"] + ((" — " + c["summary"]) if c["summary"] else "")
        for c in cleaned
    )
    header_bits = []
    if banner_title:
        header_bits.append(f"Banner title: {banner_title}")
    if banner_month:
        header_bits.append(f"Edition: {banner_month}")
    header = "\n".join(header_bits)

    system_prompt = (
        "You write the short opening paragraph for Chartmetric's monthly "
        "product-update email. Tone: warm, plain-spoken, concise, no hype. "
        "Output exactly one paragraph of 1-2 sentences (max ~45 words). "
        "Do not list every feature. Do not use markdown. Do not start with "
        "'In this email' or 'This month we'. No emojis. No salutation. "
        "Return ONLY the paragraph text — no quotes, no preamble."
    )
    user_prompt = (
        f"{header}\n\nFeatures included in this digest:\n{bullets}\n\n"
        + (f"Editor guidance: {instructions}\n\n" if instructions else "")
        + "Write the intro paragraph now."
    )

    result = generate_content(system_prompt, user_prompt, max_tokens=200)
    if not result.get("success"):
        return jsonify({"success": False, "error": result.get("error") or "Generation failed"}), 502

    intro = (result.get("content") or "").strip()
    # Strip any wrapping quotes Claude may have added despite the instruction.
    if len(intro) >= 2 and intro[0] in ('"', "'") and intro[-1] == intro[0]:
        intro = intro[1:-1].strip()
    if not intro:
        return jsonify({"success": False, "error": "Empty intro returned"}), 502
    return jsonify({"success": True, "intro": intro}), 200


@app.route("/api/publish/video", methods=["POST"])
def save_video_endpoint():
    import time as _time
    _t0 = _time.time()
    data = request.get_json() or {}
    feature_id = data.get("feature_id", "")
    data_url = data.get("dataUrl", "")
    is_url = bool(data.get("isUrl"))
    ext_url = (data.get("url") or "").strip()
    ext_thumb = (data.get("thumb_url") or "").strip()
    filename = data.get("name", "video.mp4")
    logger.info(f"[publish/video] REQ feature_id={feature_id!r} filename={filename!r} isUrl={is_url} dataUrl_len={len(data_url)} url_len={len(ext_url)}")
    if not feature_id:
        logger.warning(f"[publish/video] REJECT missing feature_id")
        return jsonify({"success": False, "error": "feature_id required"}), 400
    if is_url or (ext_url and not data_url):
        if not ext_url:
            return jsonify({"success": False, "error": "url required for URL-only video"}), 400
        try:
            video_id = save_publish_video_url(feature_id, ext_url, filename, thumb_url=ext_thumb)
        except ValueError as e:
            logger.warning(f"[publish/video] REJECT ValueError(url): {e}")
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:
            logger.exception(f"[publish/video] UNCAUGHT(url) type={type(e).__name__} repr={e!r}")
            return jsonify({"success": False, "error": f"Video URL save failed: {type(e).__name__}: {e}"}), 500
        thumb_url = ext_thumb or f"/api/videos/{video_id}/thumb"
        video_url = ext_url
        logger.info(f"[publish/video] OK url video_id={video_id} dt={(_time.time()-_t0)*1000:.0f}ms")
        return jsonify({"success": True, "video_id": video_id, "thumb_url": thumb_url, "video_url": video_url, "is_url": True}), 200
    if not data_url:
        logger.warning(f"[publish/video] REJECT missing dataUrl")
        return jsonify({"success": False, "error": "dataUrl required"}), 400
    try:
        video_id = save_publish_video(feature_id, data_url, filename)
    except ValueError as e:
        logger.warning(f"[publish/video] REJECT ValueError: {e}")
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.exception(f"[publish/video] UNCAUGHT type={type(e).__name__} repr={e!r}")
        return jsonify({"success": False, "error": f"Video upload failed: {type(e).__name__}: {e}"}), 500
    thumb_url = f"/api/videos/{video_id}/thumb"
    video_url = f"/api/videos/{video_id}"
    logger.info(f"[publish/video] OK video_id={video_id} dt={(_time.time()-_t0)*1000:.0f}ms")
    return jsonify({"success": True, "video_id": video_id, "thumb_url": thumb_url, "video_url": video_url}), 200


_VIDEO_PLACEHOLDER_PNG_BYTES = None
_VIDEO_PLACEHOLDER_PATH = os.path.join(_app_dir, "static", "video_placeholder.png")


def _video_placeholder_png_bytes() -> bytes:
    """Return cached PNG bytes for our local video-thumbnail placeholder.

    Used when a real thumbnail isn't available (ffmpeg failed, external
    thumb cache miss, unrecognized URL host). We ship a pre-generated
    640x360 PNG at static/video_placeholder.png so this works in
    production deployments where Pillow may not be installed. Generates
    on the fly via PIL only as a fallback if the static asset is missing.
    """
    global _VIDEO_PLACEHOLDER_PNG_BYTES
    if _VIDEO_PLACEHOLDER_PNG_BYTES is not None:
        return _VIDEO_PLACEHOLDER_PNG_BYTES
    try:
        with open(_VIDEO_PLACEHOLDER_PATH, "rb") as fh:
            _VIDEO_PLACEHOLDER_PNG_BYTES = fh.read()
            return _VIDEO_PLACEHOLDER_PNG_BYTES
    except Exception:
        pass
    try:
        from PIL import Image, ImageDraw
        from io import BytesIO
        img = Image.new("RGB", (640, 360), (34, 34, 34))
        draw = ImageDraw.Draw(img)
        cx, cy = 320, 180
        size = 56
        draw.polygon(
            [(cx - size + 12, cy - size), (cx - size + 12, cy + size), (cx + size, cy)],
            fill=(255, 255, 255),
        )
        try:
            draw.text((cx - 22, cy + size + 12), "Video", fill=(180, 180, 180))
        except Exception:
            pass
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        _VIDEO_PLACEHOLDER_PNG_BYTES = buf.getvalue()
    except Exception:
        # Smallest possible valid 1x1 grey PNG as a last resort.
        _VIDEO_PLACEHOLDER_PNG_BYTES = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108020000"
            "00907753de0000000c4944415478da6360606000000000050001a5f6"
            "45400000000049454e44ae426082"
        )
    return _VIDEO_PLACEHOLDER_PNG_BYTES


@app.route("/api/placeholder/video-thumb")
def video_thumb_placeholder():
    """Serve our local fallback video-thumbnail PNG.

    We used to redirect to via.placeholder.com here, but that host's TLS
    chain went stale and every fallback request now fails with an SSL
    handshake error, surfacing as a broken-image icon in the in-app
    preview and recipient inboxes. Serving our own bytes keeps the
    placeholder working as long as our app is up.
    """
    return (
        _video_placeholder_png_bytes(),
        200,
        {
            "Content-Type": "image/png",
            "Cache-Control": "public, max-age=86400",
        },
    )


def _placeholder_thumb_url(absolute: bool = False) -> str:
    """Return the URL for our local video-thumb placeholder.

    Pass ``absolute=True`` when the URL will be embedded in something
    delivered off-host (rendered email HTML, external thumb redirects);
    in-app previews can use the relative form.
    """
    if absolute:
        try:
            return f"{_get_base_url().rstrip('/')}/api/placeholder/video-thumb"
        except Exception:
            return "/api/placeholder/video-thumb"
    return "/api/placeholder/video-thumb"


@app.route("/api/videos/<video_id>")
def serve_video(video_id):
    from flask import send_file, redirect
    # S3 redirect when we recorded a key (Task #99).
    try:
        from ai.publish_store import get_video_meta
        meta_only = get_video_meta(video_id)
    except Exception:
        meta_only = None
    if meta_only:
        s3_key = meta_only.get("s3_key") or ""
        if s3_key:
            try:
                from integrations import attachment_store as _astore
                target = _astore.s3_serve_url(s3_key)
                if target:
                    return redirect(target, code=302)
            except Exception:
                pass
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
    # S3 redirect for the thumbnail when we have one (Task #99).
    try:
        from ai.publish_store import get_video_meta
        meta_only = get_video_meta(video_id)
    except Exception:
        meta_only = None
    if meta_only:
        s3_key = meta_only.get("s3_thumb_key") or ""
        if s3_key:
            try:
                from integrations import attachment_store as _astore
                target = _astore.s3_serve_url(s3_key)
                if target:
                    return redirect(target, code=302)
            except Exception:
                pass
    try:
        thumb_path = get_video_thumb_path(video_id)
    except ValueError:
        return "Not found", 404
    if not thumb_path:
        return (
            _video_placeholder_png_bytes(),
            200,
            {"Content-Type": "image/png", "Cache-Control": "public, max-age=300"},
        )
    return send_file(thumb_path, mimetype="image/jpeg")


@app.route("/api/videos/external-thumb/<key>")
def serve_external_video_thumb(key):
    from flask import send_file
    from integrations.video_thumb import get_cached_external_thumb_path, get_external_thumb_s3_key
    from flask import redirect
    # Prefer S3 when enabled (Task #99) — independent of local cache
    # state. Re-mint a presigned URL each request via s3_serve_url so
    # private buckets work (the prior s3_public_url path returned 403).
    s3_key = get_external_thumb_s3_key(key)
    if s3_key:
        try:
            from integrations import attachment_store as _astore
            target = _astore.s3_serve_url(s3_key)
            if target:
                return redirect(target, code=302)
        except Exception:
            pass
    path = get_cached_external_thumb_path(key)
    if not path:
        return (
            _video_placeholder_png_bytes(),
            200,
            {"Content-Type": "image/png", "Cache-Control": "public, max-age=300"},
        )
    return send_file(path, mimetype="image/jpeg")


@app.route("/api/features/<feature_id>/videos/<video_id>", methods=["DELETE"])
def delete_feature_video(feature_id, video_id):
    if not feature_id or not video_id:
        return jsonify({"success": False, "error": "feature_id and video_id required"}), 400
    try:
        delete_publish_video(feature_id, video_id)
    except ValueError as e:
        msg = str(e)
        logger.warning(f"[features/videos DELETE] REJECT feature_id={feature_id!r} video_id={video_id!r}: {msg}")
        status = 404 if "not found" in msg.lower() or "does not belong" in msg.lower() else 400
        return jsonify({"success": False, "error": msg}), status
    except Exception as e:
        logger.exception(f"[features/videos DELETE] UNCAUGHT feature_id={feature_id!r} video_id={video_id!r} type={type(e).__name__}")
        return jsonify({"success": False, "error": f"Delete failed: {type(e).__name__}: {e}"}), 500
    return jsonify({"success": True}), 200


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
            if v.get("is_url"):
                ext_thumb = v.get("external_thumb_url") or ""
                result.append({
                    "video_id": vid_id,
                    "filename": fname,
                    "thumb_url": ext_thumb or "/api/placeholder/video-thumb",
                    "video_url": v.get("external_url") or "",
                    "is_url": True,
                })
            else:
                result.append({
                    "video_id": vid_id,
                    "filename": fname,
                    "thumb_url": f"{scheme}://{base}/api/videos/{vid_id}/thumb" if has_thumb else "/api/placeholder/video-thumb",
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
        # Surface the AI-crafted headline as the card title so the per-card
        # Regenerate button updates the visible title alongside the body —
        # matches the behavior of the batch endpoint above.
        from ai.generator import extract_benefit_title
        raw_title = feature.get("title", "")
        extracted = extract_benefit_title(result.get("content", ""), channel, raw_title)
        result["feature_title"] = extracted or raw_title
        result["raw_feature_title"] = raw_title
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
    skip_cache = bool(data.get("skip_cache", False))

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
        cache_note = " [skip_cache=True]" if skip_cache else ""
        print(f"[generate/batch-single] Generating {channel} for {len(features)} features (mode: {mode or 'default'}){cache_note}", flush=True)

        from ai.generator import extract_benefit_title

        def gen_one(feature):
            result = generate_for_channel(feature, channel, custom_instructions=custom_instructions or None, mode=mode, skip_cache=skip_cache)
            result["feature_id"] = feature.get("id", "")
            raw_title = feature.get("title", "")
            # Surface a benefit-driven, marketer-ready headline (lifted from the
            # generated content) as the visible card title. Falls back to the
            # raw ticket title when no clean headline can be extracted.
            extracted = extract_benefit_title(result.get("content", ""), channel, raw_title)
            result["feature_title"] = extracted or raw_title
            result["raw_feature_title"] = raw_title
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


# Allowlist of upstream hosts our /api/thumb endpoint will fetch from.
# Restricting to known thumbnail/CDN hosts prevents the route from being
# turned into an open proxy that can be abused to mask the server's IP
# against arbitrary third-party sites.
_THUMB_PROXY_ALLOWED_HOSTS = (
    "drive.google.com",
    "lh3.googleusercontent.com",
    "lh4.googleusercontent.com",
    "lh5.googleusercontent.com",
    "lh6.googleusercontent.com",
    "cdn.loom.com",
    "img.youtube.com",
    "i.ytimg.com",
    "vumbnail.com",
)
_THUMB_PROXY_MAX_BYTES = 2 * 1024 * 1024  # 2 MB hard cap per upstream fetch
_THUMB_PROXY_TTL_SECONDS = 60 * 60  # 1 hour
_THUMB_PROXY_CACHE_MAX_ENTRIES = 128  # ~256MB worst case, ~10MB realistic
_THUMB_PROXY_MAX_REDIRECTS = 5
_thumb_proxy_cache: dict = {}
_thumb_proxy_cache_lock = threading.Lock()


def _thumb_proxy_cache_get(key: str):
    now = time.time()
    with _thumb_proxy_cache_lock:
        entry = _thumb_proxy_cache.get(key)
        if not entry:
            return None
        bytes_, mime, expires_at = entry
        if expires_at < now:
            _thumb_proxy_cache.pop(key, None)
            return None
        return bytes_, mime


def _thumb_proxy_cache_put(key: str, bytes_: bytes, mime: str) -> None:
    expires_at = time.time() + _THUMB_PROXY_TTL_SECONDS
    with _thumb_proxy_cache_lock:
        # Drop oldest entries when at capacity to keep memory bounded.
        # Strict LRU isn't required for a thumbnail cache, FIFO is fine.
        if len(_thumb_proxy_cache) >= _THUMB_PROXY_CACHE_MAX_ENTRIES:
            try:
                oldest_key = next(iter(_thumb_proxy_cache))
                _thumb_proxy_cache.pop(oldest_key, None)
            except StopIteration:
                pass
        _thumb_proxy_cache[key] = (bytes_, mime, expires_at)


@app.route("/api/thumb")
def api_thumb():
    """Server-side image proxy for video/image thumbnails.

    Browsers can't reliably fetch Google Drive thumbnail URLs directly:
    Drive often serves an HTML auth page (or 302 to one) when the
    request lacks a Google session cookie, so the in-app preview
    silently shows a broken image. Our server has no such restriction
    and gets the actual bytes for any publicly shared file. This proxy
    fetches a thumbnail from an allowlisted host and streams the raw
    bytes back to the browser with cache-friendly headers, so the
    preview reliably renders.

    Category: Media

    Query params:
        url: Absolute http(s) URL of the thumbnail. Must point at an
             allowlisted host (Drive, Loom CDN, YouTube/Vimeo
             thumbnail endpoints).

    Response:
        200: image bytes with the upstream Content-Type
        302: redirect back to the original URL when fetch fails (so the
             browser can still try direct, falling back to its own
             broken-image handling).
        400: missing/invalid url, host not allowlisted.
    """
    from urllib.parse import urlparse
    from flask import Response, redirect
    import requests as req_lib

    raw = (request.args.get("url") or "").strip()
    if not raw:
        return jsonify({"error": "url is required"}), 400
    try:
        parsed = urlparse(raw)
    except Exception:
        return jsonify({"error": "invalid url"}), 400
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return jsonify({"error": "invalid url"}), 400
    host = parsed.hostname.lower()
    if host not in _THUMB_PROXY_ALLOWED_HOSTS:
        return jsonify({"error": "host not allowed"}), 400

    cached = _thumb_proxy_cache_get(raw)
    if cached:
        cached_bytes, cached_mime = cached
        resp = Response(cached_bytes, mimetype=cached_mime)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        resp.headers["X-Thumb-Cache"] = "hit"
        return resp

    # Manually follow redirects so we can re-validate each hop against
    # the host allowlist. requests.get(allow_redirects=True) would
    # initially aim at an allowlisted host but then silently follow a
    # 30x to an arbitrary internal/external target before our MIME
    # check ran, which is an SSRF risk. Drive's /thumbnail endpoint
    # legitimately redirects to lh3.googleusercontent.com, which is
    # also allowlisted, so a single hop is normal here.
    current_url = raw
    upstream = None
    try:
        for _hop in range(_THUMB_PROXY_MAX_REDIRECTS + 1):
            try:
                hop_parsed = urlparse(current_url)
            except Exception:
                logger.info("thumb proxy invalid redirect url: %s", current_url)
                return redirect(raw, code=302)
            if hop_parsed.scheme not in ("http", "https") or not hop_parsed.hostname:
                logger.info("thumb proxy invalid redirect target: %s", current_url)
                return redirect(raw, code=302)
            hop_host = hop_parsed.hostname.lower()
            if hop_host not in _THUMB_PROXY_ALLOWED_HOSTS:
                logger.info(
                    "thumb proxy refused redirect to non-allowed host %s",
                    hop_host,
                )
                return redirect(raw, code=302)
            try:
                upstream = req_lib.get(
                    current_url,
                    timeout=8,
                    allow_redirects=False,
                    stream=True,
                    headers={
                        # A real browser User-Agent helps a few CDNs return the
                        # actual image instead of a stripped-down response.
                        "User-Agent": "Mozilla/5.0 (compatible; AmplifyThumbProxy/1.0)",
                        "Accept": "image/*,*/*;q=0.8",
                    },
                )
            except Exception as exc:
                logger.warning("thumb proxy fetch failed for %s: %s", current_url, exc)
                return redirect(raw, code=302)
            if upstream.status_code in (301, 302, 303, 307, 308):
                next_url = upstream.headers.get("Location") or ""
                try:
                    upstream.close()
                except Exception:
                    pass
                upstream = None
                if not next_url:
                    return redirect(raw, code=302)
                # Resolve relative redirects against the previous hop.
                from urllib.parse import urljoin
                current_url = urljoin(current_url, next_url)
                continue
            break
        else:
            # Hit the redirect cap without a terminal response.
            return redirect(raw, code=302)

        if upstream is None or upstream.status_code != 200:
            status = upstream.status_code if upstream is not None else "no-response"
            logger.info("thumb proxy upstream returned %s for %s", status, current_url)
            return redirect(raw, code=302)
        mime = (upstream.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # Drive sometimes serves an HTML error/auth page with status 200.
        # Treat anything that isn't an image as a failed fetch and let the
        # browser fall back to the direct URL (which will likely also
        # show a broken image, but that's the truthful state).
        if not mime.startswith("image/"):
            logger.info("thumb proxy upstream returned non-image %s for %s", mime, raw)
            return redirect(raw, code=302)
        chunks = []
        total = 0
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > _THUMB_PROXY_MAX_BYTES:
                logger.info("thumb proxy aborted oversize fetch for %s", raw)
                return redirect(raw, code=302)
            chunks.append(chunk)
        body = b"".join(chunks)
    finally:
        try:
            upstream.close()
        except Exception:
            pass

    _thumb_proxy_cache_put(raw, body, mime)
    resp = Response(body, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    resp.headers["X-Thumb-Cache"] = "miss"
    return resp


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

    # Kick off the background attachment-backfill sweep (Task #104). It
    # only starts when S3 is enabled and ``AMPLIFY_BACKFILL_AUTO`` isn't
    # set to 0 — see ``_start_background_attachment_backfill`` for the
    # gate and the env-var knobs.
    try:
        _start_background_attachment_backfill()
    except Exception:
        logger.exception("[backfill-sweep] failed to start")

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
