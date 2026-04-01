import json
import logging
from contextlib import contextmanager

import config

logger = logging.getLogger("amplify.db")

_db_available = False
_connection_string = None


def _init():
    global _db_available, _connection_string
    url = config.DATABASE_URL
    if not url:
        logger.warning("[db] DATABASE_URL not set — running in memory-only mode")
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        conn.close()
        _connection_string = url
        logger.info("[db] Database connection verified")
        _create_tables()
        _db_available = True
    except Exception as e:
        logger.warning(f"[db] Could not connect to database — running in memory-only mode: {e}")
        _connection_string = None
        _db_available = False


def is_available() -> bool:
    return _db_available


@contextmanager
def _get_conn():
    import psycopg2
    conn = psycopg2.connect(_connection_string)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_tables():
    ddl = """
    CREATE TABLE IF NOT EXISTS amplify_classifications (
        feature_id TEXT PRIMARY KEY,
        data JSONB NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS amplify_manual_overrides (
        feature_id TEXT PRIMARY KEY,
        data JSONB NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS amplify_classification_overrides (
        id SERIAL PRIMARY KEY,
        feature_id TEXT NOT NULL,
        data JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS amplify_feedback (
        id SERIAL PRIMARY KEY,
        channel TEXT NOT NULL,
        data JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS amplify_keyword_overrides (
        keyword TEXT PRIMARY KEY,
        override_count INT NOT NULL DEFAULT 0,
        match_count INT NOT NULL DEFAULT 0,
        last_overridden_at TIMESTAMPTZ
    );
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
    logger.info("[db] Tables ensured")


# ── Classifications ────────────────────────────────────────────────────────────

def load_classification_by_id(feature_id: str) -> dict | None:
    if not _db_available:
        return None
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM amplify_classifications WHERE feature_id = %s",
                    (feature_id,),
                )
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"[db] load_classification_by_id failed for {feature_id}: {e}")
        return None


def load_classifications() -> dict:
    if not _db_available:
        return {}
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT feature_id, data FROM amplify_classifications")
                rows = cur.fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        logger.error(f"[db] load_classifications failed: {e}")
        return {}


def save_classification(feature_id: str, data: dict):
    if not _db_available:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amplify_classifications (feature_id, data, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (feature_id) DO UPDATE
                        SET data = EXCLUDED.data, updated_at = NOW()
                    """,
                    (feature_id, json.dumps(data)),
                )
    except Exception as e:
        logger.error(f"[db] save_classification failed for {feature_id}: {e}")


def delete_all_classifications():
    if not _db_available:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM amplify_classifications")
    except Exception as e:
        logger.error(f"[db] delete_all_classifications failed: {e}")


# ── Manual Overrides ───────────────────────────────────────────────────────────

def load_manual_overrides() -> dict:
    if not _db_available:
        return {}
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT feature_id, data FROM amplify_manual_overrides")
                rows = cur.fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        logger.error(f"[db] load_manual_overrides failed: {e}")
        return {}


def save_manual_override(feature_id: str, data: dict):
    if not _db_available:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amplify_manual_overrides (feature_id, data, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (feature_id) DO UPDATE
                        SET data = EXCLUDED.data, updated_at = NOW()
                    """,
                    (feature_id, json.dumps(data)),
                )
    except Exception as e:
        logger.error(f"[db] save_manual_override failed for {feature_id}: {e}")


def delete_manual_override(feature_id: str):
    if not _db_available:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM amplify_manual_overrides WHERE feature_id = %s",
                    (feature_id,),
                )
    except Exception as e:
        logger.error(f"[db] delete_manual_override failed for {feature_id}: {e}")


# ── Classification Overrides ───────────────────────────────────────────────────

def load_classification_overrides() -> list:
    if not _db_available:
        return []
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM amplify_classification_overrides ORDER BY created_at DESC"
                )
                rows = cur.fetchall()
        return [row[0] for row in rows]
    except Exception as e:
        logger.error(f"[db] load_classification_overrides failed: {e}")
        return []


def save_classification_override(data: dict):
    if not _db_available:
        return
    try:
        feature_id = data.get("feature_id", "")
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO amplify_classification_overrides (feature_id, data) VALUES (%s, %s)",
                    (feature_id, json.dumps(data)),
                )
    except Exception as e:
        logger.error(f"[db] save_classification_override failed: {e}")


# ── Feedback ───────────────────────────────────────────────────────────────────

def load_feedback() -> dict:
    if not _db_available:
        return {}
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT channel, data FROM amplify_feedback ORDER BY created_at ASC"
                )
                rows = cur.fetchall()
        result: dict = {}
        for channel, data in rows:
            result.setdefault(channel, []).append(data)
        return result
    except Exception as e:
        logger.error(f"[db] load_feedback failed: {e}")
        return {}


def save_feedback_record(channel: str, data: dict):
    if not _db_available:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO amplify_feedback (channel, data) VALUES (%s, %s)",
                    (channel, json.dumps(data)),
                )
    except Exception as e:
        logger.error(f"[db] save_feedback_record failed for channel {channel}: {e}")


def delete_feedback(channel: str = None):
    if not _db_available:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                if channel:
                    cur.execute(
                        "DELETE FROM amplify_feedback WHERE channel = %s", (channel,)
                    )
                else:
                    cur.execute("DELETE FROM amplify_feedback")
    except Exception as e:
        logger.error(f"[db] delete_feedback failed: {e}")


# ── Keyword Overrides ──────────────────────────────────────────────────────────

def increment_keyword_match(keyword: str):
    if not _db_available:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amplify_keyword_overrides (keyword, match_count, override_count)
                    VALUES (%s, 1, 0)
                    ON CONFLICT (keyword) DO UPDATE
                        SET match_count = amplify_keyword_overrides.match_count + 1
                    """,
                    (keyword,),
                )
    except Exception as e:
        logger.error(f"[db] increment_keyword_match failed for '{keyword}': {e}")


def increment_keyword_override(keyword: str):
    if not _db_available:
        return
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amplify_keyword_overrides (keyword, match_count, override_count, last_overridden_at)
                    VALUES (%s, 0, 1, NOW())
                    ON CONFLICT (keyword) DO UPDATE
                        SET override_count = amplify_keyword_overrides.override_count + 1,
                            last_overridden_at = NOW()
                    """,
                    (keyword,),
                )
    except Exception as e:
        logger.error(f"[db] increment_keyword_override failed for '{keyword}': {e}")


def load_keyword_stats() -> list:
    if not _db_available:
        return []
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT keyword, override_count, match_count, last_overridden_at FROM amplify_keyword_overrides ORDER BY keyword"
                )
                rows = cur.fetchall()
        return [
            {
                "keyword": row[0],
                "override_count": row[1],
                "match_count": row[2],
                "last_overridden_at": row[3].isoformat() if row[3] else None,
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"[db] load_keyword_stats failed: {e}")
        return []


def get_keyword_override_counts() -> dict:
    if not _db_available:
        return {}
    try:
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT keyword, override_count FROM amplify_keyword_overrides"
                )
                rows = cur.fetchall()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        logger.error(f"[db] get_keyword_override_counts failed: {e}")
        return {}


_init()
