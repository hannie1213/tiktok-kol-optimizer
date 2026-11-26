"""
Operator console API — user management for the Lumio team.

Every endpoint requires the X-Admin-Key header matching the ADMIN_KEY
environment variable (BILLING_ADMIN_KEY is a legacy alias). With no key
configured the endpoints are inert (403), so nothing is exposed by default;
repeated wrong keys from one address are throttled.

Serves the standalone frontend at frontend/admin.html.

Endpoints
---------
  GET  /admin/overview       KPI totals: users, paid, revenue, pending payments, expiring plans
  GET  /admin/users          Per-user rows: plan + expiry, usage, library, campaigns
  POST /admin/plan           Set a user's plan, permanently or for a term of months
  GET  /admin/status         Configuration self-check (email, QR pay, copilot, launch blockers)
  GET  /admin/audit          Trail of operator actions
  POST /admin/reset-password Issue a one-time temporary password (forgot-password support)
  POST /admin/delete-user    Delete one account + wipe its tenant data
  POST /admin/cleanup-demos  Delete all @demo.lumio throwaway accounts
  GET  /admin/interests      Early-access interest registrations
"""
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from backend import mailer, ratelimit, repo, settings, tenancy
from backend.auth import DEMO_SUFFIX as _DEMO_SUFFIX, _pwd
from backend.billing import PLANS, TERMS, term_end

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
    # Length of the term in months; omit for a plan that never expires.
    months: Optional[int] = None


def _audit(request: Request, action: str, target: str = "", detail: str = ""):
    try:
        repo.audit_add(action, target, detail, ratelimit.client_ip(request))
    except Exception:
        log.exception("could not write audit entry")      # never block the action itself


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
            "email_verified": bool(u.get("email_verified")),
            "role": u.get("role", "owner"),
            "disabled": bool(u.get("disabled")),
        })
    # How many accounts share each workspace, and when it was last used.
    seats: dict = {}
    for r in rows:
        seats[r["tenant_id"]] = seats.get(r["tenant_id"], 0) + 1
    engagement = repo.admin_engagement(_since(7))
    for r in rows:
        r["members"] = seats[r["tenant_id"]]
        r["last_active"] = engagement.get(r["tenant_id"], {}).get("last_active")
    return rows


# ── Endpoints ─────────────────────────────────────────────────────
@router.get("/admin/overview")
def overview():
    month = _month()
    rows = _user_rows(month)
    demo = sum(r["demo"] for r in rows)
    # One paying customer per workspace: invited members share the owner's plan.
    paying = [r for r in rows if not r["demo"] and r["plan"] != "free" and r["role"] != "member"]
    mrr = sum(PLANS.get(r["plan"], PLANS["free"])["price_myr"] for r in paying)
    now = datetime.now(timezone.utc)
    soon = (now + timedelta(days=7)).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    real = [r for r in rows if not r["demo"]]
    return {
        # Money actually received through confirmed QR payments, per currency.
        "revenue_month": repo.orders_paid_totals(month_start),
        "revenue_total": repo.orders_paid_totals(),
        "pending_orders": repo.orders_count("submitted"),
        # Paid terms ending within 7 days — the renewal follow-up list.
        "expiring_7d": sum(1 for r in paying if r["plan_expires_at"] and r["plan_expires_at"] <= soon),
        "verified_users": sum(1 for r in real if r["email_verified"]),
        "month": month,
        "total_users": len(rows),
        "real_users": len(rows) - demo,
        "paid_users": len(paying),
        "demo_users": demo,
        "mrr_myr": mrr,
        "optimize_runs_month": sum(r["optimize_runs"] for r in rows),
        "agent_messages_month": sum(r["agent_messages"] for r in rows),
    }


@router.get("/admin/users")
def users():
    month = _month()
    return {"users": _user_rows(month), "month": month, "plans": list(PLANS.keys())}


@router.post("/admin/plan")
def set_plan(req: PlanChange, request: Request, background: BackgroundTasks):
    """Set a plan by hand (payment received another way, a comp, a fix).
    With `months` the plan runs for that term — extending the current term
    when it is a renewal; without it the plan never expires."""
    if req.plan not in PLANS:
        raise HTTPException(status_code=422, detail=f"Unknown plan: {req.plan}")
    if req.months is not None and req.months not in TERMS:
        raise HTTPException(status_code=422, detail=f"months must be one of {list(TERMS)}")
    user = repo.user_find(req.email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="No account with that email")

    before = repo.plan_info(user["tenant_id"])
    paid = PLANS[req.plan]["price_myr"] > 0
    expires_at = None
    if paid and req.months:
        # A renewal of a permanent grant is the one case term_end keeps
        # permanent; an explicit term from the operator overrides that.
        expires_at = term_end(user["tenant_id"], req.plan, req.months) or (
            datetime.now(timezone.utc) + timedelta(days=30 * req.months)).isoformat()
    repo.plan_set(user["tenant_id"], req.plan, datetime.now(timezone.utc).isoformat(),
                  expires_at=expires_at)
    _audit(request, "plan.set", user["email"],
           f"{before['plan']} -> {req.plan}, "
           + (f"{req.months}mo until {expires_at[:10]}" if expires_at else "no expiry"))

    changed = before["plan"] != req.plan or before["expires_at"] != expires_at
    if changed and not user["email"].endswith(_DEMO_SUFFIX):
        background.add_task(mailer.send_plan_changed, user["email"], PLANS[req.plan]["name"],
                            mailer.base_url(), user.get("lang", "en"),
                            expires_at[:10] if expires_at else "")
    return {"status": "ok", "email": user["email"], "plan": req.plan,
            "plan_name": PLANS[req.plan]["name"], "expires_at": expires_at}


@router.post("/admin/reset-password")
def reset_password(req: UserRef, request: Request):
    """Forgot-password support: set a random temporary password and return it
    once, for the operator to pass to the (identity-verified) merchant, who
    then changes it under Plan & Billing -> Account. Signs out all sessions."""
    user = repo.user_find(req.email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="No account with that email")
    if user["email"].endswith(_DEMO_SUFFIX):
        raise HTTPException(status_code=422, detail="Demo accounts have no password to reset")
    temp = secrets.token_urlsafe(9)
    repo.user_set_password(user["email"], _pwd.hash(temp))
    log.info("password reset by operator tenant=%s", user["tenant_id"])
    _audit(request, "user.reset_password", user["email"])
    return {"status": "ok", "email": user["email"], "temporary_password": temp}


@router.post("/admin/delete-user")
def delete_user(req: UserRef, request: Request):
    user = repo.user_find(req.email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="No account with that email")
    tenant_id = repo.user_delete(user["email"])
    # Deleting an owner removes the workspace (and its members' access);
    # deleting an invited member must leave the owner's data alone.
    if user["role"] != "member":
        repo.tenant_wipe(tenant_id)
    _audit(request, "user.delete", user["email"], f"tenant={tenant_id} role={user['role']}")
    return {"status": "ok", "email": req.email.strip().lower()}


@router.post("/admin/cleanup-demos")
def cleanup_demos(request: Request):
    removed = 0
    for u in repo.users_all():
        if u["email"].endswith(_DEMO_SUFFIX):
            tid = repo.user_delete(u["email"])
            if tid:
                repo.tenant_wipe(tid)
                removed += 1
    if removed:
        _audit(request, "demo.cleanup", "", f"removed={removed}")
    return {"status": "ok", "removed": removed}


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@router.get("/admin/growth")
def growth():
    """Sign-ups per day, the activation funnel and feature adoption — the
    questions "is anyone coming, and do they get to value?"."""
    month = _month()
    rows = [r for r in _user_rows(month) if not r["demo"]]
    owners = [r for r in rows if r["role"] != "member"]
    eng = repo.admin_engagement(_since(7))
    e = lambda r: eng.get(r["tenant_id"], {})          # noqa: E731

    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    per_day = {d: 0 for d in days}
    for r in rows:
        d = (r["created_at"] or "")[:10]
        if d in per_day:
            per_day[d] += 1

    funnel = [
        ("registered", len(owners)),
        ("verified", sum(1 for r in owners if r["email_verified"])),
        ("optimized", sum(1 for r in owners if e(r).get("ever_optimized") or r["campaigns"] > 0)),
        ("campaign", sum(1 for r in owners if r["campaigns"] > 0)),
        ("outreach", sum(1 for r in owners if e(r).get("contacted") or e(r).get("messages"))),
        ("paid", sum(1 for r in owners if r["plan"] != "free")),
    ]
    return {
        "signups": [{"date": d, "count": per_day[d]} for d in days],
        "signups_7d": sum(per_day[d] for d in days[-7:]),
        "signups_prev_7d": sum(per_day[d] for d in days[:7]),
        "funnel": [{"step": s, "count": n} for s, n in funnel],
        "active_7d": sum(1 for r in owners if e(r).get("active_recent")),
        "workspaces": len(owners),
        "team_workspaces": sum(1 for r in owners if r["members"] > 1),
        "members": sum(1 for r in rows if r["role"] == "member"),
        "adoption": {
            "messages": sum(e(r).get("messages", 0) for r in owners),
            "contracts": sum(e(r).get("contracts", 0) for r in owners),
            "campaigns": sum(r["campaigns"] for r in owners),
            "ai_chats_month": sum(r["agent_messages"] for r in owners),
        },
    }


@router.get("/admin/workspace")
def workspace(email: str):
    """Everything about one account's workspace on a single screen — the view
    to open when a merchant writes in. Read-only: no creator or campaign
    content, only counts, billing and the activity trail."""
    from backend import collab, pipeline        # deferred: both import auth, which this module precedes
    user = repo.user_find(email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="No account with that email")
    tid = user["tenant_id"]
    plan = repo.plan_info(tid)
    limits = PLANS.get(plan["plan"], PLANS["free"])
    month = _month()
    stages = {s: 0 for s in pipeline.STAGES}
    campaigns = repo.campaigns_all(tid)
    for c in campaigns:
        for k in c.get("selected_kols") or []:
            st = ((c.get("pipeline") or {}).get(str(k.get("id"))) or {}).get("status", "shortlisted")
            stages[st] = stages.get(st, 0) + 1
    return {
        "email": user["email"], "role": user["role"], "tenant_id": tid,
        "created_at": user["created_at"], "email_verified": user["email_verified"], "lang": user["lang"],
        "members": [{"email": u["email"], "role": u["role"], "joined_at": u["created_at"],
                     "email_verified": u["email_verified"]} for u in repo.users_for_tenant(tid)],
        "pending_invites": [i["email"] for i in repo.invites_pending(tid)],
        "plan": plan["plan"], "plan_name": limits["name"], "plan_expires_at": plan["expires_at"],
        "seats": limits["seats"],
        "usage": {
            "month": month,
            "optimize_runs": repo.optimize_runs(tid, month), "optimize_limit": limits["monthly_optimizations"],
            "agent_messages": repo.agent_messages(tid, month), "agent_limit": limits["monthly_agent_messages"],
        },
        "counts": {
            "creators": repo.kols_count(tid), "campaigns": len(campaigns),
            "completed_campaigns": sum(1 for c in campaigns if c.get("actual_total_gmv") is not None),
            "messages": repo.docs_count(tid, "message"), "contracts": repo.docs_count(tid, "contract"),
        },
        "pipeline": stages,
        "onboarding": collab.onboarding(tid),
        "orders": [{k: o[k] for k in ("order_no", "plan", "months", "amount", "currency", "status",
                                      "reference", "created_at", "decided_at")}
                   for o in repo.orders_for_tenant(tid, limit=10)],
        "activity": repo.docs_list(tid, "activity", limit=25),
    }


class Suspend(BaseModel):
    email: str
    suspended: bool = True


@router.post("/admin/suspend")
def suspend_user(req: Suspend, request: Request):
    """Lock an account out (or let it back in) without touching its data.
    A suspended account cannot sign in, use an existing session, or capture
    creators with the extension."""
    user = repo.user_find(req.email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="No account with that email")
    repo.user_update(user["email"], disabled=bool(req.suspended))
    _audit(request, "user.suspend" if req.suspended else "user.unsuspend", user["email"])
    return {"status": "ok", "email": user["email"], "disabled": bool(req.suspended)}


class Announcement(BaseModel):
    text: str = Field("", max_length=300)
    text_zh: str = Field("", max_length=300)
    level: str = Field("info", max_length=8)


@router.get("/admin/announcement")
def get_announcement():
    return {"announcement": repo.system_get("announcement")}


@router.post("/admin/announcement")
def set_announcement(req: Announcement, request: Request):
    """Site-wide notice shown at the top of every merchant's console. Empty
    text in both languages removes it."""
    if req.level not in ("info", "warn"):
        raise HTTPException(status_code=422, detail="level must be info or warn")
    text, text_zh = req.text.strip(), req.text_zh.strip()
    if not text and not text_zh:
        repo.system_set("announcement", None)
        _audit(request, "announcement.clear")
        return {"status": "ok", "announcement": None}
    repo.system_set("announcement", {"text": text, "text_zh": text_zh, "level": req.level},
                    datetime.now(timezone.utc).isoformat())
    _audit(request, "announcement.set", "", (text or text_zh)[:120])
    return {"status": "ok", "announcement": repo.system_get("announcement")}


@router.get("/admin/pool")
def pool_list(q: str = "", status: str = "", limit: int = 100):
    """The shared Explore pool, newest capture first — every workspace can
    write to it, so the operator needs to be able to look, approve and prune.
    `status` filters to pending | public | hidden."""
    from sqlalchemy import func, or_, select
    from backend.db import PoolCreator, SessionLocal
    from backend.pool import _to_dict
    stmt = select(PoolCreator)
    if status in _POOL_STATUSES:
        stmt = stmt.where(PoolCreator.status == status)
    if q.strip():
        like = "%" + q.strip().lower().lstrip("@") + "%"
        stmt = stmt.where(or_(PoolCreator.handle_key.like(like), func.lower(PoolCreator.name).like(like)))
    with SessionLocal() as s:
        total = s.scalar(select(func.count()).select_from(PoolCreator)) or 0
        counts = dict(s.execute(select(PoolCreator.status, func.count()).group_by(PoolCreator.status)).all())
        rows = s.scalars(stmt.order_by(PoolCreator.last_seen.desc()).limit(max(1, min(500, limit)))).all()
        items = []
        for r in rows:
            d = _to_dict(r)
            d["confirmations"] = r.confirmations or 0
            items.append(d)
        return {"total": total, "counts": {k: counts.get(k, 0) for k in _POOL_STATUSES}, "items": items}


_POOL_STATUSES = ("pending", "public", "hidden")


class PoolStatus(BaseModel):
    status: str


@router.post("/admin/pool/{creator_id}/status")
def pool_set_status(creator_id: int, req: PoolStatus, request: Request):
    """Approve (public), hide, or send a pool creator back to pending."""
    from backend.db import PoolCreator, SessionLocal
    if req.status not in _POOL_STATUSES:
        raise HTTPException(status_code=422, detail="status must be pending, public or hidden")
    with SessionLocal.begin() as s:
        row = s.get(PoolCreator, creator_id)
        if not row:
            raise HTTPException(status_code=404, detail="Creator not found")
        row.status = req.status
        handle = row.handle
    _audit(request, "pool." + req.status, "@" + handle)
    return {"status": "ok"}


@router.delete("/admin/pool/{creator_id}")
def pool_delete(creator_id: int, request: Request):
    from sqlalchemy import delete
    from backend.db import PoolCapture, PoolCreator, SessionLocal
    with SessionLocal.begin() as s:
        row = s.get(PoolCreator, creator_id)
        if not row:
            raise HTTPException(status_code=404, detail="Creator not found")
        handle = row.handle
        s.execute(delete(PoolCapture).where(PoolCapture.creator_id == creator_id))
        s.delete(row)
    _audit(request, "pool.delete", "@" + handle)
    return {"status": "ok"}


@router.post("/admin/jobs/run")
def run_jobs(request: Request):
    """Run the hourly housekeeping now (renewal reminders, stale orders, demo sweep)."""
    from backend import jobs
    result = jobs.run_once()
    _audit(request, "jobs.run", "", ", ".join(f"{k}={v}" for k, v in result.items()))
    return {"status": "ok", "result": result}


@router.get("/admin/audit")
def audit(limit: int = 100):
    return {"entries": repo.audit_list(max(1, min(500, limit)))}


@router.get("/admin/status")
def status():
    """What is switched on, and what still blocks a public launch. Shows
    whether each secret is SET, never its value."""
    from backend import aivideo, jobs, payments    # deferred: payments imports this module
    from backend.db import engine
    copilot_key = bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("AGENT_API_KEY"))
    return {
        "app_env": settings.APP_ENV,
        "production": settings.IS_PRODUCTION,
        # Reasons this config must not face the public yet (empty = ready).
        "launch_blockers": settings.production_problems(),
        "database": engine.url.get_backend_name(),
        "email": {
            "enabled": mailer.enabled(),
            "from": mailer.SMTP_FROM or None,
            "base_url": mailer.APP_BASE_URL or None,
            "operator_email_set": bool(settings.OPERATOR_EMAIL),
        },
        "payments": {
            "enabled": payments.enabled(),
            "payee": settings.PAY_PAYEE_NAME or None,
            "currency": settings.PAY_CURRENCY,
            "prices": {pid: payments.list_price(pid, 1)
                       for pid, p in PLANS.items() if p["price_myr"] > 0},
        },
        "copilot": {"enabled": copilot_key,
                    "model": os.environ.get("AGENT_MODEL", "deepseek-chat")},
        "rate_limiting": settings.RATE_LIMIT_ENABLED,
        "jobs": jobs.status(),
        "ai_video": {"enabled": aivideo.enabled(), "model": aivideo.MODEL if aivideo.enabled() else None,
                     "resolution": aivideo.RESOLUTION},
        "billing_self_serve": settings.BILLING_SELF_SERVE,
    }


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
