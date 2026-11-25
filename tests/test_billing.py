"""Plans, usage limits and manual QR payments with admin approval (MYR)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import billing, db, mailer
from backend.main import app

client = TestClient(app)

OPT = {"countries": ["MY"], "category": "beauty", "budget": 3000}
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _account(email=None):
    email = email or f"bill_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    return h, email


@pytest.fixture
def admin(monkeypatch):
    h, email = _account()
    monkeypatch.setenv("ADMIN_EMAILS", f"someone@else.com, {email}")
    return h


def _pay(h, plan="pro", months=1, ref="TNG123456", proof=True):
    files = {"proof": ("r.png", PNG, "image/png")} if proof else None
    return client.post("/billing/requests", headers=h, files=files,
                       data={"plan": plan, "months": str(months), "reference": ref})


# ── Limits ────────────────────────────────────────────────────────
def test_new_account_is_free_in_myr():
    h, _ = _account()
    s = client.get("/billing/status", headers=h).json()
    assert s["plan"] == "free" and s["currency"] == "MYR"
    assert s["limits"]["optimizations_per_month"] == 10
    plans = client.get("/billing/plans").json()
    assert plans["plans"]["pro"]["price_myr"] == 99 and plans["month_options"] == [1, 3, 12]


def test_free_optimization_quota_enforced(monkeypatch):
    monkeypatch.setitem(billing.PLANS["free"], "optimizations_per_month", 2)
    h, _ = _account()
    assert client.post("/optimize", json=OPT, headers=h).status_code == 200
    assert client.post("/optimize", json=OPT, headers=h).status_code == 200
    r = client.post("/optimize", json=OPT, headers=h)
    assert r.status_code == 402 and r.json()["detail"]["upgrade"] is True


def test_free_cannot_export_or_import():
    h, _ = _account()
    assert client.get("/kols/export", headers=h).status_code == 402
    files = {"file": ("k.csv", b"name,country\nA,MY\n", "text/csv")}
    assert client.post("/kols/import", files=files, headers=h).status_code == 402


def test_free_campaign_cap(monkeypatch):
    monkeypatch.setitem(billing.PLANS["free"], "max_campaigns", 1)
    h, _ = _account()
    camp = {"name": "c", "countries": ["MY"], "category": "beauty", "budget": 1000,
            "best_algorithm": "Greedy Ranking", "selected_kols": [], "predicted_gmv": 0, "total_cost": 0}
    assert client.post("/campaigns", json=camp, headers=h).status_code == 200
    assert client.post("/campaigns", json=camp, headers=h).status_code == 402


# ── Payment submission ────────────────────────────────────────────
def test_submit_payment_validates_input():
    h, _ = _account()
    assert _pay(h, plan="gold").status_code == 422
    assert _pay(h, months=2).status_code == 422
    assert _pay(h, ref="x").status_code == 422
    bad = client.post("/billing/requests", headers=h, data={"plan": "pro", "months": "1", "reference": "REF12345"},
                      files={"proof": ("r.pdf", b"%PDF", "application/pdf")})
    assert bad.status_code == 422


def test_one_pending_request_at_a_time_and_cancel():
    h, _ = _account()
    r = _pay(h, months=3)
    assert r.status_code == 200 and r.json()["amount_myr"] == 297
    assert _pay(h).status_code == 409
    s = client.get("/billing/status", headers=h).json()
    assert s["pending_request"]["status"] == "pending" and s["plan"] == "free"
    assert client.delete("/billing/requests/pending", headers=h).status_code == 200
    assert _pay(h).status_code == 200


# ── Admin review ──────────────────────────────────────────────────
def test_non_admin_cannot_review(admin):
    h, _ = _account()
    assert client.get("/admin/payments", headers=h).status_code == 403


def test_approve_activates_plan_with_expiry(admin):
    h, email = _account()
    req = _pay(h, months=1).json()
    listed = client.get("/admin/payments", headers=admin).json()["payments"]
    mine = next(p for p in listed if p["id"] == req["id"])
    assert mine["email"] == email and mine["has_proof"] and "proof" not in mine
    assert client.get(f"/admin/payments/{req['id']}/proof", headers=admin).content == PNG

    r = client.post(f"/admin/payments/{req['id']}/approve", headers=admin)
    assert r.status_code == 200
    s = client.get("/billing/status", headers=h).json()
    assert s["plan"] == "pro"
    exp = datetime.fromisoformat(s["plan_expires_at"])
    assert timedelta(days=29) < exp - datetime.now(timezone.utc) <= timedelta(days=30)
    assert client.get("/kols/export", headers=h).status_code == 200
    assert any(m["to"] == email and "active" in m["subject"] for m in mailer.outbox)
    # can't decide twice
    assert client.post(f"/admin/payments/{req['id']}/approve", headers=admin).status_code == 409


def test_renewal_stacks_on_remaining_time(admin):
    h, _ = _account()
    first = _pay(h, months=1).json()
    client.post(f"/admin/payments/{first['id']}/approve", headers=admin)
    second = _pay(h, months=3, ref="TNG999999").json()
    client.post(f"/admin/payments/{second['id']}/approve", headers=admin)
    exp = datetime.fromisoformat(client.get("/billing/status", headers=h).json()["plan_expires_at"])
    assert exp - datetime.now(timezone.utc) > timedelta(days=119)


def test_reject_keeps_free_and_records_reason(admin):
    h, _ = _account()
    req = _pay(h).json()
    r = client.post(f"/admin/payments/{req['id']}/reject", headers=admin, data={"reason": "Amount mismatch"})
    assert r.status_code == 200
    s = client.get("/billing/status", headers=h).json()
    assert s["plan"] == "free" and s["last_decision"]["reject_reason"] == "Amount mismatch"


def test_expired_plan_falls_back_to_free(admin):
    h, email = _account()
    req = _pay(h).json()
    client.post(f"/admin/payments/{req['id']}/approve", headers=admin)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE users SET plan_expires_at=? WHERE email=?", (past, email))
    assert client.get("/billing/status", headers=h).json()["plan"] == "free"
    assert client.get("/kols/export", headers=h).status_code == 402


def test_admin_uploads_qr(admin, tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_QR_PATH", str(tmp_path / "qr.png"))
    assert client.get("/billing/qr").status_code == 404
    r = client.post("/admin/payment-qr", headers=admin, files={"file": ("qr.png", PNG, "image/png")})
    assert r.status_code == 200
    assert client.get("/billing/qr").content == PNG
    assert client.get("/billing/plans").json()["qr_available"] is True
