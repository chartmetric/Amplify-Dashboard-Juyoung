import logging
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)

ANNOUNCEMENTS = []


def publish_announcement(title: str, body: str, feature_id: str = None, category: str = None) -> dict:
    announcement = {
        "id": "ann-" + uuid4().hex[:8],
        "title": title,
        "body": body,
        "feature_id": feature_id,
        "category": category,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
    }
    ANNOUNCEMENTS.append(announcement)
    logger.info(f"[inapp] Published announcement {announcement['id']}: {title}")
    return {"success": True, "announcement": announcement}


def get_announcements(limit: int = 20, status: str = None) -> list:
    results = list(reversed(ANNOUNCEMENTS))
    if status:
        results = [a for a in results if a["status"] == status]
    return results[:limit]


def dismiss_announcement(announcement_id: str) -> dict:
    for a in ANNOUNCEMENTS:
        if a["id"] == announcement_id:
            a["status"] = "dismissed"
            logger.info(f"[inapp] Dismissed announcement {announcement_id}")
            return {"success": True, "id": announcement_id}
    return {"success": False, "error": "Announcement not found"}
