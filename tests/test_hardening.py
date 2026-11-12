"""
Launch-hardening tests: the guards that must hold on a public deployment.

conftest disables throttling and enables self-serve plan switching for the
rest of the suite; the tests here switch the real production defaults back on.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import auth, ratelimit, repo, settings

client = TestClient(app)


def _email():
    return f"hard_{uuid.uuid4().hex[:10]}@example.com"


def _register(password="secret123"):
    email = _email()
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return email, {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def throttled(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    ratelimit.reset()
    yield
    ratelimit.reset()


# ── Billing: paid plans cannot be self-activated ──────────────────
def test_paid_plan_cannot_be_self_activated(monkeypatch):
    monkeypatch.setattr(settings, "BILLING_SELF_SERVE", False)
    _, headers = _register()
    for plan in ("pro", "team"):
        r = client.post("/billing/checkout", json={"plan": plan}, headers=headers)
        assert r.status_code == 402, r.text
    info = client.get("/billing/info", headers=headers).json()
    assert info["plan"] == "free"
    assert info["self_serve"] is False


def test_operator_grant_then_free_downgrade(monkeypatch):
    monkeypatch.setattr(settings, "BILLING_SELF_SERVE", False)
    monkeypatch.setattr(settings, "ADMIN_KEY", "operator-key-for-tests")
    email, headers = _register()

    denied = client.post("/billing/grant", json={"email": email, "plan": "pro"},
                         headers={"X-Admin-Key": "wrong"})
    assert denied.status_code == 403

    ok = client.post("/billing/grant", json={"email": email, "plan": "pro"},
                     headers={"X-Admin-Key": "operator-key-for-tests"})
    assert ok.status_code == 200
    assert client.get("/billing/info", headers=headers).json()["plan"] == "pro"

    # Downgrading to Free needs no payment, so it stays self-serve.
    assert client.post("/billing/checkout", json={"plan": "free"}, headers=headers).status_code == 200
    assert client.get("/billing/info", headers=headers).json()["plan"] == "free"


def test_admin_endpoints_inert_without_configured_key(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "")
    assert client.get("/admin/users", headers={"X-Admin-Key": ""}).status_code == 403
    assert client.get("/admin/users").status_code == 403


# ── Account lifecycle ─────────────────────────────────────────────
def test_change_password_signs_out_old_sessions():
    email, old_headers = _register("first-pass-1")

    wrong = client.post("/auth/change-password", headers=old_headers,
                        json={"current_password": "nope-nope", "new_password": "second-pass-2"})
    assert wrong.status_code == 403
    weak = client.post("/auth/change-password", headers=old_headers,
                       json={"current_password": "first-pass-1", "new_password": "short"})
    assert weak.status_code == 422

    r = client.post("/auth/change-password", headers=old_headers,
                    json={"current_password": "first-pass-1", "new_password": "second-pass-2"})
    assert r.status_code == 200, r.text
    new_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    assert client.get("/auth/me", headers=old_headers).status_code == 401
    assert client.get("/auth/me", headers=new_headers).status_code == 200
    assert client.post("/auth/login", json={"email": email, "password": "first-pass-1"}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": "second-pass-2"}).status_code == 200


def test_delete_account_removes_user_and_data():
    email, headers = _register("delete-me-1")
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    assert repo.kols_count(tenant_id) > 0

    assert client.post("/auth/delete-account", headers=headers,
                       json={"password": "wrong-one"}).status_code == 403
    r = client.post("/auth/delete-account", headers=headers, json={"password": "delete-me-1"})
    assert r.status_code == 200

    assert client.get("/kols", headers=headers).status_code == 401      # token is dead
    assert repo.user_find(email) is None
    assert repo.tenant_exists(tenant_id) is False
    assert client.post("/auth/login", json={"email": email, "password": "delete-me-1"}).status_code == 401


def test_operator_password_reset(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "operator-key-for-tests")
    email, headers = _register("forgotten-1")
    r = client.post("/admin/reset-password", json={"email": email},
                    headers={"X-Admin-Key": "operator-key-for-tests"})
    assert r.status_code == 200, r.text
    temp = r.json()["temporary_password"]
    assert client.get("/auth/me", headers=headers).status_code == 401   # old session ended
    assert client.post("/auth/login", json={"email": email, "password": "forgotten-1"}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": temp}).status_code == 200


def test_register_rejects_markup_and_reserved_addresses():
    for bad in ("<svg/onload=alert(1)>@x.io", "a@b", "who@demo.tiko", "x" * 250 + "@example.com"):
        r = client.post("/auth/register", json={"email": bad, "password": "secret123"})
        assert r.status_code == 422, bad
    too_long = client.post("/auth/register", json={"email": _email(), "password": "p" * 200})
    assert too_long.status_code == 422


def test_demo_session_cannot_manage_an_account():
    r = client.post("/auth/demo")
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/auth/me", headers=headers).json()["demo"] is True
    r = client.post("/auth/delete-account", headers=headers, json={"password": "whatever"})
    assert r.status_code == 403


def test_expired_demo_accounts_are_swept():
    r = client.post("/auth/demo")
    old_email = r.json()["email"]
    old_tenant = repo.user_find(old_email)["tenant_id"]
    # Age the account past the retention window.
    stale = (datetime.now(timezone.utc) - auth._DEMO_RETENTION - timedelta(hours=1)).isoformat()
    from backend.db import SessionLocal, User
    with SessionLocal.begin() as s:
        s.query(User).filter(User.email == old_email).update({"created_at": stale})

    fresh = client.post("/auth/demo").json()["email"]      # triggers the sweep
    assert repo.user_find(old_email) is None
    assert repo.tenant_exists(old_tenant) is False
    assert repo.user_find(fresh) is not None


# ── Throttling ────────────────────────────────────────────────────
def test_login_is_throttled_per_email(throttled):
    email, _ = _register()
    codes = [client.post("/auth/login", json={"email": email, "password": "bad-guess"}).status_code
             for _ in range(12)]
    assert codes[:10] == [401] * 10
    assert codes[10:] == [429, 429]
    # Even the right password is refused while the lock is on.
    assert client.post("/auth/login", json={"email": email, "password": "secret123"}).status_code == 429


def test_demo_is_throttled_per_ip(throttled):
    codes = [client.post("/auth/demo").status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


def test_bad_admin_key_is_throttled(throttled, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "operator-key-for-tests")
    codes = [client.get("/admin/overview", headers={"X-Admin-Key": f"guess{i}"}).status_code
             for i in range(11)]
    assert codes[:10] == [403] * 10 and codes[10] == 429
    # The right key is unaffected by other people's failures.
    assert client.get("/admin/overview",
                      headers={"X-Admin-Key": "operator-key-for-tests"}).status_code == 200


# ── Request hygiene ───────────────────────────────────────────────
def test_simulate_scale_requires_auth():
    r = client.post("/simulate-scale", json={"budget": 5000},
                    headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


def test_security_headers_and_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["database"] == "ok"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers.get("X-Request-ID")


def test_oversized_request_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)
    big = b"name,country\n" + b"x" * 4096
    r = client.post("/kols/import", files={"file": ("big.csv", big, "text/csv")})
    assert r.status_code == 413


def test_absurd_budget_is_rejected():
    r = client.post("/optimize", json={"budget": 1e15, "category": "beauty"})
    assert r.status_code == 422


def test_validation_message_lists_categories():
    r = client.post("/optimize", json={"budget": 1000, "category": "nope"})
    assert r.status_code == 422
    assert "beauty" in r.json()["detail"]


def test_interest_is_stored_for_the_operator(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "operator-key-for-tests")
    email = _email()
    r = client.post("/api/connections/interest",
                    json={"email": email, "integration_type": "tiktok_shop"})
    assert r.status_code == 200
    listed = client.get("/admin/interests",
                        headers={"X-Admin-Key": "operator-key-for-tests"}).json()["interests"]
    assert listed[0]["email"] == email
    bad = client.post("/api/connections/interest",
                      json={"email": "<b>@x.io", "integration_type": "tiktok_shop"})
    assert bad.status_code == 422


# ── Production config guard ───────────────────────────────────────
def test_production_refuses_unsafe_config(monkeypatch):
    monkeypatch.setattr(settings, "IS_PRODUCTION", True)
    monkeypatch.setattr(settings, "JWT_SECRET", settings._DEV_JWT_SECRET)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ["*"])
    monkeypatch.setattr(settings, "BILLING_SELF_SERVE", True)
    with pytest.raises(RuntimeError) as exc:
        settings.validate()
    msg = str(exc.value)
    assert "JWT_SECRET" in msg and "CORS_ORIGINS" in msg and "BILLING_SELF_SERVE" in msg
    assert "DEFAULT_TENANT" in msg          # the test env sets it

    monkeypatch.setattr(settings, "JWT_SECRET", "s" * 48)
    monkeypatch.setattr(settings, "CORS_ORIGINS", [])
    monkeypatch.setattr(settings, "BILLING_SELF_SERVE", False)
    monkeypatch.setattr(settings, "ADMIN_KEY", "k" * 32)
    monkeypatch.delenv("DEFAULT_TENANT")
    settings.validate()                      # a clean config boots
