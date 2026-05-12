"""Direct connection to the Chartmetric production PostgreSQL database.

Used for write operations (create, update, soft-delete) that bypass the
Chartmetric REST API so Amplify can act as an independent admin without
going through the prod API server.

Connection credentials come from:
  PROD_RDS_HOST     — RDS endpoint hostname
  PROD_RDS_USER     — DB username
  PROD_RDS_PASSWORD — DB password

Database: chartmetric  |  Schema: chartmetric  |  Port: 5432 (SSL required)

All functions raise RuntimeError if the required env vars are absent, and
let psycopg2 exceptions propagate so callers can handle them.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("amplify.prod_db")

_SCHEMA = "chartmetric"
_DB = "chartmetric"
_PORT = 5432


def _get_conn():
    """Return a new psycopg2 connection to the Chartmetric prod RDS."""
    import psycopg2

    host = os.environ.get("PROD_RDS_HOST", "")
    user = os.environ.get("PROD_RDS_USER", "")
    password = os.environ.get("PROD_RDS_PASSWORD", "")
    if not (host and user and password):
        raise RuntimeError(
            "PROD_RDS_HOST / PROD_RDS_USER / PROD_RDS_PASSWORD are not all set"
        )
    return psycopg2.connect(
        host=host,
        user=user,
        password=password,
        dbname=_DB,
        port=_PORT,
        sslmode="require",
        connect_timeout=8,
    )


def is_available() -> bool:
    """Return True if the prod DB credentials are configured."""
    return all(
        os.environ.get(k)
        for k in ("PROD_RDS_HOST", "PROD_RDS_USER", "PROD_RDS_PASSWORD")
    )


def soft_delete_post(chartmetric_id: int) -> bool:
    """Set deleted_at = now() on the given announcement_post row.

    Returns True if a row was updated, False if no row matched the id.
    Raises on DB errors.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {_SCHEMA}.announcement_post
                       SET deleted_at  = NOW(),
                           modified_at = NOW()
                     WHERE id = %s
                       AND deleted_at IS NULL
                    """,
                    (chartmetric_id,),
                )
                updated = cur.rowcount
        logger.info(
            "[prod_db] soft_delete_post chartmetric_id=%s rows_updated=%s",
            chartmetric_id,
            updated,
        )
        return updated > 0
    finally:
        conn.close()


def restore_post(chartmetric_id: int) -> bool:
    """Clear deleted_at on the given announcement_post row (restore it).

    Returns True if a row was updated, False if no row matched the id.
    Raises on DB errors.
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {_SCHEMA}.announcement_post
                       SET deleted_at  = NULL,
                           modified_at = NOW()
                     WHERE id = %s
                       AND deleted_at IS NOT NULL
                    """,
                    (chartmetric_id,),
                )
                updated = cur.rowcount
        logger.info(
            "[prod_db] restore_post chartmetric_id=%s rows_updated=%s",
            chartmetric_id,
            updated,
        )
        return updated > 0
    finally:
        conn.close()


def get_deleted_post_ids() -> set[int]:
    """Return the set of chartmetric announcement_post ids that are soft-deleted."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id FROM {_SCHEMA}.announcement_post WHERE deleted_at IS NOT NULL"
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read helpers — bypass the Chartmetric REST API entirely
# ---------------------------------------------------------------------------

def _dt_to_iso(v) -> str | None:
    """Convert a datetime (or existing str) to an ISO-8601 string."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return v.isoformat()


_POST_QUERY = f"""
    SELECT
        ap.id,
        ap.title,
        ap.is_published,
        ap.is_pinned,
        ap.is_boosted,
        ap.published_at,
        ap.created_at,
        ap.modified_at,
        ap.image_url,
        ap.content,
        ap.translations,
        ap.deleted_at,
        ARRAY_AGG(DISTINCT lapc.announcement_category_id)
            FILTER (WHERE lapc.announcement_category_id IS NOT NULL)  AS category_ids,
        ARRAY_AGG(DISTINCT abt.boost_name)
            FILTER (WHERE abt.boost_name IS NOT NULL)                 AS boost_names
    FROM {_SCHEMA}.announcement_post ap
    LEFT JOIN {_SCHEMA}.l_announcement_post_category lapc
           ON lapc.announcement_post_id = ap.id
    LEFT JOIN {_SCHEMA}.l_announcement_post_boost_type lapbt
           ON lapbt.announcement_post_id = ap.id
    LEFT JOIN {_SCHEMA}.announcement_boost_type abt
           ON abt.id = lapbt.announcement_boost_type_id
    GROUP BY ap.id
"""


def _row_to_post(row: dict, categories_by_id: dict) -> dict:
    """Convert a raw DB row dict to the post shape the UI expects."""
    cat_ids = row.get("category_ids") or []
    categories = [
        {
            "id": categories_by_id[cid]["id"],
            "name": categories_by_id[cid]["name"],
            "color": categories_by_id[cid]["color"],
            "translations": categories_by_id[cid].get("translations") or {},
        }
        for cid in cat_ids
        if cid in categories_by_id
    ]
    is_published = bool(row.get("is_published"))
    deleted_at = _dt_to_iso(row.get("deleted_at"))
    if deleted_at:
        status = "deleted"
    elif is_published:
        status = "published"
    else:
        status = "draft"

    return {
        "id": row["id"],
        "title": row.get("title") or "",
        "content": row.get("content") or [],
        "translations": row.get("translations") or {},
        "is_published": is_published,
        "is_pinned": bool(row.get("is_pinned")),
        "is_boosted": bool(row.get("is_boosted")),
        "published_at": _dt_to_iso(row.get("published_at")),
        "created_at": _dt_to_iso(row.get("created_at")),
        "modified_at": _dt_to_iso(row.get("modified_at")),
        "image_url": row.get("image_url"),
        "deleted_at": deleted_at,
        "category_ids": cat_ids,
        "categories": categories,
        "boost_types": list(row.get("boost_names") or []),
        "status": status,
    }


def list_categories() -> list[dict]:
    """Return all active announcement categories from the prod DB."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, name, color, translations
                  FROM {_SCHEMA}.announcement_category
                 WHERE deleted_at IS NULL
                 ORDER BY name
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def list_posts() -> dict:
    """Return all announcement posts (active + deleted) from the prod DB.

    Returns ``{"items": [...], "total": N}``.  Each item is a fully-hydrated
    post dict (categories embedded, status derived, timestamps as ISO strings).
    Posts are ordered: pinned first, then by modified_at desc.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Fetch categories first so we can hydrate posts.
            cur.execute(
                f"""
                SELECT id, name, color, translations
                  FROM {_SCHEMA}.announcement_category
                 WHERE deleted_at IS NULL
                """
            )
            cats_by_id = {
                r[0]: {"id": r[0], "name": r[1], "color": r[2],
                        "translations": r[3] or {}}
                for r in cur.fetchall()
            }

            cur.execute(
                _POST_QUERY + " ORDER BY ap.is_pinned DESC, ap.modified_at DESC"
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        items = [_row_to_post(r, cats_by_id) for r in rows]
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


def get_post(chartmetric_id: int) -> dict | None:
    """Return a single announcement post by its Chartmetric DB id, or None."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, name, color, translations
                  FROM {_SCHEMA}.announcement_category
                 WHERE deleted_at IS NULL
                """
            )
            cats_by_id = {
                r[0]: {"id": r[0], "name": r[1], "color": r[2],
                        "translations": r[3] or {}}
                for r in cur.fetchall()
            }

            # _POST_QUERY ends with "GROUP BY ap.id" — inject WHERE before it.
            single_query = _POST_QUERY.replace(
                "GROUP BY ap.id",
                "WHERE ap.id = %s\n    GROUP BY ap.id",
            )
            cur.execute(single_query, (chartmetric_id,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            if row is None:
                return None
            return _row_to_post(dict(zip(cols, row)), cats_by_id)
    finally:
        conn.close()
