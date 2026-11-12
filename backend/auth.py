"""
Authentication & tenant resolution for the multi-tenant API.

- Users live in the `users` table (backend/db.py); a legacy data/users.json is
  imported automatically on first access (backend/repo.py).
- Passwords are hashed with pbkdf2_sha256 (passlib, pure-python).
- Login issues a JWT (HS256) whose `sub` is the tenant_id. The token also
  carries `pv`, a fingerprint of the password hash, so changing or resetting
  the password (or deleting the account) invalidates every token issued before.
- `require_tenant` is the dependency every data endpoint depends on: it validates the
  Bearer token, sets the request-scoped tenant (backend/tenancy.py), and returns the id.
- Sign-in, registration and the one-click demo are throttled per client IP
  (backend/ratelimit.py).

Endpoints
---------
  POST /auth/register          Create an account + isolated tenant
  POST /auth/login             Exchange credentials for a token
  POST /auth/demo              Throwaway demo account (expires after 24 h)
  GET  /auth/me                Who am I
  POST /auth/change-password   Change password (returns a fresh token)
  POST /auth/delete-account    Permanently delete the account and its data

JWT_SECRET must be set in production (see backend/settings.py).
"""
import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from passlib.context import CryptContext
from pydantic import BaseModel

from backend import ratelimit, repo, settings, tenancy

router = APIRouter()
log = logging.getLogger("kol.auth")

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
_JWT_ALG = "HS256"
_TOKEN_TTL = timedelta(days=7)
_DEMO_TOKEN_TTL = timedelta(hours=24)
_DEMO_RETENTION = timedelta(hours=48)     # demo accounts are swept after this
DEMO_SUFFIX = "@demo.tiko"

# Deliberately conservative: the address is rendered in the operator console
# and pasted into payment messages, so markup characters are never accepted.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")
_PASSWORD_MIN = 8
_PASSWORD_MAX = 128        # bounds the cost of hashing attacker-supplied input

# Verified against when the email is unknown, so a miss costs the same time as
# a wrong password and can't be used to enumerate accounts.
_DUMMY_HASH = _pwd.hash(uuid.uuid4().hex)


def _pw_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def _make_token(tenant_id: str, email: str, password_hash: str,
                ttl: timedelta = _TOKEN_TTL) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": tenant_id,
        "email": email,
        "pv": _pw_fingerprint(password_hash),
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=_JWT_ALG)


def _check_password_rules(password: str):
    if len(password) < _PASSWORD_MIN:
        raise HTTPException(status_code=422,
                            detail=f"Password must be at least {_PASSWORD_MIN} characters")
    if len(password) > _PASSWORD_MAX:
        raise HTTPException(status_code=422,
                            detail=f"Password must be at most {_PASSWORD_MAX} characters")


def _sweep_expired_demos():
    """Delete demo accounts past their retention window. Called from the demo
    endpoint, so the cleanup cost is paid by the traffic that creates them."""
    cutoff = (datetime.now(timezone.utc) - _DEMO_RETENTION).isoformat()
    try:
        for u in repo.users_demo_before(DEMO_SUFFIX, cutoff):
            tid = repo.user_delete(u["email"])
            if tid:
                repo.tenant_wipe(tid)
    except Exception:                         # cleanup must never block a demo
        log.exception("demo sweep failed")


# ── Models ────────────────────────────────────────────────────────
class Credentials(BaseModel):
    email: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class AccountDeletion(BaseModel):
    password: str


# ── Endpoints ─────────────────────────────────────────────────────
@router.post("/auth/register")
def register(req: Credentials, request: Request):
    """Create a merchant account + an isolated data tenant, and return a token."""
    ratelimit.enforce("register", ratelimit.client_ip(request), limit=10, window=3600,
                      detail="Too many sign-ups from this network — please try again later.")
    email = req.email.strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email) or email.endswith(DEMO_SUFFIX):
        raise HTTPException(status_code=422, detail="Please enter a valid email address")
    _check_password_rules(req.password)
    if repo.user_find(email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    tenant_id = uuid.uuid4().hex
    password_hash = _pwd.hash(req.password)
    repo.user_create(
        email=email,
        password_hash=password_hash,
        tenant_id=tenant_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    tenancy.provision_tenant(tenant_id)
    log.info("registered account tenant=%s", tenant_id)

    return {"token": _make_token(tenant_id, email, password_hash), "email": email}


@router.post("/auth/demo")
def demo_login(request: Request):
    """One-click live demo: provision a throwaway account with its own seeded
    tenant and return a token. No registration required — used by the landing
    page "see live demo" button. Demo accounts are ordinary Free-plan tenants
    with a random unguessable password (nobody can log back in); the token
    lasts 24 hours and the account is deleted once it is 48 hours old."""
    ratelimit.enforce("demo", ratelimit.client_ip(request), limit=10, window=3600,
                      detail="Too many demo sessions from this network — "
                             "create a free account to keep going.")
    _sweep_expired_demos()

    suffix = uuid.uuid4().hex[:10]
    email = f"demo_{suffix}{DEMO_SUFFIX}"
    tenant_id = uuid.uuid4().hex
    password_hash = _pwd.hash(uuid.uuid4().hex)
    repo.user_create(
        email=email,
        password_hash=password_hash,
        tenant_id=tenant_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    tenancy.provision_tenant(tenant_id)
    return {"token": _make_token(tenant_id, email, password_hash, ttl=_DEMO_TOKEN_TTL),
            "email": email, "demo": True}


@router.post("/auth/login")
def login(req: Credentials, request: Request):
    """Verify credentials and return a token."""
    email = req.email.strip().lower()
    too_many = "Too many sign-in attempts — please wait a few minutes and try again."
    ratelimit.enforce("login-ip", ratelimit.client_ip(request), limit=20, window=300, detail=too_many)
    ratelimit.enforce("login-email", email[:254], limit=10, window=900, detail=too_many)

    user = repo.user_find(email) if len(email) <= 254 else None
    password = req.password[:_PASSWORD_MAX + 1]
    if not user:
        _pwd.verify(password, _DUMMY_HASH)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not _pwd.verify(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    # Heal any tenant that was never provisioned (or predates the database).
    if not tenancy.tenant_exists(user["tenant_id"]):
        tenancy.provision_tenant(user["tenant_id"])
    return {"token": _make_token(user["tenant_id"], user["email"], user["password_hash"]),
            "email": user["email"]}


# ── Tenant dependency (async so the contextvar propagates to sync endpoints) ──
async def require_tenant(authorization: Optional[str] = Header(None)) -> str:
    # Test/dev fallback: no token needed when DEFAULT_TENANT is set
    # (settings.validate() refuses to boot production with it).
    if tenancy.DEFAULT_TENANT and not authorization:
        tenancy.set_current_tenant(tenancy.DEFAULT_TENANT)
        return tenancy.DEFAULT_TENANT

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    tenant_id = payload.get("sub")
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    # The account must still exist and the password must be the one the token
    # was issued under — so deleting an account or changing its password
    # signs every old session out immediately.
    user = repo.user_find_by_tenant(tenant_id)
    if not user or payload.get("pv") != _pw_fingerprint(user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    tenancy.set_current_tenant(tenant_id)
    return tenant_id


@router.get("/auth/me")
def me(tenant_id: str = Depends(require_tenant)):
    user = repo.user_find_by_tenant(tenant_id)
    email = user["email"] if user else None
    return {"tenant_id": tenant_id, "email": email,
            "demo": bool(email and email.endswith(DEMO_SUFFIX))}


def _account_or_403(tenant_id: str) -> dict:
    """The signed-in user's row; demo/fallback sessions have no manageable account."""
    user = repo.user_find_by_tenant(tenant_id)
    if not user or user["email"].endswith(DEMO_SUFFIX):
        raise HTTPException(status_code=403,
                            detail="Demo sessions have no account to manage — create a free account first.")
    return user


@router.post("/auth/change-password")
def change_password(req: PasswordChange, tenant_id: str = Depends(require_tenant)):
    """Change the password. Every other session is signed out; the response
    carries a fresh token so this one stays signed in."""
    user = _account_or_403(tenant_id)
    ratelimit.enforce("pw-change", tenant_id, limit=10, window=900)
    if not _pwd.verify(req.current_password[:_PASSWORD_MAX + 1], user["password_hash"]):
        raise HTTPException(status_code=403, detail="Current password is incorrect")
    _check_password_rules(req.new_password)
    if req.new_password == req.current_password:
        raise HTTPException(status_code=422, detail="New password must be different from the current one")

    new_hash = _pwd.hash(req.new_password)
    repo.user_set_password(tenant_id, new_hash)
    log.info("password changed tenant=%s", tenant_id)
    return {"status": "ok", "token": _make_token(tenant_id, user["email"], new_hash),
            "email": user["email"]}


@router.post("/auth/delete-account")
def delete_account(req: AccountDeletion, tenant_id: str = Depends(require_tenant)):
    """Permanently delete the account and everything in its workspace
    (creator library, campaigns, calibration, usage). Not reversible."""
    user = _account_or_403(tenant_id)
    ratelimit.enforce("pw-change", tenant_id, limit=10, window=900)
    if not _pwd.verify(req.password[:_PASSWORD_MAX + 1], user["password_hash"]):
        raise HTTPException(status_code=403, detail="Password is incorrect")
    repo.user_delete(user["email"])
    repo.tenant_wipe(tenant_id)
    log.info("account deleted tenant=%s", tenant_id)
    return {"status": "deleted"}
