"""
QR-payment orders: create → pay → submit reference → operator confirms,
plus plan expiry. Uses the QR image that ships in frontend/pay/.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import billing, mailer, payments, repo, settings

client = TestClient(app)
ADMIN = {"X-Admin-Key": "operator-key-for-tests"}


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "operator-key-for-tests")
    monkeypatch.setattr(settings, "BILLING_SELF_SERVE", False)
    monkeypatch.setattr(settings, "PAY_CURRENCY", "MYR")
    assert payments.enabled(), "frontend/pay/duitnow-qr.png should ship with the repo"


def _register():
    email = f"payer_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    return email, headers, tenant_id


def _ref():
    return "TNG" + uuid.uuid4().hex[:12].upper()


def _order(headers, plan="pro", months=1):
    r = client.post("/billing/orders", json={"plan": plan, "months": months}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_pay_config_lists_prices_in_local_currency():
    _, headers, _ = _register()
    c = client.get("/billing/pay-config", headers=headers).json()
    assert c["enabled"] is True and c["currency"] == "MYR"
    assert c["qr_image"] == "/pay/duitnow-qr.png"
    assert c["prices"]["pro"]["1"] == 29.0                          # RM29 / month
    assert c["prices"]["team"]["1"] == 99.0                         # RM99 / month
    assert c["prices"]["team"]["12"] == 99.0 * 12
    assert client.get("/pay/duitnow-qr.png").status_code == 200       # the image is served


def test_full_payment_flow_activates_plan_with_expiry():
    email, headers, _ = _register()
    order = _order(headers, "pro", 1)
    base = payments.list_price("pro", 1)
    assert order["status"] == "pending" and order["currency"] == "MYR"
    assert base - 0.50 < order["amount"] < base           # never more than list price

    # Reopening the dialog returns the same order, same amount.
    assert _order(headers, "pro", 1)["order_no"] == order["order_no"]

    # Nothing is activated by creating or submitting.
    ref = _ref()
    r = client.post(f"/billing/orders/{order['order_no']}/submit", json={"reference": ref}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    assert client.get("/billing/info", headers=headers).json()["plan"] == "free"

    # Operator sees it in the to-do list and confirms.
    todo = client.get("/admin/orders", headers=ADMIN).json()["orders"]
    mine = next(o for o in todo if o["order_no"] == order["order_no"])
    assert mine["email"] == email and mine["reference"] == ref
    r = client.post(f"/admin/orders/{order['order_no']}/confirm", headers=ADMIN)
    assert r.status_code == 200, r.text

    info = client.get("/billing/info", headers=headers).json()
    assert info["plan"] == "pro"
    expires = datetime.fromisoformat(info["expires_at"])
    assert timedelta(days=29) < expires - datetime.now(timezone.utc) < timedelta(days=31)

    # Confirming twice must not extend the term again.
    assert client.post(f"/admin/orders/{order['order_no']}/confirm", headers=ADMIN).status_code == 409
    assert client.get("/billing/info", headers=headers).json()["expires_at"] == info["expires_at"]
    assert client.get("/billing/orders", headers=headers).json()["orders"][0]["status"] == "paid"


def test_renewal_extends_from_current_end():
    _, headers, _ = _register()
    for _ in range(2):
        o = _order(headers, "pro", 3)
        client.post(f"/billing/orders/{o['order_no']}/submit", json={"reference": _ref()}, headers=headers)
        assert client.post(f"/admin/orders/{o['order_no']}/confirm", headers=ADMIN).status_code == 200
    expires = datetime.fromisoformat(client.get("/billing/info", headers=headers).json()["expires_at"])
    assert timedelta(days=179) < expires - datetime.now(timezone.utc) < timedelta(days=181)


def test_plan_lapses_to_free_after_expiry():
    _, headers, tenant_id = _register()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    repo.plan_set(tenant_id, "pro", past, expires_at=past)
    info = client.get("/billing/info", headers=headers).json()
    assert info["plan"] == "free" and info["expires_at"] is None
    assert info["limits"]["monthly_optimizations"] == billing.PLANS["free"]["monthly_optimizations"]


def test_operator_grant_has_no_expiry():
    email, headers, _ = _register()
    assert client.post("/admin/plan", json={"email": email, "plan": "team"}, headers=ADMIN).status_code == 200
    info = client.get("/billing/info", headers=headers).json()
    assert info["plan"] == "team" and info["expires_at"] is None


def test_open_orders_never_share_an_amount():
    amounts = set()
    for _ in range(6):
        _, headers, _ = _register()
        amounts.add(_order(headers, "pro", 1)["amount"])
    assert len(amounts) == 6


def test_reject_then_resubmit():
    _, headers, _ = _register()
    o = _order(headers)
    client.post(f"/billing/orders/{o['order_no']}/submit", json={"reference": _ref()}, headers=headers)
    r = client.post(f"/admin/orders/{o['order_no']}/reject", json={"note": "amount not found"}, headers=ADMIN)
    assert r.status_code == 200
    mine = client.get("/billing/orders", headers=headers).json()["orders"][0]
    assert mine["status"] == "rejected" and mine["note"] == "amount not found"
    assert client.get("/billing/info", headers=headers).json()["plan"] == "free"

    again = client.post(f"/billing/orders/{o['order_no']}/submit", json={"reference": _ref()}, headers=headers)
    assert again.status_code == 200 and again.json()["status"] == "submitted"


def test_reference_cannot_be_reused_across_orders():
    ref = _ref()
    _, a, _ = _register()
    oa = _order(a)
    assert client.post(f"/billing/orders/{oa['order_no']}/submit", json={"reference": ref}, headers=a).status_code == 200
    _, b, _ = _register()
    ob = _order(b)
    dup = client.post(f"/billing/orders/{ob['order_no']}/submit", json={"reference": ref.lower()}, headers=b)
    assert dup.status_code == 409


def test_orders_are_private_to_their_tenant():
    _, a, _ = _register()
    o = _order(a)
    _, b, _ = _register()
    assert client.post(f"/billing/orders/{o['order_no']}/submit", json={"reference": _ref()}, headers=b).status_code == 404
    assert client.post(f"/billing/orders/{o['order_no']}/cancel", headers=b).status_code == 404
    assert client.get("/billing/orders", headers=b).json()["orders"] == []


def test_validation_and_guards():
    _, headers, _ = _register()
    assert client.post("/billing/orders", json={"plan": "free"}, headers=headers).status_code == 422
    assert client.post("/billing/orders", json={"plan": "pro", "months": 7}, headers=headers).status_code == 422
    o = _order(headers)
    bad = client.post(f"/billing/orders/{o['order_no']}/submit", json={"reference": "<x>"}, headers=headers)
    assert bad.status_code == 422
    # Operator endpoints need the key; an unsubmitted order can't be confirmed.
    assert client.get("/admin/orders").status_code == 403
    assert client.post(f"/admin/orders/{o['order_no']}/confirm").status_code == 403
    assert client.post(f"/admin/orders/{o['order_no']}/confirm", headers=ADMIN).status_code == 409
    # Cancel, then it can no longer be submitted.
    assert client.post(f"/billing/orders/{o['order_no']}/cancel", headers=headers).status_code == 200
    late = client.post(f"/billing/orders/{o['order_no']}/submit", json={"reference": _ref()}, headers=headers)
    assert late.status_code == 409


def test_demo_sessions_cannot_buy():
    token = client.post("/auth/demo").json()["token"]
    r = client.post("/billing/orders", json={"plan": "pro"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_unpaid_orders_expire_after_a_day():
    _, headers, _ = _register()
    o = _order(headers)
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    repo.order_update(o["order_no"], created_at=old)
    assert client.get("/billing/orders", headers=headers).json()["orders"][0]["status"] == "expired"
    assert _order(headers)["order_no"] != o["order_no"]       # a fresh one is issued


def test_emails_operator_on_submit_and_merchant_on_confirm(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(mailer, "SMTP_FROM", "tiko <no-reply@tiko.test>")
    monkeypatch.setattr(mailer, "APP_BASE_URL", "https://app.tiko.test")
    monkeypatch.setattr(mailer, "_deliver", sent.append)
    monkeypatch.setattr(settings, "OPERATOR_EMAIL", "ops@tiko.test")

    email, headers, _ = _register()
    sent.clear()
    o = _order(headers)
    client.post(f"/billing/orders/{o['order_no']}/submit", json={"reference": _ref()}, headers=headers)
    assert sent[-1]["To"] == "ops@tiko.test" and o["order_no"] in sent[-1]["Subject"]

    client.post(f"/admin/orders/{o['order_no']}/confirm", headers=ADMIN)
    assert sent[-1]["To"] == email and "Pro" in sent[-1]["Subject"]


def test_disabled_when_qr_image_missing(monkeypatch):
    monkeypatch.setattr(settings, "PAY_QR_IMAGE", "pay/nope.png")
    _, headers, _ = _register()
    assert client.get("/billing/pay-config", headers=headers).json()["enabled"] is False
    assert client.post("/billing/orders", json={"plan": "pro"}, headers=headers).status_code == 503
