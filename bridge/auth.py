import hashlib
import hmac
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import delete, select

from . import config, db
from .errors import BridgeError


@dataclass(frozen=True)
class Actor:
    id: str
    tenant_id: str
    email: str
    name: str
    role: str
    teams: tuple[str, ...]
    session_id: str
    run_id: str | None = None
    lease: str | None = None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_session(conn, user_id, *, run_id=None, lease=None, expires=None):
    token = secrets.token_urlsafe(32)
    expires = expires or db.now() + config.SESSION_SECONDS
    db.add(conn, db.sessions, id=digest(token), user_id=user_id, expires=expires, run_id=run_id, lease=lease)
    return token


def login(email, password):
    if not config.DEMO or not hmac.compare_digest(password.encode(), config.DEV_PASSWORD.encode()):
        raise BridgeError("AUTH_DENIED", "Invalid credentials", 401)
    with db.transaction() as conn:
        user = db.row(conn, db.users, db.users.c.email == email.strip().lower(), db.users.c.active.is_(True))
        if not user:
            raise BridgeError("AUTH_DENIED", "Invalid credentials", 401)
        return create_session(conn, user["id"])


def logout(request):
    token = request.cookies.get(config.COOKIE)
    if token:
        with db.transaction() as conn:
            conn.execute(delete(db.sessions).where(db.sessions.c.id == digest(token)))


def cookie(response, token):
    response.set_cookie(
        config.COOKIE,
        token,
        max_age=config.SESSION_SECONDS,
        httponly=True,
        secure=config.SECURE_COOKIE,
        samesite="strict",
        path="/",
    )


def from_session(conn, sid):
    session = db.row(conn, db.sessions, db.sessions.c.id == sid, db.sessions.c.expires > db.now())
    user = session and db.row(
        conn, db.users, db.users.c.id == session["user_id"], db.users.c.active.is_(True)
    )
    if not user or not db.row(
        conn, db.tenants, db.tenants.c.id == user["tenant_id"], db.tenants.c.status == "active"
    ):
        raise BridgeError("AUTH_EXPIRED", "Sign in with an authorized account", 401)
    team_ids = conn.execute(
        select(db.memberships.c.team_id).where(
            db.memberships.c.user_id == user["id"], db.memberships.c.tenant_id == user["tenant_id"]
        )
    ).scalars()
    if session["run_id"]:
        run = db.scoped(conn, db.runs, user["tenant_id"], session["run_id"])
        if not run or run["lease"] != session["lease"] or (run["leased_until"] or 0) <= db.now():
            raise BridgeError("AUTH_EXPIRED", "This browser session is no longer active", 401)
    return Actor(
        user["id"],
        user["tenant_id"],
        user["email"],
        user["name"],
        user["role"],
        tuple(team_ids),
        sid,
        session["run_id"],
        session["lease"],
    )


def actor(request: Request):
    token = request.cookies.get(config.COOKIE)
    with db.engine.connect() as conn:
        return from_session(conn, digest(token or ""))


def human(request: Request):
    principal = actor(request)
    if principal.run_id:
        raise BridgeError("POLICY_DENIED", "Operator console requires a human session", 403)
    return principal


def csrf(request: Request):
    origin = request.headers.get("origin")
    if not origin:
        referer = urlsplit(request.headers.get("referer", ""))
        origin = f"{referer.scheme}://{referer.netloc}"
    if origin not in config.ORIGINS:
        raise BridgeError("CSRF_REJECTED", "Request must originate from BrowserBridge", 403)
