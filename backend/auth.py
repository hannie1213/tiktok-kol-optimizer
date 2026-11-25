"""
Authentication & tenant resolution for the multi-tenant API.

- Users live in the SQLite `users` table (backend/db.py).
- Passwords are hashed with pbkdf2_sha256 (passlib, pure-python).
- Login issues a JWT (HS256) whose `sub` is the tenant_id. Changing or resetting a
  password revokes every token issued before the change.
- Login / register / password-reset endpoints are rate limited per client IP.
- `require_tenant` is the dependency every data endpoint depends on: it validates the
  Bearer token, sets the request-scoped tenant (backend/tenancy.py), and returns the id.

Set APP_ENV=production and a strong JWT_SECRET when deployed (see backend/config.py).
"""
import hashlib
import logging
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from passlib.context import CryptContext
from pydantic import BaseModel

from backend import config, db, mailer, tenancy
from backend.ratelimit import limit_auth

router = APIRouter()
log = logging.getLogger("kol.auth")

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
_JWT_ALG = "HS256"
_TOKEN_TTL = timedelta(days=7)
_RESET_TTL = timedelta(hours=1)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8


# ── User store ────────────────────────────────────────────────────
def _find_user(email: str) -> Optional[dict]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    return dict(row) if row else None


def _user_by_tenant(tenant_id: str) -> Optional[dict]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE tenant_id=?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def _check_password_rules(password: str):
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=422, detail=f"Password must be at least {MIN_PASSWORD_LEN} characters")
    if password.isdigit() or password.isalpha():
        raise HTTPException(status_code=422, detail="Password must contain both letters and numbers")


def _set_password(conn: sqlite3.Connection, user_id: int, password: str):
    # Tokens carry whole-second `iat`; anything issued before this second is revoked.
    conn.execute(
        "UPDATE users SET password_hash=?, password_changed_at=? WHERE id=?",
        (_pwd.hash(password), int(datetime.now(timezone.utc).timestamp()), user_id),
    )


def _make_token(tenant_id: str, email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": tenant_id, "email": email, "iat": now, "exp": now + _TOKEN_TTL}
    return jwt.encode(payload, config.JWT_SECRET, algorithm=_JWT_ALG)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── Models ────────────────────────────────────────────────────────
class Credentials(BaseModel):
    email: str
    password: str


class ForgotPassword(BaseModel):
    email: str


class ResetPassword(BaseModel):
    token: str
    password: str


class ChangePassword(BaseModel):
    current_password: str
    new_password: str


# ── Endpoints ─────────────────────────────────────────────────────
@router.post("/auth/register", dependencies=[Depends(limit_auth)])
def register(req: Credentials):
    """Create a merchant account + an isolated data tenant, and return a token."""
    email = req.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="Please enter a valid email address")
    _check_password_rules(req.password)

    tenant_id = uuid.uuid4().hex
    try:
        with db.connect() as conn, db.tx(conn):
            conn.execute(
                "INSERT INTO users (email, password_hash, tenant_id, created_at) VALUES (?,?,?,?)",
                (email, _pwd.hash(req.password), tenant_id, db.now_iso()),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    tenancy.provision_tenant(tenant_id)

    return {"token": _make_token(tenant_id, email), "email": email}


@router.post("/auth/login", dependencies=[Depends(limit_auth)])
def login(req: Credentials):
    """Verify credentials and return a token."""
    user = _find_user(req.email)
    if not user or not _pwd.verify(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    # Heal any tenant whose data went missing.
    if not tenancy.tenant_exists(user["tenant_id"]):
        tenancy.provision_tenant(user["tenant_id"])
    return {"token": _make_token(user["tenant_id"], user["email"]), "email": user["email"]}


@router.post("/auth/forgot-password", dependencies=[Depends(limit_auth)])
def forgot_password(req: ForgotPassword):
    """Email a one-hour reset link. Always answers the same, so it can't probe accounts."""
    user = _find_user(req.email)
    if user:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + _RESET_TTL
        with db.connect() as conn, db.tx(conn):
            conn.execute(
                "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?,?,?)",
                (_hash_token(token), user["id"], expires.isoformat()),
            )
        link = f"{config.PUBLIC_BASE_URL}/?reset_token={token}"
        try:
            mailer.send(
                user["email"],
                "Reset your KOL Matrix Optimizer password",
                "We received a request to reset your password.\n\n"
                f"Open this link within 1 hour to choose a new one:\n{link}\n\n"
                "If you didn't ask for this, you can ignore this email.",
            )
        except Exception:  # never reveal mail failures to the caller
            log.exception("Failed to send password reset email")
    return {"message": "If that email has an account, a reset link has been sent."}


@router.post("/auth/reset-password", dependencies=[Depends(limit_auth)])
def reset_password(req: ResetPassword):
    _check_password_rules(req.password)
    with db.connect() as conn, db.tx(conn):
        row = conn.execute(
            "SELECT user_id, expires_at, used FROM password_resets WHERE token_hash=?",
            (_hash_token(req.token),),
        ).fetchone()
        if (not row or row["used"]
                or datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc)):
            raise HTTPException(status_code=400, detail="This reset link is invalid or has expired")
        conn.execute("UPDATE password_resets SET used=1 WHERE user_id=?", (row["user_id"],))
        _set_password(conn, row["user_id"], req.password)
    return {"message": "Password updated. Please sign in with your new password."}


# ── Tenant dependency (async so the contextvar propagates to sync endpoints) ──
async def require_tenant(authorization: Optional[str] = Header(None)) -> str:
    # Test/dev fallback: no token needed when DEFAULT_TENANT is set (refused in production).
    if tenancy.DEFAULT_TENANT and not authorization:
        tenancy.set_current_tenant(tenancy.DEFAULT_TENANT)
        return tenancy.DEFAULT_TENANT

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    tenant_id = payload.get("sub")
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = _user_by_tenant(tenant_id)
    if not user or int(payload.get("iat", 0)) < user["password_changed_at"]:
        raise HTTPException(status_code=401, detail="Session expired, please sign in again")
    tenancy.set_current_tenant(tenant_id)
    return tenant_id


@router.get("/auth/me")
def me(tenant_id: str = Depends(require_tenant)):
    user = _user_by_tenant(tenant_id)
    return {
        "tenant_id": tenant_id,
        "email": user["email"] if user else None,
        "plan": user["plan"] if user else "free",
    }


@router.post("/auth/change-password", dependencies=[Depends(limit_auth)])
def change_password(req: ChangePassword, tenant_id: str = Depends(require_tenant)):
    user = _user_by_tenant(tenant_id)
    if not user or not _pwd.verify(req.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    _check_password_rules(req.new_password)
    with db.connect() as conn, db.tx(conn):
        _set_password(conn, user["id"], req.new_password)
    return {"token": _make_token(tenant_id, user["email"]), "email": user["email"]}
