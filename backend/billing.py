"""
Subscription plans, usage limits and Lemon Squeezy billing.

Lemon Squeezy is the merchant of record: it takes the card payment, handles
global sales tax/VAT and invoices, and pays out to an individual (no company
needed). We only:
  1. create a hosted checkout for a plan        POST /billing/checkout
  2. receive subscription webhooks               POST /billing/webhook
  3. report plan + usage to the app              GET  /billing/status

Env
  LEMONSQUEEZY_API_KEY         API key (Settings → API)
  LEMONSQUEEZY_STORE_ID        numeric store id
  LEMONSQUEEZY_VARIANT_PRO     variant id of the Pro monthly product
  LEMONSQUEEZY_VARIANT_AGENCY  variant id of the Agency monthly product
  LEMONSQUEEZY_WEBHOOK_SECRET  signing secret set on the webhook
"""
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend import config, db, tenancy
from backend.auth import require_tenant

router = APIRouter()
log = logging.getLogger("kol.billing")

# ── Plans ─────────────────────────────────────────────────────────
# None means unlimited.
PLANS = {
    "free": {
        "name": "Free", "price_usd": 0,
        "optimizations_per_month": 10, "max_campaigns": 2, "import_export": False,
    },
    "pro": {
        "name": "Pro", "price_usd": 29,
        "optimizations_per_month": None, "max_campaigns": None, "import_export": True,
    },
    "agency": {
        "name": "Agency", "price_usd": 99,
        "optimizations_per_month": None, "max_campaigns": None, "import_export": True,
    },
}
PAID_PLANS = ("pro", "agency")

# Lemon Squeezy subscription statuses that keep paid access.
# `cancelled` keeps access until the period ends, when LS sends `expired`.
_ACTIVE_STATUSES = {"on_trial", "active", "past_due", "cancelled"}


def _variant_for(plan: str) -> Optional[str]:
    return os.environ.get(f"LEMONSQUEEZY_VARIANT_{plan.upper()}")


def _plan_for_variant(variant_id) -> Optional[str]:
    for plan in PAID_PLANS:
        v = _variant_for(plan)
        if v and str(v) == str(variant_id):
            return plan
    return None


# ── Plan & usage lookups ──────────────────────────────────────────
def _account(tenant_id: str) -> Optional[dict]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE tenant_id=?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def plan_for(tenant_id: str) -> str:
    # The unauthenticated dev/test tenant (never allowed in production) gets everything.
    if tenancy.DEFAULT_TENANT and tenant_id == tenancy.DEFAULT_TENANT:
        return "agency"
    acct = _account(tenant_id)
    plan = (acct or {}).get("plan") or "free"
    return plan if plan in PLANS else "free"


def limits_for(tenant_id: str) -> dict:
    return PLANS[plan_for(tenant_id)]


def _period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage(tenant_id: str, metric: str) -> int:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE tenant_id=? AND period=? AND metric=?",
            (tenant_id, _period(), metric),
        ).fetchone()
    return row["count"] if row else 0


def _upgrade_error(message: str):
    # 402 Payment Required: the frontend turns this into an upgrade prompt.
    raise HTTPException(status_code=402, detail={"message": message, "upgrade": True})


def consume_optimization(metric: str = "optimizations"):
    """Dependency for the optimizer endpoints: atomically count one run against the quota."""
    async def dep(tenant_id: str = Depends(require_tenant)) -> str:
        cap = limits_for(tenant_id)["optimizations_per_month"]
        with db.connect() as conn, db.tx(conn):
            row = conn.execute(
                "SELECT count FROM usage WHERE tenant_id=? AND period=? AND metric=?",
                (tenant_id, _period(), metric),
            ).fetchone()
            used = row["count"] if row else 0
            if cap is not None and used >= cap:
                _upgrade_error(f"You've used all {cap} free optimizations this month. "
                               "Upgrade to Pro for unlimited runs.")
            conn.execute(
                "INSERT INTO usage (tenant_id, period, metric, count) VALUES (?,?,?,1) "
                "ON CONFLICT(tenant_id, period, metric) DO UPDATE SET count = count + 1",
                (tenant_id, _period(), metric),
            )
        return tenant_id
    return dep


async def require_import_export(tenant_id: str = Depends(require_tenant)) -> str:
    if not limits_for(tenant_id)["import_export"]:
        _upgrade_error("Importing and exporting creator lists is a Pro feature.")
    return tenant_id


def check_campaign_quota(tenant_id: str, current_count: int):
    cap = limits_for(tenant_id)["max_campaigns"]
    if cap is not None and current_count >= cap:
        _upgrade_error(f"The Free plan tracks up to {cap} campaigns. Upgrade to Pro for unlimited campaigns.")


# ── Endpoints ─────────────────────────────────────────────────────
@router.get("/billing/plans")
def list_plans():
    return {"plans": PLANS, "checkout_enabled": _checkout_configured()}


@router.get("/billing/status")
def billing_status(tenant_id: str = Depends(require_tenant)):
    acct = _account(tenant_id) or {}
    plan = plan_for(tenant_id)
    return {
        "plan": plan,
        "limits": PLANS[plan],
        "usage": {"optimizations": usage(tenant_id, "optimizations")},
        "subscription_status": acct.get("subscription_status"),
        "renews_at": acct.get("subscription_renews_at"),
        "ends_at": acct.get("subscription_ends_at"),
        "portal_url": acct.get("customer_portal_url"),
        "checkout_enabled": _checkout_configured(),
    }


class CheckoutRequest(BaseModel):
    plan: str


def _checkout_configured() -> bool:
    return bool(os.environ.get("LEMONSQUEEZY_API_KEY") and os.environ.get("LEMONSQUEEZY_STORE_ID"))


@router.post("/billing/checkout")
def create_checkout(req: CheckoutRequest, tenant_id: str = Depends(require_tenant)):
    if req.plan not in PAID_PLANS:
        raise HTTPException(status_code=422, detail="Unknown plan")
    variant = _variant_for(req.plan)
    if not _checkout_configured() or not variant:
        raise HTTPException(status_code=503, detail="Payments are not set up yet. Please try again later.")
    acct = _account(tenant_id) or {}
    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "email": acct.get("email"),
                    "custom": {"tenant_id": tenant_id},
                },
                "product_options": {"redirect_url": f"{config.PUBLIC_BASE_URL}/?upgraded=1"},
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(os.environ["LEMONSQUEEZY_STORE_ID"])}},
                "variant": {"data": {"type": "variants", "id": str(variant)}},
            },
        }
    }
    try:
        r = httpx.post(
            "https://api.lemonsqueezy.com/v1/checkouts",
            json=payload,
            headers={
                "Authorization": f"Bearer {os.environ['LEMONSQUEEZY_API_KEY']}",
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/vnd.api+json",
            },
            timeout=20,
        )
        r.raise_for_status()
        url = r.json()["data"]["attributes"]["url"]
    except (httpx.HTTPError, KeyError, ValueError):
        log.exception("Lemon Squeezy checkout creation failed")
        raise HTTPException(status_code=502, detail="Could not start checkout. Please try again.")
    return {"url": url}


def _verify_signature(raw: bytes, signature: Optional[str]) -> bool:
    secret = os.environ.get("LEMONSQUEEZY_WEBHOOK_SECRET")
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


@router.post("/billing/webhook")
async def webhook(request: Request):
    raw = await request.body()
    if not _verify_signature(raw, request.headers.get("x-signature")):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    name = event.get("meta", {}).get("event_name", "")
    if not name.startswith("subscription_"):
        return {"ok": True, "ignored": name}

    custom = event.get("meta", {}).get("custom_data") or {}
    data = event.get("data", {})
    attrs = data.get("attributes", {})
    sub_id = str(data.get("id", ""))

    with db.connect() as conn, db.tx(conn):
        row = None
        if custom.get("tenant_id"):
            row = conn.execute("SELECT id FROM users WHERE tenant_id=?", (custom["tenant_id"],)).fetchone()
        if row is None and sub_id:
            row = conn.execute("SELECT id FROM users WHERE subscription_id=?", (sub_id,)).fetchone()
        if row is None:
            log.warning("Webhook %s for unknown account (sub %s)", name, sub_id)
            return {"ok": True, "ignored": "unknown account"}

        status = attrs.get("status")
        paid_plan = _plan_for_variant(attrs.get("variant_id"))
        plan = paid_plan if (paid_plan and status in _ACTIVE_STATUSES) else "free"
        conn.execute(
            "UPDATE users SET plan=?, subscription_id=?, subscription_status=?, "
            "subscription_renews_at=?, subscription_ends_at=?, customer_portal_url=? WHERE id=?",
            (plan, sub_id, status, attrs.get("renews_at"), attrs.get("ends_at"),
             (attrs.get("urls") or {}).get("customer_portal"), row["id"]),
        )
    return {"ok": True, "plan": plan}
