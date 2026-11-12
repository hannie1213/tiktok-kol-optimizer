"""
Operator console (admin) endpoint tests.

The admin key is injected directly into backend.admin's module global so the
suite does not depend on environment ordering; every endpoint must refuse
requests without the exact key.
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import admin, billing, settings

client = TestClient(app)

KEY = "test-admin-key"
settings.ADMIN_KEY = KEY
H = {"X-Admin-Key": KEY}


def _register(prefix="admin_t"):
    email = f"{prefix}_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    assert r.status_code == 200, r.text
    return email


# ── Auth gate ─────────────────────────────────────────────────────

def test_admin_requires_key():
    for path in ("/admin/overview", "/admin/users", "/admin/interests"):
        assert client.get(path).status_code == 403
        assert client.get(path, headers={"X-Admin-Key": "wrong"}).status_code == 403
    assert client.post("/admin/plan", json={"email": "x@y.z", "plan": "pro"}).status_code == 403


# ── Overview + users ──────────────────────────────────────────────

def test_users_lists_new_account_with_stats():
    email = _register()
    d = client.get("/admin/users", headers=H).json()
    row = next(u for u in d["users"] if u["email"] == email)
    assert row["plan"] == "free"
    assert row["creators"] > 0          # seeded from the template library
    assert row["optimize_runs"] == 0
    assert row["campaigns"] == 0
    assert row["demo"] is False
    assert set(d["plans"]) == set(billing.PLANS)


def test_overview_totals_match_user_list():
    _register()
    ov = client.get("/admin/overview", headers=H).json()
    users = client.get("/admin/users", headers=H).json()["users"]
    assert ov["total_users"] == len(users)
    assert ov["demo_users"] == sum(u["demo"] for u in users)
    assert ov["real_users"] == ov["total_users"] - ov["demo_users"]
    assert ov["optimize_runs_month"] == sum(u["optimize_runs"] for u in users)


# ── Plan fulfillment + MRR ────────────────────────────────────────

def test_plan_change_reflects_in_users_and_mrr():
    email = _register()
    before = client.get("/admin/overview", headers=H).json()

    r = client.post("/admin/plan", json={"email": email, "plan": "pro"}, headers=H)
    assert r.status_code == 200
    assert r.json()["plan_name"] == "Pro"

    row = next(u for u in client.get("/admin/users", headers=H).json()["users"]
               if u["email"] == email)
    assert row["plan"] == "pro"
    after = client.get("/admin/overview", headers=H).json()
    assert after["paid_users"] == before["paid_users"] + 1
    assert after["mrr_usd"] == before["mrr_usd"] + billing.PLANS["pro"]["price_usd"]


def test_plan_change_rejects_unknown_plan_and_user():
    email = _register()
    assert client.post("/admin/plan", json={"email": email, "plan": "gold"},
                       headers=H).status_code == 422
    assert client.post("/admin/plan", json={"email": "nobody@example.com", "plan": "pro"},
                       headers=H).status_code == 404


# ── Deletion ──────────────────────────────────────────────────────

def test_delete_user_removes_account():
    email = _register()
    r = client.post("/admin/delete-user", json={"email": email}, headers=H)
    assert r.status_code == 200
    emails = [u["email"] for u in client.get("/admin/users", headers=H).json()["users"]]
    assert email not in emails
    # Deleting again → 404
    assert client.post("/admin/delete-user", json={"email": email},
                       headers=H).status_code == 404


def test_cleanup_demos_only_touches_demo_accounts():
    keeper = _register()
    r = client.post("/auth/demo")
    assert r.status_code == 200, r.text
    demo_email = r.json()["email"]

    d = client.post("/admin/cleanup-demos", headers=H).json()
    assert d["removed"] >= 1
    emails = [u["email"] for u in client.get("/admin/users", headers=H).json()["users"]]
    assert demo_email not in emails
    assert keeper in emails
