import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from google.genai import errors as genai_errors
from pydantic import BaseModel, Field
from sqlalchemy import text

from app import db
from app.auth import (
    ADMIN_COOKIE,
    ADMIN_TTL_SECONDS,
    Identity,
    admin_login_enabled,
    attach_session_cookie,
    check_login_allowed,
    clear_login_failures,
    client_ip,
    get_identity,
    is_https,
    make_admin_token,
    record_login_failure,
    require_admin,
    verify_password,
)
from app.config import settings
from app.embedder import gemini_error_message
from app.indexer import MAX_CHUNKS, create_job, get_job, has_running_job, run_index_job
from app.limits import check_new_index_limit, check_question_limit
from app.loader import normalize_folder, parse_github_url
from app.rag import NO_MATCH_MESSAGE, build_tree, generate_stream, prepare_answer
from app.smalltalk import smalltalk_reply

logger = logging.getLogger(__name__)

# Folder that holds the chat page (index.html)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the app starts: make sure the tables exist and old data is cleaned up
    db.init_db()
    db.maybe_cleanup(0)
    yield


app = FastAPI(title="CodeLens", lifespan=lifespan)


class LoginRequest(BaseModel):
    password: str = Field(max_length=200)


class IndexRepoRequest(BaseModel):
    repo_url: str
    folder: str = Field(default="", max_length=200, description="Optional folder to index (admin only)")
    show_to_visitors: bool = Field(default=False, description="Admin only: show this repo as an example")


class ExampleRequest(BaseModel):
    is_example: bool


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


def _adds_left(identity: Identity) -> int:
    """How many more repositories this visitor may add.

    Every add counts for the last 24 hours, and deleting a repository does not give the slot back.
    """
    used = db.count_activity("add", settings.visitor_data_hours, owner_key=identity.owner)
    return max(0, settings.visitor_max_repos - used)


def _index_in_background(
    job_id: str,
    repo_url: str,
    folder: str,
    owner_key: str,
    ttl_hours: int | None,
    max_chunks: int,
    make_example: bool,
    add_activity_id: int | None,
) -> None:
    """Runs the indexing job. A visitor's add is taken back if the indexing fails."""
    run_index_job(job_id, repo_url, folder, owner_key, ttl_hours, max_chunks, make_example)
    if add_activity_id is not None:
        job = get_job(job_id)
        if job is None or job["status"] != "done":
            try:
                db.delete_activity(add_activity_id)
            except Exception:
                logger.exception("Could not take back the repository slot")


@app.get("/", include_in_schema=False)
def root():
    """Serves the chat page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    """Checks the DB connection, the pgvector extension and the API key."""
    with db.engine.connect() as conn:
        conn.execute(text("SELECT 1"))
        ext = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).first()
    return {
        "database": "connected",
        "pgvector": "enabled" if ext else "missing",
        "gemini_key_set": bool(settings.google_api_key),
    }


# ---------- Admin login ----------


@app.post("/admin/login")
def admin_login(body: LoginRequest, request: Request, response: Response):
    """Logs the admin in with the password. Sets a signed cookie."""
    if not admin_login_enabled():
        raise HTTPException(status_code=400, detail="Admin login is not configured on this server.")
    ip = client_ip(request)
    check_login_allowed(ip)
    if not verify_password(body.password):
        record_login_failure(ip)
        raise HTTPException(status_code=401, detail="Wrong password.")
    clear_login_failures(ip)
    response.set_cookie(
        ADMIN_COOKIE,
        make_admin_token(),
        max_age=ADMIN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https(request),
    )
    return {"is_admin": True}


@app.post("/admin/logout")
def admin_logout(response: Response):
    response.delete_cookie(ADMIN_COOKIE)
    return {"is_admin": False}


@app.get("/me")
def me(request: Request, response: Response, identity: Identity = Depends(get_identity)):
    """Who am I, and what are my limits? Used by the page."""
    attach_session_cookie(identity, request, response)
    return {
        "is_admin": identity.is_admin,
        "admin_login_enabled": admin_login_enabled(),
        "adds_left": 0 if identity.is_admin else _adds_left(identity),
        "limits": {
            "max_repos": settings.visitor_max_repos,
            "max_chunks": settings.visitor_max_chunks,
            "data_hours": settings.visitor_data_hours,
            "questions_per_hour": settings.visitor_questions_per_hour,
        },
    }


@app.get("/usage")
def usage(identity: Identity = Depends(require_admin)):
    """Gemini requests made by this app today (admin only)."""
    return db.get_usage()


# ---------- Repositories ----------


@app.get("/repos")
def repos(request: Request, response: Response, identity: Identity = Depends(get_identity)):
    """Lists the repositories this person may see: examples plus their own."""
    db.maybe_cleanup()
    attach_session_cookie(identity, request, response)
    items = db.list_visible_repos(identity.owner)
    for item in items:
        item["can_delete"] = identity.is_admin or (item["mine"] and not item["is_example"])
        item["can_toggle"] = identity.is_admin
    return {"repos": items}


@app.get("/repos/{owner}/{name}/tree")
def repo_tree(owner: str, name: str, identity: Identity = Depends(get_identity)):
    """Returns the folder tree of the indexed files of a repo."""
    repo_id = f"{owner}/{name}"
    if not db.can_view_repo(identity.owner, repo_id):
        raise HTTPException(status_code=404, detail="Repository not found.")
    files = db.get_repo_files(repo_id)
    return {"repo": repo_id, "total_files": len(files), "tree": build_tree(files, max_lines=400)}


@app.patch("/repos/{owner}/{name}")
def update_repo(owner: str, name: str, body: ExampleRequest, identity: Identity = Depends(require_admin)):
    """Admin only: shows a repository to all visitors as an example, or hides it again."""
    repo_id = f"{owner}/{name}"
    if not db.set_example(repo_id, body.is_example):
        raise HTTPException(status_code=404, detail="Repository not found.")
    return {"repo": repo_id, "is_example": body.is_example}


@app.delete("/repos/{owner}/{name}")
def remove_repo(owner: str, name: str, identity: Identity = Depends(get_identity)):
    """Admin: deletes a repository for everyone. Visitor: removes their own repository."""
    repo_id = f"{owner}/{name}"

    if identity.is_admin:
        if db.delete_repo_completely(repo_id) == 0:
            raise HTTPException(status_code=404, detail="Repository not found.")
        return {"repo": repo_id, "removed": True}

    if not db.can_view_repo(identity.owner, repo_id):
        raise HTTPException(status_code=404, detail="Repository not found.")
    info = db.get_repo_info(repo_id)
    if info and info["is_example"]:
        raise HTTPException(
            status_code=403,
            detail="This is an example repository, so only the admin can remove it.",
        )
    db.revoke_access(repo_id, identity.owner)
    db.cleanup()  # removes the repository data if nobody else uses it
    return {"repo": repo_id, "removed": True}


# ---------- Indexing ----------


@app.post("/index-repo", status_code=202)
def index_repo(
    body: IndexRepoRequest,
    background_tasks: BackgroundTasks,
    identity: Identity = Depends(get_identity),
):
    """Starts indexing in the background and returns a job id to poll."""
    if not settings.google_api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_API_KEY is not set in .env")

    # Validate the input right away so the user gets instant feedback
    try:
        owner, name = parse_github_url(body.repo_url)
        folder = normalize_folder(body.folder) if identity.is_admin else ""
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    repo_id = f"{owner}/{name}"

    if not identity.is_admin:
        info = db.get_repo_info(repo_id)
        if info and info["is_example"]:
            raise HTTPException(
                status_code=409,
                detail="This repository is already available as an example. Just select it from the list.",
            )
        if db.owner_has_access(identity.owner, repo_id):
            raise HTTPException(status_code=409, detail="You have already added this repository.")
        if _adds_left(identity) <= 0:
            raise HTTPException(
                status_code=403,
                detail=f"You have reached the limit of {settings.visitor_max_repos} repositories. Please try again later.",
            )
        # Someone already indexed it: share it, no new embeddings (and no quota) needed
        if info:
            if info["total_chunks"] > settings.visitor_max_chunks:
                raise HTTPException(
                    status_code=400,
                    detail=f"This repository is too large for visitors (limit {settings.visitor_max_chunks} chunks).",
                )
            db.grant_access(repo_id, identity.owner, settings.visitor_data_hours)
            db.log_activity("add", identity.owner, identity.ip)
            return {"status": "ready", "repo": repo_id}

    if has_running_job():
        raise HTTPException(
            status_code=409,
            detail="Another repository is being indexed. Please wait until it finishes.",
        )

    add_activity_id = None
    if not identity.is_admin:
        check_new_index_limit(identity)
        db.log_activity("index", identity.owner, identity.ip)
        # The slot is used right away. It is given back only if the indexing fails.
        add_activity_id = db.log_activity("add", identity.owner, identity.ip)

    job_id = create_job(body.repo_url, folder, identity.owner)
    background_tasks.add_task(
        _index_in_background,
        job_id,
        body.repo_url,
        folder,
        identity.owner,
        None if identity.is_admin else settings.visitor_data_hours,
        MAX_CHUNKS if identity.is_admin else settings.visitor_max_chunks,
        identity.is_admin and body.show_to_visitors,
        add_activity_id,
    )
    return {"job_id": job_id, "status": "running"}


@app.get("/index-jobs/{job_id}")
def index_job(job_id: str, identity: Identity = Depends(get_identity)):
    """Returns the status, stage and progress percent of an indexing job."""
    job = get_job(job_id)
    if job is None or (not identity.is_admin and job["owner"] != identity.owner):
        raise HTTPException(status_code=404, detail="Indexing job not found.")
    return {k: v for k, v in job.items() if k not in ("owner", "created")}


# ---------- Questions ----------


@app.post("/ask")
def ask(body: AskRequest, identity: Identity = Depends(get_identity)):
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

    # Real questions use the Gemini quota, so visitors are limited
    check_question_limit(identity)

    if not settings.google_api_key:
        raise HTTPException(status_code=500, detail="GOOGLE_API_KEY is not set in .env")

    repo = body.repo.strip()
    if not repo:
        raise HTTPException(status_code=400, detail="Please index or select a repository first.")
    if not db.can_view_repo(identity.owner, repo):
        raise HTTPException(status_code=404, detail="This repository is not available.")

    history = [m.model_dump() for m in body.history]

    # Retrieval happens before streaming starts, so real errors can still use HTTP status codes
    try:
        plan = prepare_answer(repo, question, history, body.top_k)
    except genai_errors.APIError as e:
        raise HTTPException(status_code=502, detail=gemini_error_message(e))

    if plan is None:
        raise HTTPException(status_code=404, detail="This repository is not available.")

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