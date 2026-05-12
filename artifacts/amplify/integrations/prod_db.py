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
