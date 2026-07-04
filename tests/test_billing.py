"""
Subscription plan & quota enforcement tests.

Each test registers a fresh merchant (free tier) so the DEFAULT_TENANT
fallback tenant — pinned to the unlimited Team plan in conftest — is never
metered.
"""
import os
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import billing, repo

client = TestClient(app)


def _register():
    email = f"biller_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    return headers, tenant_id


# ── /billing/info ─────────────────────────────────────────────────

def test_new_tenant_starts_on_free_plan():
    headers, _ = _register()
    r = client.get("/billing/info", headers=headers)
    assert r.status_code == 200
    d = r.json()
    assert d["plan"] == "free"
    assert d["plan_name"] == "Free"
    assert set(d["plans"].keys()) == {"free", "pro", "team"}
    assert d["limits"]["monthly_optimizations"] == billing.PLANS["free"]["monthly_optimizations"]
    assert d["usage"]["optimize_runs"] == 0
    # Optimization runs are the metered axis; the library is not capped.
    assert d["limits"]["max_creators"] is None
    assert d["usage"]["creators"] > 0


def test_billing_requires_auth():
    r = client.get("/billing/info", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


# ── Optimization quota ────────────────────────────────────────────

def test_optimize_metered_and_blocked_at_quota():
    headers, tenant_id = _register()
    payload = {"budget": 1000, "countries": ["MY"], "category": "beauty"}

    r = client.post("/optimize", json=payload, headers=headers)
    assert r.status_code == 200
    assert client.get("/billing/info", headers=headers).json()["usage"]["optimize_runs"] == 1

    # Burn the rest of the month's quota directly, then the API must refuse.
    limit = billing.PLANS["free"]["monthly_optimizations"]
    for _ in range(limit - 1):
        repo.optimize_consume(tenant_id, billing._month())
    r = client.post("/optimize", json=payload, headers=headers)
    assert r.status_code == 402
    assert "limit" in r.json()["detail"].lower()

    # Upgrading lifts the cap immediately.
    r = client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
    assert r.status_code == 200
    r = client.post("/optimize", json=payload, headers=headers)
    assert r.status_code == 200


def test_optimize_plans_not_metered():
    """/optimize-plans rides along with /optimize in the UI — it must not
    double-charge the quota."""
    headers, _ = _register()
    payload = {"budget": 1000, "countries": ["MY"], "category": "beauty"}
    r = client.post("/optimize-plans", json=payload, headers=headers)
    assert r.status_code == 200
    assert client.get("/billing/info", headers=headers).json()["usage"]["optimize_runs"] == 0


# ── Creator library cap ───────────────────────────────────────────

NEW_KOL = {"name": "Cap Test", "country": "MY", "category": "beauty",
           "followers": 50000, "engagement_rate": 0.07}


def test_creator_cap_blocks_add_and_upgrade_unblocks(monkeypatch):
    headers, tenant_id = _register()
    # Shrink the free cap to the current library size so the next add trips it.
    current = repo.kols_count(tenant_id)
    monkeypatch.setitem(billing.PLANS["free"], "max_creators", current)

    r = client.post("/kols/add", json=NEW_KOL, headers=headers)
    assert r.status_code == 402
    assert "limit" in r.json()["detail"].lower()

    r = client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
    assert r.status_code == 200
    r = client.post("/kols/add", json=NEW_KOL, headers=headers)
    assert r.status_code == 200


# ── /billing/checkout ─────────────────────────────────────────────

def test_checkout_validates_plan():
    headers, _ = _register()
    assert client.post("/billing/checkout", json={"plan": "platinum"}, headers=headers).status_code == 422
    assert client.post("/billing/checkout", json={"plan": "free"}, headers=headers).status_code == 409  # already on it


def test_downgrade_blocked_when_library_exceeds_new_cap(monkeypatch):
    headers, tenant_id = _register()
    r = client.post("/billing/checkout", json={"plan": "team"}, headers=headers)
    assert r.status_code == 200
    # Pretend the free cap is below the current library size.
    monkeypatch.setitem(billing.PLANS["free"], "max_creators", repo.kols_count(tenant_id) - 1)
    r = client.post("/billing/checkout", json={"plan": "free"}, headers=headers)
    assert r.status_code == 409
    assert "creators" in r.json()["detail"].lower()
    # Still on team.
    assert client.get("/billing/info", headers=headers).json()["plan"] == "team"
