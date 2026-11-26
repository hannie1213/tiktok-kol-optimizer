"""
Subscription plans, metered usage, and quota enforcement.

Tiers follow the commercialization roadmap (docs/saas-architecture.md). The
product has two metered value axes, each with a real marginal cost:

  1. Optimization runs — the core engine. Free gives 10 runs/month to hook new
     merchants, Pro raises the quota, Team removes it.
  2. AI Copilot chats — the LLM campaign assistant (backend/agent.py). Every
     chat turn calls an LLM, so it is metered too: Free gets a 20-chat taste,
     Pro a working allowance, Team unlimited.

Both quotas are enforced server-side and surfaced with HTTP 402 so the frontend
can route the merchant to the upgrade page.

Payments: paid plans are never self-activated. A merchant pays offline
(WhatsApp / WeChat) and the operator activates the plan from the console
(POST /admin/plan) or POST /billing/grant. POST /billing/checkout therefore
only performs moves that need no payment — i.e. downgrading to Free — and
answers 402 for a paid plan. It is also the integration point for a card
provider: create a Stripe Checkout session there and apply the plan in the
payment-succeeded webhook. (BILLING_SELF_SERVE=1 restores instant switching
for local demos; production refuses to boot with it on.)

Endpoints
---------
  GET  /billing/info       Current plan, limits, this month's usage, catalog
  POST /billing/checkout   Downgrade to Free (paid plans -> 402, see above)
  POST /billing/grant      Operator: activate a plan after offline payment
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from backend import mailer, ratelimit, repo, settings
from backend.auth import require_owner, require_tenant

router = APIRouter()

# Plan catalog. None = unlimited. Prices in MYR (Malaysian ringgit) / month —
# the subscription is sold to the SEA seller community and paid by DuitNow QR.
# (Optimizer budgets and GMV stay in USD; only subscription prices are MYR.)
# Tiers differentiate on the two metered axes — optimization runs and AI Copilot
# chats — both of which carry a real marginal cost. The creator library is
# unlimited on every plan (max_creators stays in the schema so a cap could
# return, and check_creator_capacity below no-ops while it is None).
PLANS = {
    "free": {
        "name": "Free",
        "price_myr": 0,
        "max_creators": None,
        "monthly_optimizations": 10,
        "monthly_agent_messages": 20,
        "monthly_ai_videos": 1,
        "seats": 1,
        "features": [
            "10 optimization runs / month",
            "AI Copilot — 20 chats / month",
            "1 AI video clip / month",
            "Smart lineup picks + 'why this creator'",
            "Unlimited creator library",
            "CSV / Excel import & export",
        ],
    },
    "pro": {
        "name": "Pro",
        "price_myr": 29,
        "max_creators": None,
        "monthly_optimizations": 300,
        "monthly_agent_messages": 500,
        "monthly_ai_videos": 20,
        "seats": 3,
        "features": [
            "300 optimization runs / month",
            "AI Copilot — 500 chats / month",
            "20 AI video clips / month",
            "Self-calibrating GMV predictions",
            "Campaign attribution & accuracy tracking",
            "Unlimited creator library",
            "3 team seats",
        ],
    },
    "team": {
        "name": "Team",
        "price_myr": 99,
        "max_creators": None,
        "monthly_optimizations": None,
        "monthly_agent_messages": None,
        "monthly_ai_videos": 60,          # each clip costs real money, so never unlimited
        "seats": 10,
        "features": [
            "Unlimited optimization runs",
            "Unlimited AI Copilot",
            "60 AI video clips / month",
            "10 team seats",
            "Priority support",
        ],
    },
}


TERMS = (1, 3, 12)          # months a paid term can be bought / granted for
DAYS_PER_MONTH = 30


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def term_end(tenant_id: str, plan: str, months: int) -> Optional[str]:
    """End (UTC ISO) of a `months`-long term of `plan` starting now. Renewing
    the plan already active extends from its current end; a permanent grant
    of that same plan stays permanent (returns None)."""
    current = repo.plan_info(tenant_id)
    start = datetime.now(timezone.utc)
    if current["plan"] == plan:
        if current["expires_at"] is None:
            return None
        start = max(start, datetime.fromisoformat(current["expires_at"]))
    return (start + timedelta(days=DAYS_PER_MONTH * months)).isoformat()


def plan_limits(tenant_id: str) -> dict:
    return PLANS.get(repo.plan_get(tenant_id), PLANS["free"])


# ── Enforcement helpers (used by /optimize and creator writes) ─────
def check_and_consume_optimize(tenant_id: str):
    """Meter one optimization run; 402 when the month's quota is spent."""
    limit = plan_limits(tenant_id)["monthly_optimizations"]
    if limit is not None and repo.optimize_runs(tenant_id, _month()) >= limit:
        raise HTTPException(
            status_code=402,
            detail=f"Monthly optimization limit reached ({limit} runs on your plan). "
                   "Upgrade for more optimization runs.",
        )
    repo.optimize_consume(tenant_id, _month())


def check_and_consume_agent(tenant_id: str):
    """Meter one AI Copilot chat turn; 402 when the month's quota is spent."""
    limit = plan_limits(tenant_id)["monthly_agent_messages"]
    if limit is not None and repo.agent_messages(tenant_id, _month()) >= limit:
        raise HTTPException(
            status_code=402,
            detail=f"Monthly AI Copilot limit reached ({limit} chats on your plan). "
                   "Upgrade for more copilot conversations.",
        )
    repo.agent_consume(tenant_id, _month())


def check_creator_capacity(tenant_id: str, adding: int = 1):
    """402 when adding `adding` creators would exceed the plan's library cap."""
    cap = plan_limits(tenant_id)["max_creators"]
    if cap is None:
        return
    current = repo.kols_count(tenant_id)
    if current + adding > cap:
        raise HTTPException(
            status_code=402,
            detail=f"Creator library limit reached ({current}/{cap} on your plan"
                   f"{f', adding {adding}' if adding > 1 else ''}). "
                   "Upgrade for a larger library.",
        )


# ── Models & endpoints ─────────────────────────────────────────────
class CheckoutRequest(BaseModel):
    plan: str


@router.get("/billing/info")
def billing_info(tenant_id: str = Depends(require_tenant)):
    """Current plan + this month's usage against its limits, and the catalog."""
    current = repo.plan_info(tenant_id)
    plan_id = current["plan"]
    limits = PLANS.get(plan_id, PLANS["free"])
    return {
        "plan": plan_id,
        "plan_name": limits["name"],
        # End of the paid period (UTC ISO); null = free plan or no expiry.
        "expires_at": current["expires_at"],
        "limits": {
            "max_creators": limits["max_creators"],
            "monthly_optimizations": limits["monthly_optimizations"],
            "monthly_agent_messages": limits["monthly_agent_messages"],
            "seats": limits["seats"],
        },
        "usage": {
            "creators": repo.kols_count(tenant_id),
            "optimize_runs": repo.optimize_runs(tenant_id, _month()),
            "agent_messages": repo.agent_messages(tenant_id, _month()),
            "month": _month(),
        },
        "plans": PLANS,
        # False -> the UI must send paid upgrades to the offline-payment flow.
        "self_serve": settings.BILLING_SELF_SERVE,
    }


@router.post("/billing/checkout")
def checkout(req: CheckoutRequest, tenant_id: str = Depends(require_owner)):
    """Switch the tenant's plan — only moves that need no payment.

    A paid plan answers 402: it is activated by the operator after payment
    (POST /admin/plan or /billing/grant). Real-payment integration point:
    create a Stripe Checkout session here and apply the plan in the
    payment-succeeded webhook.
    """
    if req.plan not in PLANS:
        raise HTTPException(status_code=422, detail=f"Unknown plan: {req.plan}")
    current = repo.plan_get(tenant_id)
    if req.plan == current:
        raise HTTPException(status_code=409, detail="You are already on this plan")
    if PLANS[req.plan]["price_myr"] > 0 and not settings.BILLING_SELF_SERVE:
        raise HTTPException(
            status_code=402,
            detail=f"The {PLANS[req.plan]['name']} plan is activated after payment — "
                   "contact us to upgrade and we will switch your plan.",
        )

    # Block a downgrade that would strand the library over the new cap.
    new_cap = PLANS[req.plan]["max_creators"]
    if new_cap is not None:
        creators = repo.kols_count(tenant_id)
        if creators > new_cap:
            raise HTTPException(
                status_code=409,
                detail=f"Your library has {creators} creators but the "
                       f"{PLANS[req.plan]['name']} plan allows {new_cap}. "
                       "Remove creators first or pick a bigger plan.",
            )

    repo.plan_set(tenant_id, req.plan, datetime.now(timezone.utc).isoformat())
    return {
        "status": "ok",
        "plan": req.plan,
        "plan_name": PLANS[req.plan]["name"],
        "message": f"You're now on the {PLANS[req.plan]['name']} plan",
    }


class GrantRequest(BaseModel):
    email: str
    plan: str


@router.post("/billing/grant")
def grant_plan(req: GrantRequest, request: Request, background: BackgroundTasks, x_admin_key: Optional[str] = Header(None)):
    """Manual-payment fulfillment (offline WhatsApp / WeChat payments).

    Once payment is confirmed, the operator activates the merchant's plan:

        curl -X POST http://localhost:8000/billing/grant \\
             -H "X-Admin-Key: $ADMIN_KEY" \\
             -H "Content-Type: application/json" \\
             -d '{"email": "merchant@shop.com", "plan": "pro"}'

    Requires the ADMIN_KEY environment variable (BILLING_ADMIN_KEY is a legacy
    alias); returns 403 when the key is unset or wrong so the endpoint is
    inert by default.
    """
    if not settings.admin_key_ok(x_admin_key):
        ratelimit.enforce("admin-bad-key", ratelimit.client_ip(request), limit=10, window=900)
        raise HTTPException(status_code=403, detail="Admin key required")
    if req.plan not in PLANS:
        raise HTTPException(status_code=422, detail=f"Unknown plan: {req.plan}")
    user = repo.user_find(req.email.strip().lower())
    if not user:
        raise HTTPException(status_code=404, detail="No account with that email")
    repo.plan_set(user["tenant_id"], req.plan, datetime.now(timezone.utc).isoformat())
    repo.audit_add("plan.grant", user["email"], f"plan={req.plan} (api, no expiry)",
                   ratelimit.client_ip(request))
    background.add_task(mailer.send_plan_changed, user["email"], PLANS[req.plan]["name"],
                        mailer.base_url(), user.get("lang", "en"))
    return {
        "status": "ok",
        "email": user["email"],
        "plan": req.plan,
        "plan_name": PLANS[req.plan]["name"],
        "message": f"{user['email']} is now on the {PLANS[req.plan]['name']} plan",
    }
