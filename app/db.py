import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from app.config import settings

logger = logging.getLogger(__name__)

# Engine manages the connection pool to PostgreSQL.
# pool_pre_ping checks a connection before using it, which matters for databases that sleep when idle.
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300)

# Must match the output size of the embedding model (see embedder.py)
EMBEDDING_DIM = 768

# Gemini daily quotas reset at midnight Pacific time
PACIFIC = ZoneInfo("America/Los_Angeles")

_last_cleanup = 0.0


def _vector_text(vec: list[float]) -> str:
    """pgvector accepts a vector as the text form "[0.1,0.2,...]"."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def init_db() -> None:
    """Creates the tables and indexes if they do not exist."""
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS chunks (
                    id         SERIAL PRIMARY KEY,
                    repo_id    TEXT NOT NULL,
                    file_path  TEXT NOT NULL,
                    start_line INTEGER NOT NULL,
                    end_line   INTEGER NOT NULL,
                    content    TEXT NOT NULL,
                    embedding  VECTOR({EMBEDDING_DIM}) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        # HNSW index makes similarity search fast; cosine distance suits text embeddings
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS chunks_embedding_idx "
                "ON chunks USING hnsw (embedding vector_cosine_ops)"
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS chunks_repo_idx ON chunks (repo_id)"))

        # One row per indexed repository. is_example = shown to every visitor.
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS repos (
                    repo_id    TEXT PRIMARY KEY,
                    is_example BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        # Who can see a repository besides the examples. owner_key is "admin" or "v:<session id>".
        # expires_at is NULL for the admin, and set for visitors.
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS repo_access (
                    owner_key  TEXT NOT NULL,
                    repo_id    TEXT NOT NULL REFERENCES repos(repo_id) ON DELETE CASCADE,
                    expires_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (owner_key, repo_id)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS repo_access_repo_idx ON repo_access (repo_id)"))

        # Questions and indexing requests, used for visitor limits
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS activity_log (
                    id         BIGSERIAL PRIMARY KEY,
                    kind       TEXT NOT NULL,
                    owner_key  TEXT NOT NULL,
                    ip         TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS activity_log_idx ON activity_log (kind, created_at)"))

        # Counts the Gemini requests made by this app, one row per day and kind
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS usage_daily (
                    day   DATE NOT NULL,
                    kind  TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, kind)
                )
                """
            )
        )

        # Repositories indexed before these tables existed become admin repositories
        conn.execute(
            text(
                """
                WITH missing AS (
                    SELECT DISTINCT c.repo_id FROM chunks c
                    WHERE NOT EXISTS (SELECT 1 FROM repos r WHERE r.repo_id = c.repo_id)
                ), added AS (
                    INSERT INTO repos (repo_id) SELECT repo_id FROM missing RETURNING repo_id
                )
                INSERT INTO repo_access (owner_key, repo_id) SELECT 'admin', repo_id FROM added
                ON CONFLICT DO NOTHING
                """
            )
        )


# ---------- Saving and deleting repositories ----------


def save_indexed_repo(
    repo_id: str,
    chunks: list[dict],
    embeddings: list[list[float]],
    owner_key: str,
    ttl_hours: int | None,
    make_example: bool,
) -> None:
    """Replaces the chunks of a repo and records who owns it, all in one transaction."""
    rows = [
        {
            "repo_id": repo_id,
            "file_path": c["file_path"],
            "start_line": c["start_line"],
            "end_line": c["end_line"],
            # PostgreSQL text columns cannot hold NUL characters
            "content": c["content"].replace("\x00", ""),
            "embedding": _vector_text(emb),
        }
        for c, emb in zip(chunks, embeddings)
    ]
    expires_at = None if ttl_hours is None else datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM chunks WHERE repo_id = :repo_id"), {"repo_id": repo_id})
        conn.execute(
            text(
                "INSERT INTO chunks (repo_id, file_path, start_line, end_line, content, embedding) "
                "VALUES (:repo_id, :file_path, :start_line, :end_line, :content, CAST(:embedding AS vector))"
            ),
            rows,
        )
        # An existing example flag is never switched off by re-indexing
        conn.execute(
            text(
                "INSERT INTO repos (repo_id, is_example) VALUES (:repo_id, :ex) "
                "ON CONFLICT (repo_id) DO UPDATE SET is_example = repos.is_example OR EXCLUDED.is_example"
            ),
            {"repo_id": repo_id, "ex": make_example},
        )
        conn.execute(
            text(
                "INSERT INTO repo_access (owner_key, repo_id, expires_at) VALUES (:owner, :repo_id, :exp) "
                "ON CONFLICT (owner_key, repo_id) DO UPDATE SET expires_at = EXCLUDED.expires_at"
            ),
            {"owner": owner_key, "repo_id": repo_id, "exp": expires_at},
        )


def delete_repo_completely(repo_id: str) -> int:
    """Admin delete: removes the repository and all its chunks for everyone."""
    with engine.begin() as conn:
        chunks = conn.execute(text("DELETE FROM chunks WHERE repo_id = :r"), {"r": repo_id}).rowcount
        repos = conn.execute(text("DELETE FROM repos WHERE repo_id = :r"), {"r": repo_id}).rowcount
    return chunks + repos


def set_example(repo_id: str, is_example: bool) -> bool:
    """Shows or hides a repository for visitors. Returns False if the repo does not exist."""
    with engine.begin() as conn:
        result = conn.execute(
            text("UPDATE repos SET is_example = :ex WHERE repo_id = :r"),
            {"ex": is_example, "r": repo_id},
        )
        return result.rowcount > 0


def cleanup() -> None:
    """Removes expired visitor access, repositories nobody can see any more, and old log rows."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM repo_access WHERE expires_at IS NOT NULL AND expires_at < now()"))
        conn.execute(
            text(
                """
                DELETE FROM chunks WHERE repo_id IN (
                    SELECT r.repo_id FROM repos r
                    WHERE NOT r.is_example
                      AND NOT EXISTS (SELECT 1 FROM repo_access a WHERE a.repo_id = r.repo_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                DELETE FROM repos AS r
                WHERE NOT r.is_example
                  AND NOT EXISTS (SELECT 1 FROM repo_access a WHERE a.repo_id = r.repo_id)
                """
            )
        )
        conn.execute(text("DELETE FROM activity_log WHERE created_at < now() - interval '3 days'"))


def maybe_cleanup(min_interval_seconds: int = 600) -> None:
    """Runs cleanup() at most once every few minutes. Never raises."""
    global _last_cleanup
    if time.time() - _last_cleanup < min_interval_seconds:
        return
    _last_cleanup = time.time()
    try:
        cleanup()
    except Exception:
        logger.exception("Cleanup failed")


# ---------- Who can see what ----------

_ACTIVE_ACCESS = "(a.expires_at IS NULL OR a.expires_at > now())"


def list_visible_repos(owner_key: str) -> list[dict]:
    """Example repositories plus the ones this owner has access to."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f"""
                SELECT r.repo_id, r.is_example, (a.owner_key IS NOT NULL) AS mine,
                       c.total_chunks, c.total_files, c.indexed_at
                FROM repos r
                JOIN (
                    SELECT repo_id, COUNT(*) AS total_chunks,
                           COUNT(DISTINCT file_path) AS total_files, MAX(created_at) AS indexed_at
                    FROM chunks GROUP BY repo_id
                ) c ON c.repo_id = r.repo_id
                LEFT JOIN repo_access a
                       ON a.repo_id = r.repo_id AND a.owner_key = :owner AND {_ACTIVE_ACCESS}
                WHERE r.is_example OR a.owner_key IS NOT NULL
                ORDER BY r.is_example DESC, c.indexed_at DESC
                """
            ),
            {"owner": owner_key},
        )
        return [
            {
                "repo": row.repo_id,
                "is_example": row.is_example,
                "mine": row.mine,
                "total_files": row.total_files,
                "total_chunks": row.total_chunks,
                "indexed_at": row.indexed_at.isoformat(),
            }
            for row in result
        ]


def can_view_repo(owner_key: str, repo_id: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"""
                SELECT 1 FROM repos r
                LEFT JOIN repo_access a
                       ON a.repo_id = r.repo_id AND a.owner_key = :owner AND {_ACTIVE_ACCESS}
                WHERE r.repo_id = :repo_id AND (r.is_example OR a.owner_key IS NOT NULL)
                """
            ),
            {"owner": owner_key, "repo_id": repo_id},
        ).first()
        return row is not None


def get_repo_info(repo_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT r.is_example, (SELECT COUNT(*) FROM chunks c WHERE c.repo_id = r.repo_id) AS n "
                "FROM repos r WHERE r.repo_id = :repo_id"
            ),
            {"repo_id": repo_id},
        ).first()
        return {"is_example": row.is_example, "total_chunks": row.n} if row else None


def owner_has_access(owner_key: str, repo_id: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"SELECT 1 FROM repo_access a WHERE a.owner_key = :owner AND a.repo_id = :repo_id AND {_ACTIVE_ACCESS}"
            ),
            {"owner": owner_key, "repo_id": repo_id},
        ).first()
        return row is not None


def count_owner_repos(owner_key: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(f"SELECT COUNT(*) FROM repo_access a WHERE a.owner_key = :owner AND {_ACTIVE_ACCESS}"),
            {"owner": owner_key},
        ).scalar_one()


def grant_access(repo_id: str, owner_key: str, ttl_hours: int | None) -> None:
    """Gives an owner access to a repo that is already indexed (no new embeddings needed)."""
    expires_at = None if ttl_hours is None else datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO repo_access (owner_key, repo_id, expires_at) VALUES (:owner, :repo_id, :exp) "
                "ON CONFLICT (owner_key, repo_id) DO UPDATE SET expires_at = EXCLUDED.expires_at"
            ),
            {"owner": owner_key, "repo_id": repo_id, "exp": expires_at},
        )


def revoke_access(repo_id: str, owner_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM repo_access WHERE owner_key = :owner AND repo_id = :repo_id"),
            {"owner": owner_key, "repo_id": repo_id},
        )


# ---------- Activity log (visitor limits) ----------


def log_activity(kind: str, owner_key: str, ip: str) -> int:
    """Records one action and returns its id (so it can be taken back if the action fails)."""
    with engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO activity_log (kind, owner_key, ip) VALUES (:kind, :owner, :ip) RETURNING id"),
            {"kind": kind, "owner": owner_key, "ip": ip},
        ).scalar_one()


def delete_activity(activity_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM activity_log WHERE id = :id"), {"id": activity_id})


def count_activity(kind: str, hours: int, owner_key: str | None = None, ip: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM activity_log WHERE kind = :kind AND created_at > now() - make_interval(hours => :hours)"
    params: dict = {"kind": kind, "hours": hours}
    if owner_key:
        sql += " AND owner_key = :owner"
        params["owner"] = owner_key
    if ip:
        sql += " AND ip = :ip"
        params["ip"] = ip
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


# ---------- Reading chunks ----------


def get_repo_files(repo_id: str) -> list[str]:
    """Returns the paths of all indexed files of a repo."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT DISTINCT file_path FROM chunks "
                "WHERE repo_id = :repo_id ORDER BY file_path"
            ),
            {"repo_id": repo_id},
        )
        return [row.file_path for row in result]


def search_chunks(repo_id: str, query_embedding: list[float], top_k: int = 5) -> list[dict]:
    """Returns the chunks of a repo that are closest in meaning to the query vector."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT file_path, start_line, end_line, content,
                       1 - (embedding <=> CAST(:q AS vector)) AS similarity
                FROM chunks
                WHERE repo_id = :repo_id
                ORDER BY embedding <=> CAST(:q AS vector)
                LIMIT :k
                """
            ),
            {"q": _vector_text(query_embedding), "repo_id": repo_id, "k": top_k},
        )
        return [
            {
                "file_path": row.file_path,
                "start_line": row.start_line,
                "end_line": row.end_line,
                "content": row.content,
                "similarity": round(float(row.similarity), 4),
            }
            for row in result
        ]


# ---------- Gemini usage counters ----------


def record_usage(kind: str, amount: int = 1) -> None:
    """Adds to today's counter (Pacific day). Never raises, usage tracking must not break requests."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO usage_daily (day, kind, count) VALUES (:day, :kind, :n) "
                    "ON CONFLICT (day, kind) DO UPDATE SET count = usage_daily.count + EXCLUDED.count"
                ),
                {"day": datetime.now(PACIFIC).date(), "kind": kind, "n": amount},
            )
    except Exception:
        logger.exception("Could not record usage")


def get_usage() -> dict:
    """Returns today's Gemini usage of this app and the time left until the daily reset."""
    now = datetime.now(PACIFIC)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Subtract in UTC so daylight saving changes cannot skew the result
    seconds_left = int(
        (next_midnight.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    )
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT kind, count AS n FROM usage_daily WHERE day = :day"),
            {"day": now.date()},
        )
        counts = {row.kind: row.n for row in result}
    return {
        "day": str(now.date()),
        "embedding_requests": counts.get("embed_requests", 0),
        "chunks_embedded": counts.get("embedded_chunks", 0),
        "questions": counts.get("chat_requests", 0),
        "daily_limit_hit": counts.get("daily_limit_hit", 0) > 0,
        "seconds_until_reset": seconds_left,
    }