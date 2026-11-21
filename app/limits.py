from fastapi import HTTPException

from app import db
from app.auth import Identity
from app.config import settings


def check_question_limit(identity: Identity) -> None:
    """Visitors only: limits questions per visitor and per day to protect the API quota."""
    if identity.is_admin:
        return

    per_hour = settings.visitor_questions_per_hour
    if (
        db.count_activity("question", 1, owner_key=identity.owner) >= per_hour
        or db.count_activity("question", 1, ip=identity.ip) >= per_hour
    ):
        raise HTTPException(
            status_code=429,
            detail=f"You have reached the limit of {per_hour} questions per hour. Please try again later.",
        )
    if db.count_activity("question", 24) >= settings.visitor_questions_per_day:
        raise HTTPException(
            status_code=429,
            detail="The demo has reached its daily question limit. Please come back tomorrow, or run CodeLens locally.",
        )
    db.log_activity("question", identity.owner, identity.ip)


def check_new_index_limit(identity: Identity) -> None:
    """Visitors only: limits how many new repositories can be indexed (each one uses API quota)."""
    per_ip = settings.visitor_new_repos_per_ip_per_day
    if db.count_activity("index", 24, ip=identity.ip) >= per_ip:
        raise HTTPException(
            status_code=429,
            detail=f"You can index up to {per_ip} new repositories per day. Please try again tomorrow.",
        )
    if db.count_activity("index", 24) >= settings.visitor_new_repos_per_day:
        raise HTTPException(
            status_code=429,
            detail="The demo has reached its daily indexing limit. Please try again tomorrow, or use an example repository.",
        )