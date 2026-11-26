"""
QR-code payments — scan, pay, submit the reference, get activated.

A static receiving QR (DuitNow / e-wallet / bank) cannot tell a server that
money arrived, so activation is confirmed by the operator. This module makes
that one click instead of a chat conversation:

  1. Merchant picks a plan and term → an order with a UNIQUE amount.
     (A few sen are knocked off the price so no two open orders share an
     amount; the operator can then spot the payment in the wallet history
     by amount alone.)
  2. Merchant scans the QR, pays exactly that amount, and submits the
     transaction reference shown in their banking / wallet app.
  3. Operator sees the order in the console (and by email when
     OPERATOR_EMAIL + SMTP are set), checks it against the wallet, and
     confirms → the plan is activated for the paid term and the merchant
     is emailed. Rejecting returns the order to the merchant with a note.

Paid terms expire: the plan lapses to Free at the end of the term
(repo.plan_get), and paying again while active extends from the current end.

Configuration: PAY_PAYEE_NAME, OPERATOR_EMAIL (backend/settings.py). The
receiving QR code is uploaded by the operator in the admin console and kept in
the database — never in the repository or a public URL. The feature is on
once a QR code has been uploaded; signed-in merchants fetch the image from
GET /billing/pay-qr.

Endpoints
---------
  GET  /billing/pay-config               Is QR payment on; payee, currency, prices
  POST /billing/orders                   Create (or reuse) an order
  GET  /billing/orders                   My recent orders
  POST /billing/orders/{no}/submit       "I've paid" + transaction reference
  POST /billing/orders/{no}/cancel       Withdraw an unpaid order
  GET  /billing/pay-qr                   The receiving QR image (signed-in accounts only)
  POST /admin/pay-qr                     Operator: upload / replace the QR image
  DELETE /admin/pay-qr                   Operator: remove it (turns scan-to-pay off)
  GET  /admin/orders                     Operator: orders (default: awaiting confirmation)
  POST /admin/orders/{no}/confirm        Operator: payment received → activate
  POST /admin/orders/{no}/reject         Operator: not received (with a note)
"""
import base64
import logging
import random
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend import mailer, ratelimit, repo, settings
from backend.admin import _audit, _require_admin
from backend.auth import DEMO_SUFFIX, require_owner, require_tenant
from backend.billing import PLANS, TERMS, term_end

router = APIRouter()
log = logging.getLogger("kol.pay")

_QR_KEY = "pay_qr"                        # system setting holding the uploaded image
_QR_MAX_BYTES = 2 * 1024 * 1024
_QR_TYPES = {b"\x89PNG\r\n\x1a\n": "image/png", b"\xff\xd8\xff": "image/jpeg", b"RIFF": "image/webp"}
_PENDING_TTL = timedelta(hours=24)       # unpaid orders lapse after this
_MAX_OPEN_ORDERS = 3
_ORDER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/I
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-_/.]{3,63}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _qr() -> Optional[dict]:
    doc = repo.system_get(_QR_KEY)
    return doc if doc and doc.get("data") else None


def enabled() -> bool:
    return _qr() is not None


def _sniff(data: bytes) -> Optional[str]:
    for magic, mime in _QR_TYPES.items():
        if data.startswith(magic) and (mime != "image/webp" or data[8:12] == b"WEBP"):
            return mime
    return None


def store_qr(data: bytes) -> dict:
    """Validate and save the receiving QR image (used by the admin upload)."""
    if not data:
        raise HTTPException(status_code=422, detail="The file is empty.")
    if len(data) > _QR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The image is larger than 2 MB.")
    mime = _sniff(data)
    if not mime:
        raise HTTPException(status_code=422, detail="Upload a PNG, JPEG or WebP image.")
    now = _now().isoformat()
    repo.system_set(_QR_KEY, {"mime": mime, "data": base64.b64encode(data).decode("ascii"),
                              "size": len(data), "updated_at": now}, now)
    return {"mime": mime, "size": len(data), "updated_at": now}


def list_price(plan: str, months: int) -> float:
    """Price of a term in the payment currency, before the uniqueness discount."""
    return round(PLANS[plan]["price_myr"] * months, 2)


def _unique_amount(plan: str, months: int) -> float:
    """List price minus 1–49 sen, distinct from every other open order."""
    base = list_price(plan, months)
    taken = repo.order_open_amounts(settings.PAY_CURRENCY)
    choices = [round(base - c / 100, 2) for c in range(1, 50)]
    free = [a for a in choices if a not in taken and a > 0]
    if not free:
        raise HTTPException(status_code=503,
                            detail="Too many payments are in progress — please try again in a few minutes.")
    return random.choice(free)


def _order_no() -> str:
    return "LM" + "".join(secrets.choice(_ORDER_ALPHABET) for _ in range(6))


def _expire_stale():
    repo.orders_expire_pending((_now() - _PENDING_TTL).isoformat())


def _public(o: dict) -> dict:
    """An order as its owner sees it."""
    return {k: o[k] for k in ("order_no", "plan", "months", "amount", "currency", "status",
                              "reference", "note", "created_at", "submitted_at", "decided_at")} | {
        "plan_name": PLANS.get(o["plan"], {}).get("name", o["plan"])}


def _own_order(order_no: str, tenant_id: str) -> dict:
    o = repo.order_get(order_no.strip().upper())
    if not o or o["tenant_id"] != tenant_id:        # same answer for "not yours"
        raise HTTPException(status_code=404, detail="Order not found")
    return o


# ── Merchant side ─────────────────────────────────────────────────
class OrderRequest(BaseModel):
    plan: str = Field(..., max_length=16)
    months: int = 1


class SubmitRequest(BaseModel):
    reference: str = Field(..., max_length=64)


@router.get("/billing/pay-config")
def pay_config(_t: str = Depends(require_tenant)):
    on = enabled()
    return {
        "enabled": on,
        "qr_image": "/billing/pay-qr" if on else None,
        "payee": settings.PAY_PAYEE_NAME or None,
        "currency": settings.PAY_CURRENCY,
        "terms": list(TERMS),
        "prices": {pid: {str(m): list_price(pid, m) for m in TERMS}
                   for pid, p in PLANS.items() if p["price_myr"] > 0} if on else {},
    }


@router.get("/billing/pay-qr")
def pay_qr(_t: str = Depends(require_tenant)):
    """The receiving QR image. Signed-in accounts only, never cached by shared caches."""
    qr = _qr()
    if not qr:
        raise HTTPException(status_code=404, detail="Scan-to-pay is not set up.")
    return Response(base64.b64decode(qr["data"]), media_type=qr.get("mime") or "image/png",
                    headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/billing/orders")
def create_order(req: OrderRequest, tenant_id: str = Depends(require_owner)):
    """Start a payment. Asking again for the same plan and term returns the
    order already open, so reopening the dialog doesn't change the amount."""
    if not enabled():
        raise HTTPException(status_code=503, detail="QR payment is not available right now — contact us to upgrade.")
    if req.plan not in PLANS or PLANS[req.plan]["price_myr"] <= 0:
        raise HTTPException(status_code=422, detail=f"Unknown paid plan: {req.plan}")
    if req.months not in TERMS:
        raise HTTPException(status_code=422, detail=f"months must be one of {list(TERMS)}")
    user = repo.user_find_by_tenant(tenant_id)
    if not user or user["email"].endswith(DEMO_SUFFIX):
        raise HTTPException(status_code=403,
                            detail="Create a free account first — demo sessions can't buy a plan.")

    _expire_stale()
    open_orders = [o for o in repo.orders_for_tenant(tenant_id) if o["status"] in repo.ORDER_OPEN]
    for o in open_orders:
        if o["plan"] == req.plan and o["months"] == req.months:
            return _public(o)
    if len(open_orders) >= _MAX_OPEN_ORDERS:
        raise HTTPException(status_code=409,
                            detail="You already have payments in progress — finish or cancel one first.")
    ratelimit.enforce("order", tenant_id, limit=10, window=3600)

    order = repo.order_create(
        order_no=_order_no(), tenant_id=tenant_id, email=user["email"], plan=req.plan,
        months=req.months, amount=_unique_amount(req.plan, req.months),
        currency=settings.PAY_CURRENCY, status="pending", created_at=_now().isoformat())
    log.info("order created %s plan=%s months=%s", order["order_no"], req.plan, req.months)
    return _public(order)


@router.get("/billing/orders")
def my_orders(tenant_id: str = Depends(require_tenant)):
    _expire_stale()
    return {"orders": [_public(o) for o in repo.orders_for_tenant(tenant_id)]}


@router.post("/billing/orders/{order_no}/submit")
def submit_order(order_no: str, req: SubmitRequest, background: BackgroundTasks,
                 tenant_id: str = Depends(require_owner)):
    """The merchant has paid: record the transaction reference and queue the
    order for the operator. A rejected order can be corrected and resubmitted."""
    order = _own_order(order_no, tenant_id)
    ratelimit.enforce("order-submit", tenant_id, limit=10, window=3600)
    reference = " ".join(req.reference.split())
    if not _REFERENCE_RE.match(reference):
        raise HTTPException(status_code=422,
                            detail="Enter the transaction reference from your payment receipt (4–64 letters or digits).")
    if repo.order_reference_used(reference, order["order_no"]):
        raise HTTPException(status_code=409,
                            detail="This transaction reference was already used for another order.")

    updated = repo.order_update(order["order_no"], expect_status=("pending", "rejected"),
                                status="submitted", reference=reference, note=None,
                                submitted_at=_now().isoformat())
    if updated is None:
        raise HTTPException(status_code=409, detail=f"This order is already {order['status']}.")
    log.info("order submitted %s", updated["order_no"])
    if settings.OPERATOR_EMAIL:
        background.add_task(mailer.send_order_to_operator, settings.OPERATOR_EMAIL, updated,
                            mailer.base_url())
    return _public(updated)


@router.post("/billing/orders/{order_no}/cancel")
def cancel_order(order_no: str, tenant_id: str = Depends(require_owner)):
    order = _own_order(order_no, tenant_id)
    updated = repo.order_update(order["order_no"], expect_status=("pending", "rejected"),
                                status="cancelled", decided_at=_now().isoformat())
    if updated is None:
        raise HTTPException(status_code=409,
                            detail="This order can't be cancelled — it is already being reviewed or finished.")
    return _public(updated)


# ── Operator side ─────────────────────────────────────────────────
class RejectRequest(BaseModel):
    note: str = Field("", max_length=255)


@router.get("/admin/pay-qr", dependencies=[Depends(_require_admin)])
def admin_qr_info():
    """Whether a QR code is uploaded (and the image itself for the console preview)."""
    qr = _qr()
    if not qr:
        return {"uploaded": False}
    return {"uploaded": True, "mime": qr.get("mime"), "size": qr.get("size"),
            "updated_at": qr.get("updated_at"),
            "data_url": f"data:{qr.get('mime') or 'image/png'};base64,{qr['data']}"}


@router.post("/admin/pay-qr", dependencies=[Depends(_require_admin)])
async def admin_qr_upload(request: Request, file: UploadFile = File(...)):
    data = await file.read(_QR_MAX_BYTES + 1)
    info = store_qr(data)
    _audit(request, "pay.qr_upload", "", f"{info['mime']} {info['size']} bytes")
    return {"status": "ok", **info}


@router.delete("/admin/pay-qr", dependencies=[Depends(_require_admin)])
def admin_qr_delete(request: Request):
    repo.system_set(_QR_KEY, None)
    _audit(request, "pay.qr_delete")
    return {"status": "ok"}


@router.get("/admin/orders", dependencies=[Depends(_require_admin)])
def admin_orders(status: str = Query("submitted")):
    """Orders for the console. status=submitted (default) is the to-do list;
    status=all shows recent history."""
    _expire_stale()
    statuses = None if status == "all" else tuple(s for s in status.split(",") if s)
    return {"orders": repo.orders_all(statuses)}


@router.post("/admin/orders/{order_no}/confirm", dependencies=[Depends(_require_admin)])
def admin_confirm(order_no: str, request: Request, background: BackgroundTasks):
    """Payment received: activate the plan for the paid term."""
    order = repo.order_get(order_no.strip().upper())
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    # Claim the order first, so a double click (or two operators) can't
    # activate — and extend — the same payment twice.
    claimed = repo.order_update(order["order_no"], expect_status=("submitted",),
                                status="paid", decided_at=_now().isoformat())
    if claimed is None:
        raise HTTPException(status_code=409, detail=f"Order is {order['status']}, not awaiting confirmation.")
    user = repo.user_find_by_tenant(order["tenant_id"])
    if not user:
        repo.order_update(order["order_no"], status="rejected", note="account no longer exists")
        raise HTTPException(status_code=404, detail="The account for this order no longer exists")

    expires_at = term_end(order["tenant_id"], order["plan"], order["months"])
    repo.plan_set(order["tenant_id"], order["plan"], _now().isoformat(), expires_at=expires_at)
    repo.audit_add("order.confirm", user["email"],
                   f"{order['order_no']} {order['currency']} {order['amount']:.2f} "
                   f"{order['plan']}x{order['months']}mo ref={order.get('reference') or '-'}",
                   ratelimit.client_ip(request))
    log.info("order paid %s plan=%s until=%s", order["order_no"], order["plan"], expires_at)
    background.add_task(mailer.send_plan_changed, user["email"], PLANS[order["plan"]]["name"],
                        mailer.base_url(), user.get("lang", "en"),
                        expires_at[:10] if expires_at else "")
    return {"status": "ok", "order": claimed, "plan": order["plan"], "expires_at": expires_at}


@router.post("/admin/orders/{order_no}/reject", dependencies=[Depends(_require_admin)])
def admin_reject(order_no: str, req: RejectRequest, request: Request, background: BackgroundTasks):
    """Payment not found: hand the order back with a note. The merchant can
    correct the reference and resubmit."""
    order = repo.order_get(order_no.strip().upper())
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    note = req.note.strip() or None
    updated = repo.order_update(order["order_no"], expect_status=("submitted",),
                                status="rejected", note=note, decided_at=_now().isoformat())
    if updated is None:
        raise HTTPException(status_code=409, detail=f"Order is {order['status']}, not awaiting confirmation.")
    repo.audit_add("order.reject", order["email"],
                   f"{order['order_no']} {order['currency']} {order['amount']:.2f} note={note or '-'}",
                   ratelimit.client_ip(request))
    user = repo.user_find_by_tenant(order["tenant_id"])
    if user:
        background.add_task(mailer.send_order_rejected, user["email"], updated,
                            mailer.base_url(), user.get("lang", "en"))
    return {"status": "ok", "order": updated}
