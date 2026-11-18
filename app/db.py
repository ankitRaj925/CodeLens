import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from app.config import settings

logger = logging.getLogger(__name__)

# Engine manages the connection pool to PostgreSQL
engine = create_engine(settings.database_url)

# Must match the output size of the embedding model (see embedder.py)
EMBEDDING_DIM = 768

# Gemini daily quotas reset at midnight Pacific time
PACIFIC = ZoneInfo("America/Los_Angeles")


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


def replace_repo_chunks(
    repo_id: str, chunks: list[dict], embeddings: list[list[float]]
) -> None:
    """Deletes old chunks of the repo and inserts the new ones in one transaction."""
    rows = [
        {
            "repo_id": repo_id,
            "file_path": c["file_path"],
            "start_line": c["start_line"],
            "end_line": c["end_line"],
            # PostgreSQL text columns cannot hold NUL characters
            "content": c["content"].replace("\x00", ""),
            # pgvector accepts a vector as the text form "[0.1,0.2,...]"
            "embedding": "[" + ",".join(f"{x:.6f}" for x in emb) + "]",
        }
        for c, emb in zip(chunks, embeddings)
    ]
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM chunks WHERE repo_id = :repo_id"), {"repo_id": repo_id})
        conn.execute(
            text(
                "INSERT INTO chunks (repo_id, file_path, start_line, end_line, content, embedding) "
                "VALUES (:repo_id, :file_path, :start_line, :end_line, :content, CAST(:embedding AS vector))"
            ),
            rows,
        )


def list_repos() -> list[dict]:
    """Returns every indexed repo with its file and chunk counts."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT repo_id, COUNT(*) AS total_chunks, "
                "COUNT(DISTINCT file_path) AS total_files, MAX(created_at) AS indexed_at "
                "FROM chunks GROUP BY repo_id ORDER BY indexed_at DESC"
            )
        )
        return [
            {
                "repo": row.repo_id,
                "total_files": row.total_files,
                "total_chunks": row.total_chunks,
                "indexed_at": row.indexed_at.isoformat(),
            }
            for row in result
        ]


def delete_repo(repo_id: str) -> int:
    """Deletes all chunks of a repo and returns how many rows were removed."""
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM chunks WHERE repo_id = :repo_id"), {"repo_id": repo_id}
        )
        return result.rowcount


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
    vector_text = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
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
            {"q": vector_text, "repo_id": repo_id, "k": top_k},
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