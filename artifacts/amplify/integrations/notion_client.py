"""Notion publishing client for Amplify.

Powered by the Replit Notion connector (`connection:conn_notion_...`).

Two destinations are supported:

- ``email_newsletter`` -> appends blocks to the most recent monthly child page
  under NEWSLETTER_PARENT_PAGE_ID (the user maintains one child page per
  month under that parent and wants drafts to land in the current month).
- ``notion_monthly`` -> appends blocks directly to ALL_HANDS_PAGE_ID.

Each publish appends a small "header" block (feature title + timestamp)
followed by the draft body parsed into Notion blocks (markdown-ish:
headings via ``## ``, bullets via ``- `` / ``* ``, bold ``**...**``,
inline ``[text](url)`` links, plain paragraphs).
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("amplify.notion")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# User-supplied destinations (see chat: 2026-04-21).
NEWSLETTER_PARENT_PAGE_ID = "ef0963eb171b43c6847a5b3abd74363a"
ALL_HANDS_PAGE_ID = "cbb036eeefe34d53b6b71271da19a0a1"

CHANNEL_TO_DESTINATION = {
    "email_newsletter": "newsletter",  # current month child of NEWSLETTER_PARENT
    "notion_monthly": "all_hands",     # appended directly to ALL_HANDS page
}

_token_cache: dict = {"token": None, "expires_at": 0}


# ---------------------------------------------------------------------------
# Auth: fetch a Notion OAuth access token from the Replit Connectors proxy.
# ---------------------------------------------------------------------------

def _get_access_token() -> str:
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] - 30 > now:
        return _token_cache["token"]

    hostname = os.environ.get("REPLIT_CONNECTORS_HOSTNAME")
    if not hostname:
        raise RuntimeError("REPLIT_CONNECTORS_HOSTNAME is not set; the Notion connector is unavailable.")

    repl_identity = os.environ.get("REPL_IDENTITY")
    web_renewal = os.environ.get("WEB_REPL_RENEWAL")
    if repl_identity:
        x_replit_token = f"repl {repl_identity}"
    elif web_renewal:
        x_replit_token = f"depl {web_renewal}"
    else:
        raise RuntimeError("Neither REPL_IDENTITY nor WEB_REPL_RENEWAL is available; cannot authenticate with Replit connectors.")

    url = f"https://{hostname}/api/v2/connection?include_secrets=true&connector_names=notion"
    r = requests.get(
        url,
        headers={"Accept": "application/json", "X_REPLIT_TOKEN": x_replit_token},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("items") or []
    if not items:
        raise RuntimeError("No Notion connection found. Reconnect Notion in the Replit integrations panel.")

    settings = items[0].get("settings", {}) or {}
    token = (
        settings.get("access_token")
        or (settings.get("oauth") or {}).get("credentials", {}).get("access_token")
    )
    expires_at_raw = (
        settings.get("expires_at")
        or (settings.get("oauth") or {}).get("credentials", {}).get("expires_at")
    )
    if not token:
        raise RuntimeError("Notion connection returned no access token.")

    expires_at = now + 50 * 60  # default 50 min
    if expires_at_raw:
        try:
            if isinstance(expires_at_raw, (int, float)):
                expires_at = float(expires_at_raw)
            else:
                expires_at = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00")).timestamp()
        except Exception:
            pass

    _token_cache["token"] = token
    _token_cache["expires_at"] = expires_at
    return token


def _notion(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    token = _get_access_token()
    url = f"{NOTION_API}{path}"
    r = requests.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=json_body,
        timeout=20,
    )
    if not r.ok:
        # Surface Notion's error body for easier debugging.
        try:
            err_body = r.json()
        except Exception:
            err_body = {"text": r.text}
        raise RuntimeError(f"Notion {method} {path} failed [{r.status_code}]: {err_body}")
    return r.json() if r.text else {}


# ---------------------------------------------------------------------------
# Markdown-ish -> Notion block conversion.
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")


def _rich_text(text: str) -> list[dict]:
    """Convert a string with markdown bold and links to Notion rich text spans."""
    if not text:
        return []

    spans: list[dict] = []

    def flush_plain(seg: str) -> None:
        if not seg:
            return
        # Apply bold within the plain segment.
        idx = 0
        for m in _BOLD_RE.finditer(seg):
            if m.start() > idx:
                spans.append({"type": "text", "text": {"content": seg[idx:m.start()]}})
            spans.append({
                "type": "text",
                "text": {"content": m.group(1)},
                "annotations": {"bold": True},
            })
            idx = m.end()
        if idx < len(seg):
            spans.append({"type": "text", "text": {"content": seg[idx:]}})

    cursor = 0
    for m in _LINK_RE.finditer(text):
        if m.start() > cursor:
            flush_plain(text[cursor:m.start()])
        label = m.group(1)
        url = m.group(2)
        spans.append({
            "type": "text",
            "text": {"content": label, "link": {"url": url}},
        })
        cursor = m.end()
    if cursor < len(text):
        flush_plain(text[cursor:])

    # Notion caps each rich_text content at 2000 chars; trim defensively.
    for span in spans:
        c = span.get("text", {}).get("content", "")
        if len(c) > 1900:
            span["text"]["content"] = c[:1900]
    return spans


def _block(block_type: str, rich: list[dict]) -> dict:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": rich},
    }


def _content_to_blocks(content: str) -> list[dict]:
    blocks: list[dict] = []
    lines = content.replace("\r\n", "\n").split("\n")
    paragraph_buf: list[str] = []

    def flush_paragraph():
        if not paragraph_buf:
            return
        joined = " ".join(s.strip() for s in paragraph_buf if s.strip())
        if joined:
            blocks.append(_block("paragraph", _rich_text(joined)))
        paragraph_buf.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_paragraph()
            continue

        # Headings
        if line.startswith("### "):
            flush_paragraph()
            blocks.append(_block("heading_3", _rich_text(line[4:].strip())))
            continue
        if line.startswith("## "):
            flush_paragraph()
            blocks.append(_block("heading_2", _rich_text(line[3:].strip())))
            continue
        if line.startswith("# "):
            flush_paragraph()
            blocks.append(_block("heading_1", _rich_text(line[2:].strip())))
            continue

        # Bullets
        m = re.match(r"^\s*[-*•]\s+(.*)$", line)
        if m:
            flush_paragraph()
            blocks.append(_block("bulleted_list_item", _rich_text(m.group(1).strip())))
            continue

        paragraph_buf.append(line)

    flush_paragraph()
    return blocks


# ---------------------------------------------------------------------------
# Destination resolution.
# ---------------------------------------------------------------------------

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def _resolve_target_page_id(channel: str) -> tuple[str, str]:
    """Returns (page_id, label_for_logging)."""
    dest = CHANNEL_TO_DESTINATION.get(channel)
    if dest == "all_hands":
        return ALL_HANDS_PAGE_ID, "All Hands"
    if dest == "newsletter":
        return _resolve_newsletter_month_page(), "Newsletter (current month)"
    raise ValueError(f"Channel '{channel}' has no Notion destination configured.")


def _resolve_newsletter_month_page() -> str:
    """Find the current-month child page under the Newsletter parent.

    Falls back to the parent page if no month-named child is found.
    """
    now = datetime.now(timezone.utc)
    month_name = _MONTH_NAMES[now.month - 1]
    year = str(now.year)

    # List children of the parent page (paginated).
    children: list[dict] = []
    cursor = None
    while True:
        path = f"/blocks/{NEWSLETTER_PARENT_PAGE_ID}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        resp = _notion("GET", path)
        children.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    candidates: list[tuple[int, str, str]] = []  # (score, page_id, title)
    for blk in children:
        if blk.get("type") != "child_page":
            continue
        title = (blk.get("child_page") or {}).get("title", "") or ""
        title_l = title.lower()
        score = 0
        if month_name in title_l:
            score += 10
        if year in title_l:
            score += 5
        if score > 0:
            candidates.append((score, blk.get("id", ""), title))

    if candidates:
        candidates.sort(key=lambda t: -t[0])
        chosen = candidates[0]
        logger.info(f"[notion] Newsletter -> matched month page '{chosen[2]}' ({chosen[1]})")
        return chosen[1]

    # Fallback: append directly to the parent page.
    logger.warning(
        f"[notion] Newsletter: no child page matching '{month_name} {year}' under parent "
        f"{NEWSLETTER_PARENT_PAGE_ID}; appending to the parent page directly."
    )
    return NEWSLETTER_PARENT_PAGE_ID


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def publish_to_notion(
    *,
    content: str,
    channel: str,
    feature_title: str = "",
    feature_url: Optional[str] = None,
) -> dict:
    """Append the given content to the right Notion page for the channel.

    Returns ``{"success": True, "page_id": ..., "page_url": ..., "block_count": N, "destination": "..."}``
    or ``{"success": False, "error": "..."}``.
    """
    if not content or not content.strip():
        return {"success": False, "error": "Content is empty."}
    if channel not in CHANNEL_TO_DESTINATION:
        return {"success": False, "error": f"Channel '{channel}' is not supported for Notion publishing."}

    try:
        page_id, dest_label = _resolve_target_page_id(channel)

        # Build the payload: a small header divider + heading, then content blocks.
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        header_text = (feature_title or "Feature update").strip()
        if len(header_text) > 200:
            header_text = header_text[:197] + "..."

        body_blocks = _content_to_blocks(content)

        blocks: list[dict] = [
            {"object": "block", "type": "divider", "divider": {}},
            _block("heading_3", _rich_text(f"{header_text}  ·  {ts}")),
        ] + body_blocks

        # Notion accepts up to 100 blocks per append call.
        appended = 0
        for i in range(0, len(blocks), 100):
            chunk = blocks[i:i + 100]
            _notion("PATCH", f"/blocks/{page_id}/children", {"children": chunk})
            appended += len(chunk)

        # Build a friendly page URL.
        page_url = f"https://www.notion.so/{page_id.replace('-', '')}"

        logger.info(
            f"[notion] Published channel={channel!r} -> {dest_label} page_id={page_id} "
            f"blocks={appended}"
        )
        return {
            "success": True,
            "page_id": page_id,
            "page_url": page_url,
            "block_count": appended,
            "destination": dest_label,
        }
    except Exception as e:
        logger.exception(f"[notion] publish_to_notion failed channel={channel!r}: {e}")
        return {"success": False, "error": str(e)}
