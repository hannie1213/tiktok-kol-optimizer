"""
Operator console API — user management for the tiko team.

Every endpoint requires the X-Admin-Key header matching the ADMIN_KEY
environment variable (BILLING_ADMIN_KEY is a legacy alias). With no key
configured the endpoints are inert (403), so nothing is exposed by default;
repeated wrong keys from one address are throttled.

Serves the standalone frontend at frontend/admin.html.

Endpoints
---------
  GET  /admin/overview       KPI totals (users, paid, demo, runs this month)
  GET  /admin/users          Per-user rows: plan, usage, library, campaigns
  POST /admin/plan           Set a user's plan (offline-payment fulfillment)
  POST /admin/reset-password Issue a one-time temporary password (forgot-password support)
  POST /admin/delete-user    Delete one account + wipe its tenant data
  POST /admin/cleanup-demos  Delete all @demo.tiko throwaway accounts
  GET  /admin/interests      Early-access interest registrations
"""
import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from backend import mailer, ratelimit, repo, settings, tenancy
from backend.auth import DEMO_SUFFIX as _DEMO_SUFFIX, _pwd
from backend.billing import PLANS

log = logging.getLogger("kol.admin")

# Legacy JSON lead list from before interests moved into the database; still
# shown (read-only) so nothing collected earlier is lost.
INTERESTS_PATH = os.path.join(tenancy.DATA_DIR, "api_interests.json")


def _require_admin(request: Request, x_admin_key: Optional[str] = Header(None)) -> None:
    if settings.admin_key_ok(x_admin_key):
        return
    # Only failures are counted, so the console itself is never throttled.
    ratelimit.enforce("admin-bad-key", ratelimit.client_ip(request), limit=10, window=900,
                      detail="Too many failed admin-key attempts — try again later.")
    raise HTTPException(status_code=403, detail="Admin key required")


router = APIRouter(dependencies=[Depends(_require_admin)])


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def record_interest(email: str, integration_type: str) -> None:
    """Store an early-access interest registration (called from main.py)."""
    try:
        repo.interest_add(email, integration_type, datetime.now(timezone.utc).isoformat())
    except Exception:
        log.exception("could not record interest")   # must never break the request


# ── Models ────────────────────────────────────────────────────────
class PlanChange(BaseModel):
    email: str
    plan: str


class UserRef(BaseModel):
    email: str


def _user_rows(month: str) -> list:
    """Join the user list with the batched per-tenant stats (no per-user
    queries — see repo.admin_stats)."""
    stats = repo.admin_stats(month)
    rows = []
    for u in repo.users_all():
        st = stats.get(u["tenant_id"], {})
        rows.append({
            "email": u["email"],
            "tenant_id": u["tenant_id"],
            "created_at": u["created_at"],
            "plan": st.get("plan", "free"),
            "plan_expires_at": st.get("plan_expires_at"),
            "optimize_runs": st.get("optimize_runs", 0),
            "agent_messages": st.get("agent_messages", 0),
            "creators": st.get("creators", 0),
            "campaigns": st.get("campaigns", 0),
            "demo": u["email"].endswith(_DEMO_SUFFIX),
        })
    return rows


# ── Endpoints ─────────────────────────────────────────────────────
@router.get("/admin/overview")
def overview():
    month = _month()
    rows = _user_rows(month)
    demo = sum(r["demo"] for r in rows)
    paying = [r for r in rows if not r["demo"] and r["plan"] != "free"]
    mrr = sum(PLANS.get(r["plan"], PLANS["free"])["price_usd"] for r in paying)
    return {
        "month": month,
        "total_users": len(rows),
        "real_users": len(rows) - demo,
        "paid_users": len(paying),
        "demo_users": demo,
        "mrr_usd": mrr,
        "optimize_runs_month": sum(r["optimize_runs"] for r in rows),
        "agent_messages_month": sum(r["agent_messages"] for r in rows),
    }


@router.get("/admin/users")
def users():
    month = _month()
    return {"users": _user_rows(month), "month": month, "plans": list(PLANS.keys())}


@router.post("/admin/plan")
def set_plan(req: PlanChange, background: BackgroundTasks):
    if req.plan not in PLANS:
        raise HTTPException(status_code=422, detail=f"Unknown plan: {req.plan}")
    user = repo.user_find(req.email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="No account with that email")
    changed = repo.plan_get(user["tenant_id"]) != req.plan
    repo.plan_set(user["tenant_id"], req.plan, datetime.now(timezone.utc).isoformat())
    if changed and not user["email"].endswith(_DEMO_SUFFIX):
        background.add_task(mailer.send_plan_changed, user["email"], PLANS[req.plan]["name"],
                            mailer.base_url(), user.get("lang", "en"))
    return {"status": "ok", "email": user["email"], "plan": req.plan,
            "plan_name": PLANS[req.plan]["name"]}


@router.post("/admin/reset-password")
def reset_password(req: UserRef):
    """Forgot-password support: set a random temporary password and return it
    once, for the operator to pass to the (identity-verified) merchant, who
    then changes it under Plan & Billing -> Account. Signs out all sessions."""
    user = repo.user_find(req.email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="No account with that email")
    if user["email"].endswith(_DEMO_SUFFIX):
        raise HTTPException(status_code=422, detail="Demo accounts have no password to reset")
    temp = secrets.token_urlsafe(9)
    repo.user_set_password(user["tenant_id"], _pwd.hash(temp))
    log.info("password reset by operator tenant=%s", user["tenant_id"])
    return {"status": "ok", "email": user["email"], "temporary_password": temp}


@router.post("/admin/delete-user")
def delete_user(req: UserRef):
    tenant_id = repo.user_delete(req.email)
    if tenant_id is None:
        raise HTTPException(status_code=404, detail="No account with that email")
    repo.tenant_wipe(tenant_id)
    return {"status": "ok", "email": req.email.strip().lower()}


@router.post("/admin/cleanup-demos")
def cleanup_demos():
    removed = 0
    for u in repo.users_all():
        if u["email"].endswith(_DEMO_SUFFIX):
            tid = repo.user_delete(u["email"])
            if tid:
                repo.tenant_wipe(tid)
                removed += 1
    return {"status": "ok", "removed": removed}


@router.get("/admin/interests")
def interests():
    legacy = []
    if os.path.exists(INTERESTS_PATH):
        try:
            with open(INTERESTS_PATH, "r", encoding="utf-8") as f:
                legacy = json.load(f)
        except (OSError, json.JSONDecodeError):
            legacy = []
    return {"interests": repo.interests_all() + list(reversed(legacy))}
