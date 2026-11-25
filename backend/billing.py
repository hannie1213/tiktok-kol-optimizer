"""
Subscription plans, usage limits and manual (QR code) payments, priced in MYR.

Flow
  1. A merchant picks a plan + number of months in the app. The app shows the
     owner's payment QR (DuitNow / Touch 'n Go / bank) with the amount and a
     unique reference code to put in the transfer note.
  2. After paying, the merchant submits the bank/e-wallet transaction id and an
     optional screenshot                              POST /billing/requests
  3. An admin (email listed in ADMIN_EMAILS) reviews it on /admin.html and
     approves or rejects                POST /admin/payments/{id}/approve|reject
  4. Approval sets the plan and extends its expiry by the months paid; after
     the expiry date the account falls back to Free automatically.

Env
  ADMIN_EMAILS      comma-separated emails allowed to review payments
  PAYMENT_QR_PATH   image of the owner's payment QR (default data/payment_qr.png)
  PAYMENT_PAYEE     name shown under the QR, e.g. "YUHAN (DuitNow)"
  PRICE_PRO_MYR / PRICE_AGENCY_MYR   override monthly prices
"""
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from backend import config, db, mailer, tenancy
from backend.auth import require_tenant

router = APIRouter()
log = logging.getLogger("kol.billing")

CURRENCY = "MYR"
MONTH_OPTIONS = (1, 3, 12)
_DAYS_PER_MONTH = 30
_MAX_PROOF_BYTES = 3 * 1024 * 1024
_PROOF_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# ── Plans (None = unlimited) ──────────────────────────────────────
PLANS = {
    "free": {
        "name": "Free", "price_myr": 0,
        "optimizations_per_month": 10, "max_campaigns": 2, "import_export": False,
    },
    "pro": {
        "name": "Pro", "price_myr": int(os.environ.get("PRICE_PRO_MYR", "99")),
        "optimizations_per_month": None, "max_campaigns": None, "import_export": True,
    },
    "agency": {
        "name": "Agency", "price_myr": int(os.environ.get("PRICE_AGENCY_MYR", "399")),
        "optimizations_per_month": None, "max_campaigns": None, "import_export": True,
    },
}
PAID_PLANS = ("pro", "agency")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(ts) if ts else None


# ── Accounts, plans, admins ───────────────────────────────────────
def _account(tenant_id: str) -> Optional[dict]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE tenant_id=?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def admin_emails() -> set:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


async def require_admin(tenant_id: str = Depends(require_tenant)) -> dict:
    acct = _account(tenant_id)
    if not acct or acct["email"] not in admin_emails():
        raise HTTPException(status_code=403, detail="Admins only")
    return acct


def _effective_plan(acct: Optional[dict]) -> str:
    if not acct:
        return "free"
    plan = acct.get("plan") or "free"
    if plan not in PAID_PLANS:
        return "free"
    expires = _parse(acct.get("plan_expires_at"))
    if expires is None or expires <= _now():
        return "free"
    return plan


def plan_for(tenant_id: str) -> str:
    # The unauthenticated dev/test tenant (refused in production) gets everything.
    if tenancy.DEFAULT_TENANT and tenant_id == tenancy.DEFAULT_TENANT:
        return "agency"
    return _effective_plan(_account(tenant_id))


def limits_for(tenant_id: str) -> dict:
    return PLANS[plan_for(tenant_id)]


# ── Usage limits ──────────────────────────────────────────────────
def _period() -> str:
    return _now().strftime("%Y-%m")


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


# ── Payment requests ──────────────────────────────────────────────
def _qr_path() -> str:
    return os.environ.get("PAYMENT_QR_PATH") or os.path.join(tenancy.DATA_DIR, "payment_qr.png")


def _request_public(row) -> dict:
    d = dict(row)
    d.pop("proof", None)
    d["has_proof"] = bool(row["proof_type"])
    d.pop("proof_type", None)
    return d


def _pending_for(conn, user_id: int):
    return conn.execute(
        "SELECT * FROM payment_requests WHERE user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


@router.get("/billing/plans")
def list_plans():
    return {
        "currency": CURRENCY,
        "plans": PLANS,
        "month_options": list(MONTH_OPTIONS),
        "payee": os.environ.get("PAYMENT_PAYEE", ""),
        "qr_available": os.path.exists(_qr_path()),
    }


@router.get("/billing/qr")
def payment_qr():
    p = _qr_path()
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="Payment QR code has not been uploaded yet")
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


@router.get("/billing/status")
def billing_status(tenant_id: str = Depends(require_tenant)):
    acct = _account(tenant_id) or {}
    plan = plan_for(tenant_id)
    pending = None
    if acct.get("id"):
        with db.connect() as conn:
            row = _pending_for(conn, acct["id"])
            last = conn.execute(
                "SELECT * FROM payment_requests WHERE user_id=? AND status!='pending' ORDER BY id DESC LIMIT 1",
                (acct["id"],),
            ).fetchone()
        pending = _request_public(row) if row else None
        last = _request_public(last) if last else None
    else:
        last = None
    return {
        "plan": plan,
        "currency": CURRENCY,
        "limits": PLANS[plan],
        "usage": {"optimizations": usage(tenant_id, "optimizations")},
        "plan_expires_at": acct.get("plan_expires_at") if plan != "free" else None,
        "pending_request": pending,
        "last_decision": last,
        "is_admin": (acct.get("email") or "") in admin_emails(),
    }


@router.post("/billing/requests")
async def submit_payment(
    plan: str = Form(...),
    months: int = Form(1),
    reference: str = Form(...),
    note: str = Form(""),
    proof: Optional[UploadFile] = File(None),
    tenant_id: str = Depends(require_tenant),
):
    """Merchant says "I've paid": record the transaction id (+ screenshot) for review."""
    if plan not in PAID_PLANS:
        raise HTTPException(status_code=422, detail="Unknown plan")
    if months not in MONTH_OPTIONS:
        raise HTTPException(status_code=422, detail="Choose 1, 3 or 12 months")
    reference = reference.strip()
    if not 4 <= len(reference) <= 80:
        raise HTTPException(status_code=422, detail="Please enter the transaction / reference number from your receipt")

    proof_bytes, proof_type = None, None
    if proof is not None and proof.filename:
        proof_bytes = await proof.read()
        proof_type = (proof.content_type or "").lower()
        if proof_type not in _PROOF_TYPES:
            raise HTTPException(status_code=422, detail="Screenshot must be a PNG, JPG, WEBP or GIF image")
        if len(proof_bytes) > _MAX_PROOF_BYTES:
            raise HTTPException(status_code=422, detail="Screenshot must be under 3 MB")

    acct = _account(tenant_id)
    if not acct:
        raise HTTPException(status_code=401, detail="Not authenticated")
    amount = PLANS[plan]["price_myr"] * months
    code = "KOL-" + secrets.token_hex(3).upper()
    with db.connect() as conn, db.tx(conn):
        if _pending_for(conn, acct["id"]):
            raise HTTPException(status_code=409, detail="You already have a payment waiting for review")
        cur = conn.execute(
            "INSERT INTO payment_requests (user_id, plan, months, amount_myr, code, reference, note, "
            "proof, proof_type, status, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (acct["id"], plan, months, amount, code, reference, note.strip()[:500],
             proof_bytes, proof_type, db.now_iso()),
        )
        req_id = cur.lastrowid

    admins = admin_emails()
    if admins:
        try:
            for a in admins:
                mailer.send(
                    a, f"New payment to review: {PLANS[plan]['name']} RM{amount} ({acct['email']})",
                    f"{acct['email']} says they paid RM{amount} for {PLANS[plan]['name']} x {months} month(s).\n"
                    f"Transaction ref: {reference}\n\nReview it at {config.PUBLIC_BASE_URL}/admin.html",
                )
        except Exception:
            log.exception("Failed to notify admins")
    return {"id": req_id, "status": "pending", "amount_myr": amount, "code": code}


@router.delete("/billing/requests/pending")
def cancel_pending(tenant_id: str = Depends(require_tenant)):
    acct = _account(tenant_id)
    with db.connect() as conn, db.tx(conn):
        row = _pending_for(conn, acct["id"]) if acct else None
        if not row:
            raise HTTPException(status_code=404, detail="No pending payment")
        conn.execute("UPDATE payment_requests SET status='cancelled', decided_at=? WHERE id=?",
                     (db.now_iso(), row["id"]))
    return {"ok": True}


# ── Admin ─────────────────────────────────────────────────────────
@router.get("/admin/payments")
def admin_list(status: str = "pending", admin: dict = Depends(require_admin)):
    q = ("SELECT p.*, u.email, u.plan AS current_plan, u.plan_expires_at AS current_expires_at FROM payment_requests p "
         "JOIN users u ON u.id = p.user_id")
    args = ()
    if status != "all":
        q += " WHERE p.status=?"
        args = (status,)
    q += " ORDER BY p.id DESC LIMIT 200"
    with db.connect() as conn:
        rows = conn.execute(q, args).fetchall()
    return {"payments": [_request_public(r) for r in rows]}


@router.get("/admin/payments/{req_id}/proof")
def admin_proof(req_id: int, admin: dict = Depends(require_admin)):
    with db.connect() as conn:
        row = conn.execute("SELECT proof, proof_type FROM payment_requests WHERE id=?", (req_id,)).fetchone()
    if not row or not row["proof"]:
        raise HTTPException(status_code=404, detail="No screenshot")
    return Response(content=row["proof"], media_type=row["proof_type"])


def _load_pending(conn, req_id: int):
    row = conn.execute(
        "SELECT p.*, u.email, u.plan AS current_plan, u.plan_expires_at AS current_expires_at FROM payment_requests p "
        "JOIN users u ON u.id = p.user_id WHERE p.id=?", (req_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Payment not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Already {row['status']}")
    return row


@router.post("/admin/payments/{req_id}/approve")
def admin_approve(req_id: int, admin: dict = Depends(require_admin)):
    with db.connect() as conn, db.tx(conn):
        row = _load_pending(conn, req_id)
        # Renewing the same plan stacks onto the remaining time; a new plan starts now.
        current_exp = _parse(row["current_expires_at"])
        start = current_exp if (row["current_plan"] == row["plan"] and current_exp and current_exp > _now()) else _now()
        expires = start + timedelta(days=_DAYS_PER_MONTH * row["months"])
        conn.execute("UPDATE users SET plan=?, plan_expires_at=? WHERE id=?",
                     (row["plan"], expires.isoformat(), row["user_id"]))
        conn.execute("UPDATE payment_requests SET status='approved', decided_at=?, decided_by=?, "
                     "plan_expires_at=? WHERE id=?",
                     (db.now_iso(), admin["email"], expires.isoformat(), req_id))
    try:
        mailer.send(row["email"], f"Your {PLANS[row['plan']]['name']} plan is active",
                    f"Thanks! We received your payment of RM{row['amount_myr']}.\n"
                    f"Your {PLANS[row['plan']]['name']} plan is active until {expires:%d %b %Y}.\n\n"
                    f"{config.PUBLIC_BASE_URL}")
    except Exception:
        log.exception("Failed to send approval email")
    return {"ok": True, "plan": row["plan"], "plan_expires_at": expires.isoformat()}


@router.post("/admin/payments/{req_id}/reject")
def admin_reject(req_id: int, reason: str = Form(""), admin: dict = Depends(require_admin)):
    reason = reason.strip()[:300] or "We could not find this payment."
    with db.connect() as conn, db.tx(conn):
        row = _load_pending(conn, req_id)
        conn.execute("UPDATE payment_requests SET status='rejected', decided_at=?, decided_by=?, "
                     "reject_reason=? WHERE id=?", (db.now_iso(), admin["email"], reason, req_id))
    try:
        mailer.send(row["email"], "About your KOL Optimizer payment",
                    f"We couldn't confirm your payment (ref {row['reference']}).\nReason: {reason}\n\n"
                    f"You can submit it again from the app: {config.PUBLIC_BASE_URL}")
    except Exception:
        log.exception("Failed to send rejection email")
    return {"ok": True}


@router.post("/admin/payment-qr")
async def admin_upload_qr(file: UploadFile = File(...), admin: dict = Depends(require_admin)):
    """Upload / replace the payment QR image shown to merchants."""
    data = await file.read()
    if (file.content_type or "").lower() not in _PROOF_TYPES or len(data) > _MAX_PROOF_BYTES:
        raise HTTPException(status_code=422, detail="Upload a PNG/JPG/WEBP image under 3 MB")
    p = _qr_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return {"ok": True}
