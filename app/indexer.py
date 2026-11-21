import logging
import threading
import time
import uuid

from git.exc import GitCommandError
from google.genai import errors as genai_errors

from app.chunker import chunk_repo
from app.db import save_indexed_repo
from app.embedder import embed_texts, gemini_error_message
from app.loader import clone_repo, list_code_files, parse_github_url

logger = logging.getLogger(__name__)

# Size limit for the admin. Visitors get a smaller limit from the settings.
MAX_CHUNKS = 1500

# How many finished jobs we keep in memory
MAX_STORED_JOBS = 50

# In-memory job store. Fine for a single server process.
JOBS: dict[str, dict] = {}
_lock = threading.Lock()


class JobError(Exception):
    """A problem with the user's repo that should be shown as a normal message."""


def create_job(repo_url: str, folder: str, owner_key: str) -> str:
    """Registers a new indexing job and returns its id."""
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        # Drop the oldest finished job when the store is full
        if len(JOBS) >= MAX_STORED_JOBS:
            for old_id, old in sorted(JOBS.items(), key=lambda kv: kv[1]["created"]):
                if old["status"] != "running":
                    del JOBS[old_id]
                    break
        JOBS[job_id] = {
            "job_id": job_id,
            "repo_url": repo_url,
            "folder": folder,
            "owner": owner_key,
            "status": "running",  # running | done | error
            "stage": "Starting",
            "percent": 0,
            "result": None,
            "error": None,
            "created": time.time(),
        }
    return job_id


def has_running_job() -> bool:
    """Only one job at a time, to protect the free API quota."""
    with _lock:
        return any(j["status"] == "running" for j in JOBS.values())


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def _update(job_id: str, **fields) -> None:
    with _lock:
        JOBS[job_id].update(fields)


def run_index_job(
    job_id: str,
    repo_url: str,
    folder: str,
    owner_key: str,
    ttl_hours: int | None,
    max_chunks: int,
    make_example: bool,
) -> None:
    """Clones, chunks, embeds and saves a repo while reporting progress."""
    try:
        _update(job_id, stage="Cloning repository", percent=3)
        repo_path = clone_repo(repo_url)
        owner, name = parse_github_url(repo_url)
        repo_id = f"{owner}/{name}"

        _update(job_id, stage="Reading and splitting code", percent=8)
        files = list_code_files(repo_path, folder)
        chunks = chunk_repo(repo_path, files)

        if not chunks:
            raise JobError("No supported code files found.")
        if len(chunks) > max_chunks:
            # Only the admin can index a single folder
            hint = " or index only one folder (for example: src)" if ttl_hours is None else ""
            raise JobError(
                f"Repo is too large ({len(chunks)} chunks, limit {max_chunks}). Try a smaller repo{hint}."
            )

        # Adding the file path to the text helps the embedding capture where the code lives
        texts = [f'File: {c["file_path"]}\n{c["content"]}' for c in chunks]

        def on_progress(done: int, total: int) -> None:
            # Embedding is the slow part, so it gets the 12% to 95% range of the bar
            _update(
                job_id,
                stage=f"Creating embeddings ({done}/{total})",
                percent=12 + int(83 * done / total),
            )

        _update(job_id, stage=f"Creating embeddings (0/{len(chunks)})", percent=12)
        embeddings = embed_texts(texts, on_progress=on_progress)

        _update(job_id, stage="Saving to database", percent=97)
        # Old data is replaced only after embeddings succeeded, so a failure loses nothing
        save_indexed_repo(repo_id, chunks, embeddings, owner_key, ttl_hours, make_example)

        _update(
            job_id,
            status="done",
            stage="Done",
            percent=100,
            result={
                "repo": repo_id,
                "folder": folder,
                "total_files": len(files),
                "total_chunks": len(chunks),
            },
        )
    except ValueError as e:
        _update(job_id, status="error", stage="Failed", error=str(e))
    except GitCommandError:
        _update(
            job_id,
            status="error",
            stage="Failed",
            error="Cannot clone this repository. Only public repositories are supported.",
        )
    except JobError as e:
        _update(job_id, status="error", stage="Failed", error=str(e))
    except genai_errors.APIError as e:
        _update(job_id, status="error", stage="Failed", error=gemini_error_message(e))
    except Exception:
        logger.exception("Unexpected error while indexing %s", repo_url)
        _update(
            job_id,
            status="error",
            stage="Failed",
            error="Unexpected error while indexing. Check the server logs.",
        )