"""Launch-readiness tests: database store, legacy import, rate limiting, password reset."""
import json
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from backend import config, db, mailer, tenancy
from backend.main import app
from backend.ratelimit import RateLimiter, auth_limiter

client = TestClient(app)


def _email():
    return f"sec_{uuid.uuid4().hex[:10]}@example.com"


def _register(email=None, password="secret123"):
    email = email or _email()
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return email, r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ── Password rules ────────────────────────────────────────────────
@pytest.mark.parametrize("pw", ["short1", "allletters", "1234567890"])
def test_weak_passwords_rejected(pw):
    r = client.post("/auth/register", json={"email": _email(), "password": pw})
    assert r.status_code == 422


def test_me_reports_plan():
    _, token = _register()
    assert client.get("/auth/me", headers=_auth(token)).json()["plan"] == "free"


# ── Forgot / reset password ───────────────────────────────────────
def _last_reset_token(email):
    msg = [m for m in mailer.outbox if m["to"] == email][-1]
    return re.search(r"reset_token=([\w-]+)", msg["body"]).group(1)


def test_forgot_password_unknown_email_gives_same_answer():
    before = len(mailer.outbox)
    r = client.post("/auth/forgot-password", json={"email": _email()})
    assert r.status_code == 200
    assert len(mailer.outbox) == before  # nothing sent, but caller can't tell


def test_reset_password_flow_and_old_token_revoked():
    email, old_token = _register(password="oldpass123")
    assert client.post("/auth/forgot-password", json={"email": email}).status_code == 200
    token = _last_reset_token(email)

    # Tokens issued in the same second as the reset survive; make the old one strictly older.
    time.sleep(1.05)

    r = client.post("/auth/reset-password", json={"token": token, "password": "newpass456"})
    assert r.status_code == 200, r.text
    assert client.get("/auth/me", headers=_auth(old_token)).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": "oldpass123"}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": "newpass456"}).status_code == 200
    # single use
    again = client.post("/auth/reset-password", json={"token": token, "password": "another789"})
    assert again.status_code == 400


def test_reset_password_rejects_bogus_token():
    r = client.post("/auth/reset-password", json={"token": "nope", "password": "newpass456"})
    assert r.status_code == 400


def test_change_password_returns_working_token():
    email, token = _register(password="firstpass1")
    bad = client.post("/auth/change-password", headers=_auth(token),
                      json={"current_password": "wrong", "new_password": "secondpass2"})
    assert bad.status_code == 401
    r = client.post("/auth/change-password", headers=_auth(token),
                    json={"current_password": "firstpass1", "new_password": "secondpass2"})
    assert r.status_code == 200
    assert client.get("/auth/me", headers=_auth(r.json()["token"])).json()["email"] == email


# ── Rate limiting ─────────────────────────────────────────────────
def test_login_is_rate_limited(monkeypatch):
    limiter = RateLimiter("3/60")
    monkeypatch.setattr(auth_limiter, "max_hits", limiter.max_hits)
    monkeypatch.setattr(auth_limiter, "window", limiter.window)
    auth_limiter.reset()
    try:
        codes = [client.post("/auth/login", json={"email": "x@example.com", "password": "nope1234"}).status_code
                 for _ in range(4)]
        assert codes == [401, 401, 401, 429]
    finally:
        auth_limiter.reset()


# ── Production config guard ───────────────────────────────────────
def test_production_refuses_default_secret(monkeypatch):
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "JWT_SECRET", config.DEV_SECRET)
    with pytest.raises(RuntimeError):
        config.validate()
    monkeypatch.setattr(config, "JWT_SECRET", "x" * 40)
    monkeypatch.delenv("DEFAULT_TENANT", raising=False)
    config.validate()


# ── Legacy JSON import ────────────────────────────────────────────
def test_legacy_json_files_are_imported(tmp_path, monkeypatch):
    tdir = tmp_path / "tenants" / "legacy1"
    tdir.mkdir(parents=True)
    (tdir / "sample_kols.json").write_text(json.dumps([{"id": 7, "name": "Old"}]))
    (tdir / "campaigns.json").write_text("[]")
    (tmp_path / "users.json").write_text(json.dumps([{
        "id": 1, "email": "legacy@example.com", "password_hash": "x",
        "tenant_id": "legacy1", "created_at": "2025-01-01T00:00:00+00:00",
    }]))
    monkeypatch.setattr(tenancy, "TENANTS_DIR", str(tmp_path / "tenants"))
    monkeypatch.setattr(tenancy, "USERS_PATH", str(tmp_path / "users.json"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "legacy.db"))
    monkeypatch.setattr(db, "_initialized_for", None)
    try:
        db.init()
        assert db.read_doc("sample_kols", tenant_id="legacy1") == [{"id": 7, "name": "Old"}]
        with db.connect() as conn:
            assert conn.execute("SELECT tenant_id FROM users WHERE email='legacy@example.com'").fetchone()[0] == "legacy1"
    finally:
        monkeypatch.undo()
        db._initialized_for = None
        db.init()
