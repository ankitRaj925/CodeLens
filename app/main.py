import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.config import settings
from app.db import delete_repo, engine, get_repo_files, get_usage, init_db, list_repos
from app.embedder import gemini_error_message
from app.indexer import create_job, get_job, has_running_job, run_index_job
from app.loader import normalize_folder, parse_github_url
from app.rag import NO_MATCH_MESSAGE, build_tree, generate_stream, prepare_answer
from app.smalltalk import smalltalk_reply

logger = logging.getLogger(__name__)

# Folder that holds the chat page (index.html)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the app starts: make sure the tables exist
    init_db()
    yield


app = FastAPI(title="CodeLens", lifespan=lifespan)


class IndexRepoRequest(BaseModel):
    repo_url: str
    folder: str = Field(default="", max_length=200, description="Optional folder to index, e.g. src")


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=6000)


class AskRequest(BaseModel):
    repo: str = Field(default="", description="Indexed repo in owner/name form, e.g. psf/requests")
    question: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=10, description="How many code chunks to retrieve")
    history: list[HistoryMessage] = Field(default_factory=list, max_length=12)


def _line(event: dict) -> str:
    """One JSON object per line (NDJSON), easy to read piece by piece in the browser."""
    return json.dumps(event, ensure_ascii=False) + "\n"


def _stream_response(generator) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", include_in_schema=False)
def root():
    """Serves the chat page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    """Checks the DB connection, the pgvector extension and the API key."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        ext = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).first()
    return {
        "database": "connected",
        "pgvector": "enabled" if ext else "missing",
        "gemini_key_set": bool(settings.google_api_key),
    }


@app.get("/usage")
def usage():
    """Gemini requests made by this app today, and the time left until the daily reset."""
    return get_usage()


@app.post("/index-repo", status_code=202)
def index_repo(body: IndexRepoRequest, background_tasks: BackgroundTasks):
    """Starts indexing in the background and returns a job id to poll."""
    if not settings.google_api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_API_KEY is not set in .env")

    # Validate the input right away so the user gets instant feedback
    try:
        parse_github_url(body.repo_url)
        folder = normalize_folder(body.folder)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if has_running_job():
        raise HTTPException(
            status_code=409,
            detail="Another repository is being indexed. Please wait until it finishes.",
        )

    job_id = create_job(body.repo_url, folder)
    background_tasks.add_task(run_index_job, job_id, body.repo_url, folder)
    return {"job_id": job_id, "status": "running"}


@app.get("/index-jobs/{job_id}")
def index_job(job_id: str):
    """Returns the status, stage and progress percent of an indexing job."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Indexing job not found.")
    return job


@app.get("/repos")
def repos():
    """Lists all indexed repositories."""
    return {"repos": list_repos()}


@app.get("/repos/{owner}/{name}/tree")
def repo_tree(owner: str, name: str):
    """Returns the folder tree of the indexed files of a repo."""
    files = get_repo_files(f"{owner}/{name}")
    if not files:
        raise HTTPException(status_code=404, detail="Repository not found.")
    return {"repo": f"{owner}/{name}", "total_files": len(files), "tree": build_tree(files, max_lines=400)}


@app.delete("/repos/{owner}/{name}")
def remove_repo(owner: str, name: str):
    """Deletes an indexed repository and all of its chunks."""
    deleted = delete_repo(f"{owner}/{name}")
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Repository not found.")
    return {"repo": f"{owner}/{name}", "deleted_chunks": deleted}


@app.post("/ask")
def ask(body: AskRequest):
    """Answers a question about an indexed repo. Streams sources first, then the answer text."""
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Please type a question.")

    # Greetings and similar messages get a ready-made reply (no Gemini call, no repo needed)
    reply = smalltalk_reply(question)
    if reply is not None:

        def smalltalk_stream():
            yield _line({"type": "sources", "sources": []})
            yield _line({"type": "token", "text": reply})
            yield _line({"type": "done"})

        return _stream_response(smalltalk_stream())

    if not settings.google_api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_API_KEY is not set in .env")

    repo = body.repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="Please index or select a repository first.")

    history = [m.model_dump() for m in body.history]

    # Retrieval happens before streaming starts, so real errors can still use HTTP status codes
    try:
        plan = prepare_answer(repo, question, history, body.top_k)
    except genai_errors.APIError as e:
        raise HTTPException(status_code=502, detail=gemini_error_message(e))

    if plan is None:
        raise HTTPException(
            status_code=404,
            detail="This repository is not indexed yet. Please index it first.",
        )

    def event_stream():
        yield _line({"type": "sources", "sources": plan["sources"]})

        # Nothing related found: skip the LLM call and save quota
        if plan["prompt"] is None:
            yield _line({"type": "token", "text": NO_MATCH_MESSAGE})
            yield _line({"type": "done"})
            return

        try:
            for piece in generate_stream(plan["prompt"]):
                yield _line({"type": "token", "text": piece})
            yield _line({"type": "done"})
        except genai_errors.APIError as e:
            yield _line({"type": "error", "message": gemini_error_message(e)})
        except Exception:
            logger.exception("Unexpected error while generating the answer")
            yield _line({"type": "error", "message": "Unexpected error while generating the answer."})

    return _stream_response(event_stream())