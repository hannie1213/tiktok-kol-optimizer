"""
Subscription plans, metered usage, and quota enforcement.

Tiers follow the commercialization roadmap (docs/saas-architecture.md):
optimization runs are the product's core value and therefore the metered
axis — Free gives 10 runs/month to hook new merchants, Pro raises the
quota, Team removes it and adds seats/API. Quotas are enforced
server-side and surfaced with HTTP 402 so the frontend can route the
merchant to the upgrade page.

Payment provider note: POST /billing/checkout switches the plan directly —
it is the integration point where a real deployment creates a Stripe
Checkout session (and a webhook confirms the switch). Everything else
(catalog, usage metering, enforcement) is production logic.

Endpoints
---------
  GET  /billing/info       Current plan, limits, this month's usage, catalog
  POST /billing/checkout   Switch plan (demo checkout — see note above)
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend import repo, tenancy
from backend.auth import require_tenant

router = APIRouter()

# Plan catalog. None = unlimited. Prices in USD/month.
# The product's value is the optimization engine, so optimization runs are
# the metered axis tiers differentiate on. The creator library is unlimited
# on every plan (max_creators stays in the schema so a cap could return, and
# check_creator_capacity below no-ops while it is None).
PLANS = {
    "free": {
        "name": "Free",
        "price_usd": 0,
        "max_creators": None,
        "monthly_optimizations": 10,
        "seats": 1,
        "features": [
            "10 optimization runs / month",
            "All 6 algorithms + explainable picks",
            "Unlimited creator library",
            "CSV / Excel import & export",
        ],
    },
    "pro": {
        "name": "Pro",
        "price_usd": 49,
        "max_creators": None,
        "monthly_optimizations": 300,
        "seats": 3,
        "features": [
            "300 optimization runs / month",
            "Self-calibrating GMV predictions",
            "Campaign attribution & accuracy tracking",
            "Unlimited creator library",
            "3 team seats",
        ],
    },
    "team": {
        "name": "Team",
        "price_usd": 199,
        "max_creators": None,
        "monthly_optimizations": None,
        "seats": 10,
        "features": [
            "Unlimited optimization runs",
            "10 team seats & multi-store support",
            "API access",
            "Unlimited creator library",
            "Priority support",
        ],
    },
}


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


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
    plan_id = repo.plan_get(tenant_id)
    limits = PLANS.get(plan_id, PLANS["free"])
    return {
        "plan": plan_id,
        "plan_name": limits["name"],
        "limits": {
            "max_creators": limits["max_creators"],
            "monthly_optimizations": limits["monthly_optimizations"],
            "seats": limits["seats"],
        },
        "usage": {
            "creators": repo.kols_count(tenant_id),
            "optimize_runs": repo.optimize_runs(tenant_id, _month()),
            "month": _month(),
        },
        "plans": PLANS,
    }


@router.post("/billing/checkout")
def checkout(req: CheckoutRequest, tenant_id: str = Depends(require_tenant)):
    """Switch the tenant's plan.

    Real-payment integration point: create a Stripe Checkout session here and
    apply the plan in the payment-succeeded webhook instead of immediately.
    """
    if req.plan not in PLANS:
        raise HTTPException(status_code=422, detail=f"Unknown plan: {req.plan}")
    current = repo.plan_get(tenant_id)
    if req.plan == current:
        raise HTTPException(status_code=409, detail="You are already on this plan")

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
