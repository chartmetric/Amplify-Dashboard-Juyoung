"""Thin shim that delegates the legacy ``publish_announcement`` /
``get_announcements`` / ``dismiss_announcement`` API used by the rest of
the dashboard to the new richer ``ai.announcement_store`` module.

The real CRUD admin lives in ``ai.announcement_store`` (stub mode) or
proxies to chartmetric-api when configured. This file exists so the
older ``/api/publish/inapp`` path and any straight imports of
``integrations.inapp_client`` keep working unchanged.
"""
from __future__ import annotations

import logging

from ai import announcement_store

logger = logging.getLogger(__name__)


def publish_announcement(title: str, body: str,
                         feature_id: str | None = None,
                         category: str | None = None) -> dict:
    return announcement_store.publish_announcement_quick(
        title=title, body=body, feature_id=feature_id, category=category,
    )


def get_announcements(limit: int = 20, status: str | None = None) -> list:
    """Legacy widget API. Returns simplified dicts in newest-first order
    where each dict has ``id``, ``title``, ``body`` (HTML), ``category``
    (first category's name, if any), ``published_at``, and ``status``."""
    from ai.announcement_serializer import slate_to_html

    filt = None
    if status == "active":
        filt = "published"
    elif status:
        filt = status

    raw = announcement_store.list_posts(status=filt, limit=max(limit, 1)).get("items", [])
    out = []
    for p in raw:
        out.append({
            "id": str(p.get("id")),
            "title": p.get("title", ""),
            "body": slate_to_html(p.get("content") or []),
            "category": (p.get("categories") or [{}])[0].get("name", "") if p.get("categories") else "",
            "published_at": (p.get("published_at") or p.get("modified_at") or ""),
            "status": "active" if p.get("is_published") else "draft",
        })
    return out


def dismiss_announcement(announcement_id: str) -> dict:
    """Dismiss is a per-user concept on the consumer side (handled in
    Chartmetric web app). The admin store does not own this state, so we
    no-op and report success so the existing widget UI keeps working."""
    logger.info("[inapp] dismiss called for %s (no-op in admin store)", announcement_id)
    return {"success": True, "id": announcement_id, "note": "dismiss is per-user, handled by reader"}
