import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response

from app.config import settings

ADMIN_COOKIE = "codelens_admin"
SESSION_COOKIE = "codelens_session"
ADMIN_TTL_SECONDS = 12 * 3600
SESSION_TTL_SECONDS = 30 * 24 * 3600

# Wrong passwords allowed per IP address before login is blocked for a while
MAX_LOGIN_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60

_lock = threading.Lock()
_failures: dict[str, deque] = defaultdict(deque)


@dataclass
class Identity:
    """Who is making the request."""

    is_admin: bool
    owner: str  # "admin" or "v:<visitor session id>"
    ip: str
    new_session_id: str | None = None  # set when a new visitor cookie has to be sent


def admin_login_enabled() -> bool:
    return bool(settings.admin_password)


def client_ip(request: Request) -> str:
    """Hosting platforms put the visitor address in X-Forwarded-For."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _secret() -> bytes:
    return (settings.secret_key or settings.admin_password).encode()


def _sign(text: str) -> str:
    return hmac.new(_secret(), text.encode(), hashlib.sha256).hexdigest()


def make_admin_token() -> str:
    """A signed token that proves the admin logged in. It expires after ADMIN_TTL_SECONDS."""
    payload = f"admin.{int(time.time()) + ADMIN_TTL_SECONDS}"
    return f"{payload}.{_sign(payload)}"


def _valid_admin_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        role, expires, signature = token.split(".")
        return (
            role == "admin"
            and hmac.compare_digest(signature, _sign(f"{role}.{expires}"))
            and int(expires) > time.time()
        )
    except ValueError:
        return False


def verify_password(password: str) -> bool:
    # compare_digest takes the same time however many characters match
    return hmac.compare_digest(password.encode(), settings.admin_password.encode())


def check_login_allowed(ip: str) -> None:
    """Blocks an IP address that entered too many wrong passwords."""
    now = time.time()
    with _lock:
        attempts = _failures[ip]
        while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= MAX_LOGIN_FAILURES:
            raise HTTPException(
                status_code=429,
                detail="Too many wrong passwords. Please try again in 15 minutes.",
            )


def record_login_failure(ip: str) -> None:
    with _lock:
        _failures[ip].append(time.time())


def clear_login_failures(ip: str) -> None:
    with _lock:
        _failures.pop(ip, None)


def get_identity(request: Request) -> Identity:
    """Admin if the admin cookie is valid (or if no admin password is configured), else a visitor."""
    ip = client_ip(request)
    if not admin_login_enabled() or _valid_admin_token(request.cookies.get(ADMIN_COOKIE)):
        return Identity(is_admin=True, owner="admin", ip=ip)

    session_id = request.cookies.get(SESSION_COOKIE)
    new_session_id = None
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", session_id):
        session_id = secrets.token_urlsafe(24)
        new_session_id = session_id
    return Identity(is_admin=False, owner=f"v:{session_id}", ip=ip, new_session_id=new_session_id)


def require_admin(identity: Identity = Depends(get_identity)) -> Identity:
    if not identity.is_admin:
        raise HTTPException(status_code=403, detail="Admin access is required.")
    return identity


def attach_session_cookie(identity: Identity, request: Request, response: Response) -> None:
    """Gives a new visitor their anonymous session cookie."""
    if identity.new_session_id:
        response.set_cookie(
            SESSION_COOKIE,
            identity.new_session_id,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=is_https(request),
        )