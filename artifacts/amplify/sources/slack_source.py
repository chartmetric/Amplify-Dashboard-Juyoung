import re
import sys
import time
import logging
from datetime import datetime, timedelta, timezone
from slack_sdk import WebClient
import config
from sources.base import SourceAdapter, FeatureContext

logger = logging.getLogger("amplify.slack")


def _clean_slack_text(text: str) -> str:
    return (
        text
        .replace("\u003C", "<")
        .replace("\u003E", ">")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def _extract_reactions(msg: dict) -> tuple[int, dict]:
    reactions = msg.get("reactions") or []
    breakdown = {}
    total = 0
    for r in reactions:
        name = r.get("name", "")
        count = r.get("count", 0)
        if name:
            breakdown[name] = count
            total += count
    return total, breakdown


def _extract_links(text: str) -> list[str]:
    return re.findall(r"<(https?://[^|>]+)", text)


def _extract_asana_task_ids(text: str) -> list[str]:
    task_pattern = re.findall(r"app\.asana\.com/[^>\s]*/task/(\d+)", text)
    standard_pattern = re.findall(r"app\.asana\.com/\d+/\d+/(\d+)(?!\w)", text)
    seen = set()
    result = []
    for tid in task_pattern + standard_pattern:
        if tid not in seen:
            seen.add(tid)
            result.append(tid)
    return result


def _extract_asana_urls(text: str) -> list[str]:
    return re.findall(r"(https?://app\.asana\.com/[^\s|>]+)", text)


def _extract_github_urls(text: str) -> list[str]:
    return re.findall(r"(https?://github\.com/[^\s|>]+)", text)


def _parse_release_version(header_line: str) -> dict:
    resolved = _resolve_slack_links(header_line)
    resolved = _strip_slack_links(resolved)

    fe_version = None
    be_version = None

    if re.search(r"FE\s*(only)?\s*[-–]?\s*(v[\w\d.-]+)", resolved, re.IGNORECASE):
        m = re.search(r"FE\s*(?:only)?\s*[-–]?\s*(v[\w\d.-]+)", resolved, re.IGNORECASE)
        fe_version = m.group(1)
    elif "fe only" in resolved.lower() or "fe" in resolved.lower():
        v = re.search(r"(v\d{8}[-\w]*)", resolved)
        if v:
            fe_version = v.group(1)

    be_m = re.search(r"BE\s*[-–]?\s*(v[\w\d.-]+)", resolved, re.IGNORECASE)
    if be_m:
        be_version = be_m.group(1)

    if not fe_version and not be_version:
        combined = re.search(r"\(FE\s+(v[\w\d.-]+)\s*\|\s*BE\s+(v[\w\d.-]+)\)", resolved, re.IGNORECASE)
        if combined:
            fe_version = combined.group(1)
            be_version = combined.group(2)

    return {"fe": fe_version, "be": be_version}


def _is_release_message(text: str) -> bool:
    lower = text.lower()
    if "production release" in lower:
        return True
    if "release" in lower and ("fe" in lower or "be" in lower) and re.search(r"v\d{8}", lower):
        return True
    return False


def _is_bot_or_noise(msg: dict) -> bool:
    if msg.get("subtype") in ("bot_message", "channel_join", "channel_leave", "channel_topic", "channel_purpose"):
        return True
    if msg.get("bot_id"):
        return True
    return False


def _resolve_slack_links(text: str) -> str:
    return re.sub(r"<https?://[^|>]+\|([^>]+)>", r"\1", text)


def _strip_slack_links(text: str) -> str:
    return re.sub(r"<https?://[^>]+>", "", text).strip()


_LEADING_TRAILING_PUNCT = r"[\s\.,;:!\?\-_'\"`~/\\\(\)\[\]\{\}<>•·…→←↑↓\u2022]+"


def is_low_quality_title(title: str) -> bool:
    """Return True if the title looks like garbage (single fragment, mostly punctuation, etc.).

    Catches things like ',etc.', 'tbd', 'n/a', '→', single-word stubs.
    """
    if not title:
        return True
    cleaned = title.strip()
    if not cleaned:
        return True
    # Leading-character rule: evaluate on the cleaned (whitespace-trimmed)
    # title — NOT a punctuation-stripped surrogate — so that titles like
    # ',etc.' or '→ TBD' are correctly rejected.
    if not re.match(r"^[A-Za-z0-9]", cleaned):
        return True
    # Length and token checks may still operate on the punct-trimmed body so
    # legitimate titles wrapped in trailing punctuation aren't penalized.
    body = re.sub(r"^" + _LEADING_TRAILING_PUNCT, "", cleaned)
    body = re.sub(_LEADING_TRAILING_PUNCT + r"$", "", body)
    if len(body) < 10:
        return True
    alpha_tokens = [t for t in re.findall(r"[A-Za-z]+", body) if len(t) >= 2]
    if len(alpha_tokens) < 2:
        return True
    junk_singletons = {"tbd", "na", "etc", "wip", "todo", "fixme"}
    if len(alpha_tokens) == 2 and all(t.lower() in junk_singletons for t in alpha_tokens):
        return True
    return False


def _parse_feature_bullet(line: str, require_bullet: bool = True) -> dict | None:
    line = line.strip()
    if not line:
        return None

    bullet_match = re.match(r"^[\u2022\u2023\u25E6\u2043\u2219•\-\*]\s+", line)
    if bullet_match:
        line = line[bullet_match.end():]
    elif require_bullet:
        fe_be_match = re.match(r"^(?:FE|BE):\s+", line, re.IGNORECASE)
        if not fe_be_match:
            return None
        line = line[fe_be_match.end():]

    line = _resolve_slack_links(line)
    line = _strip_slack_links(line)
    line = re.sub(r"\*", "", line).strip()

    if not line or len(line) < 5:
        return None

    lower = line.lower()
    if any(lower.startswith(skip) for skip in ["cc ", "release", "production release", "chartmetric production"]):
        return None

    source_prefix = None
    title = line

    prefix_patterns = [
        (r"^PE:\s*", "PE"),
        (r"^Devin:\s*", "Devin"),
        (r"^FE:\s*", "FE"),
        (r"^BE:\s*", "BE"),
    ]
    for pat, pname in prefix_patterns:
        m = re.match(pat, line, re.IGNORECASE)
        if m:
            source_prefix = pname
            title = line[m.end():]
            break
    else:
        pr_match = re.match(r"^#(\d+)\s+(?:feat|fix|chore|refactor|perf|style|docs|test|build|ci):\s*", line, re.IGNORECASE)
        if pr_match:
            source_prefix = f"#{pr_match.group(1)}"
            title = line[pr_match.end():]

    title = title.strip()
    if not title or len(title) < 5:
        return None

    return {
        "title": title,
        "source_prefix": source_prefix,
    }


def _ts_to_date(ts_str: str) -> str:
    try:
        ts_float = float(ts_str)
        dt = datetime.fromtimestamp(ts_float, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        return ""


class SlackSource(SourceAdapter):
    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self._client = None

    def _get_client(self):
        if self._client is None:
            token = config.SLACK_BOT_TOKEN
            if not token:
                raise RuntimeError("SLACK_BOT_TOKEN not set")
            self._client = WebClient(token=token)
        return self._client

    def extract_features_from_channel(self, days: int = 30) -> dict:
        client = self._get_client()
        oldest = str((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

        all_messages = []
        cursor = None
        while True:
            kwargs = {
                "channel": self.channel_id,
                "oldest": oldest,
                "limit": 200,
            }
            if cursor:
                kwargs["cursor"] = cursor
            result = client.conversations_history(**kwargs)
            all_messages.extend(result.get("messages", []))
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        features = []
        skipped_messages = []
        parse_errors = []

        for msg in all_messages:
            if _is_bot_or_noise(msg):
                skipped_messages.append({
                    "ts": msg.get("ts", ""),
                    "reason": "bot_or_noise",
                    "preview": _clean_slack_text(msg.get("text", ""))[:100],
                })
                continue

            raw_text = msg.get("text", "")
            text = _clean_slack_text(raw_text)
            msg_ts = msg.get("ts", "")
            release_date = _ts_to_date(msg_ts)
            total_reactions, reactions_breakdown = _extract_reactions(msg)

            all_urls_in_msg = _extract_links(raw_text)
            asana_urls = _extract_asana_urls(text)
            github_urls = _extract_github_urls(text)
            asana_task_ids = _extract_asana_task_ids(raw_text)

            thread_asana_urls = []
            thread_github_urls = []
            thread_asana_task_ids = []
            if msg.get("reply_count", 0) > 0:
                try:
                    thread = client.conversations_replies(
                        channel=self.channel_id,
                        ts=msg_ts,
                    )
                    for reply in thread.get("messages", [])[1:]:
                        reply_raw = reply.get("text", "")
                        reply_text = _clean_slack_text(reply_raw)
                        thread_asana_urls.extend(_extract_asana_urls(reply_text))
                        thread_github_urls.extend(_extract_github_urls(reply_text))
                        thread_asana_task_ids.extend(_extract_asana_task_ids(reply_raw))
                except Exception as e:
                    logger.warning(f"Failed to fetch thread replies for {msg_ts}: {e}")

            all_asana_urls = list(dict.fromkeys(asana_urls + thread_asana_urls))
            all_github_urls = list(dict.fromkeys(github_urls + thread_github_urls))
            all_asana_ids = list(dict.fromkeys(asana_task_ids + thread_asana_task_ids))

            if not _is_release_message(text):
                lines = text.split("\n")
                has_bullets = any(re.match(r"^\s*[\u2022\u2023\u25E6\u2043\u2219•\-\*]\s+\S", l) for l in lines)
                if not has_bullets:
                    skipped_messages.append({
                        "ts": msg_ts,
                        "reason": "not_release_message",
                        "preview": text[:100],
                    })
                    continue

            release_version = {"fe": None, "be": None}
            lines = text.split("\n")
            for line in lines:
                if "release" in line.lower() or re.search(r"v\d{8}", line):
                    release_version = _parse_release_version(line)
                    if release_version["fe"] or release_version["be"]:
                        break

            try:
                permalink = client.chat_getPermalink(
                    channel=self.channel_id,
                    message_ts=msg_ts,
                ).get("permalink", "")
            except Exception:
                permalink = ""

            bullet_idx = 0
            found_bullets = False
            for line in lines:
                parsed = _parse_feature_bullet(line)
                if parsed:
                    if is_low_quality_title(parsed["title"]):
                        parse_errors.append({
                            "ts": msg_ts,
                            "reason": "low_quality_title",
                            "preview": parsed["title"][:120],
                        })
                        logger.info(f"[slack-first] Skipping low-quality bullet title: {parsed['title'][:80]!r}")
                        continue
                    found_bullets = True
                    feature_id = f"slack-{msg_ts}-{bullet_idx}"

                    matched_asana_id = None
                    matched_asana_url = None
                    if bullet_idx < len(all_asana_ids):
                        matched_asana_id = all_asana_ids[bullet_idx]
                    elif len(all_asana_ids) == 1:
                        matched_asana_id = all_asana_ids[0]

                    if matched_asana_id:
                        for url in all_asana_urls:
                            if matched_asana_id in url:
                                matched_asana_url = url
                                break

                    matched_github_url = None
                    if bullet_idx < len(all_github_urls):
                        matched_github_url = all_github_urls[bullet_idx]
                    elif len(all_github_urls) == 1:
                        matched_github_url = all_github_urls[0]

                    features.append({
                        "id": feature_id,
                        "title": parsed["title"],
                        "description": "",
                        "source": "slack_only",
                        "source_prefix": parsed["source_prefix"],
                        "release_date": release_date,
                        "release_version": release_version,
                        "released": True,
                        "slack_url": permalink,
                        "asana_url": matched_asana_url,
                        "asana_task_id": matched_asana_id,
                        "github_url": matched_github_url,
                        "asana_linked": False,
                        "total_reactions": total_reactions,
                        "reactions_breakdown": reactions_breakdown,
                        "engineer": None,
                        "assignee": None,
                        "urgency_score": None,
                        "team": None,
                        "task_type": None,
                    })
                    bullet_idx += 1

            if not found_bullets and _is_release_message(text):
                parse_errors.append({
                    "ts": msg_ts,
                    "reason": "release_message_but_no_bullets_parsed",
                    "preview": text[:200],
                })

        logger.info(f"[slack-first] Extracted {len(features)} features from {len(all_messages)} messages ({len(skipped_messages)} skipped, {len(parse_errors)} parse errors)")

        return {
            "features": features,
            "stats": {
                "total_messages": len(all_messages),
                "total_features": len(features),
                "skipped_messages": len(skipped_messages),
                "parse_errors": len(parse_errors),
            },
            "skipped": skipped_messages,
            "parse_errors": parse_errors,
        }

    def get_released_task_ids(self) -> dict:
        client = self._get_client()
        result = client.conversations_history(
            channel=self.channel_id,
            limit=100,
        )

        released = {}
        for msg in result.get("messages", []):
            raw_text = msg.get("text", "")
            task_ids = _extract_asana_task_ids(raw_text)
            if not task_ids:
                continue

            total_reactions, reactions = _extract_reactions(msg)
            ts = msg.get("ts", "")

            for task_id in task_ids:
                if task_id not in released:
                    released[task_id] = {
                        "released": True,
                        "release_date": ts,
                        "total_reactions": total_reactions,
                        "reactions_breakdown": reactions,
                    }

        return released

    def list_recent_features(self) -> list[dict]:
        result = self.extract_features_from_channel(days=30)
        return result["features"]

    def get_feature_context(self, feature_id: str, **kwargs) -> FeatureContext:
        client = self._get_client()

        parts = feature_id.split("-")
        if len(parts) >= 2:
            msg_ts = parts[1] if len(parts) == 2 else f"{parts[1]}"
            for i in range(2, len(parts)):
                if parts[i].replace(".", "").isdigit() and "." in f"{parts[1]}.{parts[2]}":
                    msg_ts = f"{parts[1]}.{parts[2]}"
                    break

        if feature_id.startswith("slack-"):
            ts_parts = feature_id[len("slack-"):].rsplit("-", 1)
            msg_ts = ts_parts[0] if ts_parts else feature_id

        result = client.conversations_history(
            channel=self.channel_id,
            oldest=msg_ts,
            latest=msg_ts,
            inclusive=True,
            limit=1,
        )
        messages = result.get("messages", [])
        if not messages:
            raise ValueError(f"Message {feature_id} not found")

        msg = messages[0]
        raw_text = msg.get("text", "")
        text = _clean_slack_text(raw_text)
        links = _extract_links(raw_text)
        total_reactions, reactions = _extract_reactions(msg)

        replies_text = ""
        if msg.get("reply_count", 0) > 0:
            thread = client.conversations_replies(
                channel=self.channel_id,
                ts=msg_ts,
            )
            reply_texts = []
            for reply in thread.get("messages", [])[1:]:
                reply_texts.append(_clean_slack_text(reply.get("text", "")))
            replies_text = "\n---\n".join(reply_texts)

        return FeatureContext(
            title=text[:300],
            description=text,
            raw_details=replies_text,
            source_type="slack",
            metadata={
                "ts": msg.get("ts", ""),
                "user": msg.get("user", ""),
                "reply_count": msg.get("reply_count", 0),
                "total_reactions": total_reactions,
                "reactions": reactions,
                "links": links,
            },
        )


if __name__ == "__main__":
    cases = [
        (",etc.", True),
        ("tbd", True),
        ("n/a", True),
        ("→", True),
        (".", True),
        ("wip todo", True),
        ("Add Spotify Followers chart to Track page", False),
        ("Fix typo in tooltip", False),
        ("Polish TikTok Videos Trend area chart", False),
    ]
    failed = 0
    for title, expected in cases:
        actual = is_low_quality_title(title)
        ok = actual == expected
        marker = "OK" if ok else "FAIL"
        print(f"  [{marker}] is_low_quality_title({title!r}) = {actual} (expected {expected})")
        if not ok:
            failed += 1
    print(f"\n{len(cases) - failed}/{len(cases)} passed")
    sys.exit(1 if failed else 0)
