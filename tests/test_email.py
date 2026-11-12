"""
Email flows: verification, password recovery, and notifications.

No SMTP server is involved — mailer._deliver is replaced with a capture, so
the tests read the exact message a merchant would receive and follow its link.
"""
import os
import re
import sys
import uuid

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import mailer, repo, settings

client = TestClient(app)


@pytest.fixture
def outbox(monkeypatch):
    """Turn email on and capture everything that would be sent."""
    sent = []
    monkeypatch.setattr(mailer, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(mailer, "SMTP_FROM", "tiko <no-reply@tiko.test>")
    monkeypatch.setattr(mailer, "APP_BASE_URL", "https://app.tiko.test")
    monkeypatch.setattr(mailer, "_deliver", sent.append)
    return sent


def _email():
    return f"mail_{uuid.uuid4().hex[:10]}@example.com"


def _register(password="secret123", lang=None):
    email = _email()
    headers = {"X-Lang": lang} if lang else {}
    r = client.post("/auth/register", json={"email": email, "password": password}, headers=headers)
    assert r.status_code == 200, r.text
    return email, {"Authorization": f"Bearer {r.json()['token']}"}, r.json()


def _text(msg) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def _token(msg, kind: str) -> str:
    m = re.search(rf"https://app\.tiko\.test/#{kind}=([\w\-.]+)", _text(msg))
    assert m, _text(msg)
    return m.group(1)


# ── Verification ──────────────────────────────────────────────────
def test_register_sends_verification_and_link_verifies(outbox):
    email, headers, body = _register()
    assert body["verification_sent"] is True and body["email_verified"] is False
    assert len(outbox) == 1
    msg = outbox[0]
    assert msg["To"] == email and "Verify" in msg["Subject"]
    assert msg.get_body(preferencelist=("html",)) is not None        # multipart: text + html

    me = client.get("/auth/me", headers=headers).json()
    assert me["email_verified"] is False and me["email_enabled"] is True

    r = client.post("/auth/verify-email", json={"token": _token(msg, "verify")})
    assert r.status_code == 200 and r.json()["email"] == email
    assert client.get("/auth/me", headers=headers).json()["email_verified"] is True


def test_emails_follow_the_ui_language(outbox):
    _register(lang="zh-CN")
    assert "验证" in outbox[0]["Subject"]


def test_resend_verification(outbox):
    _, headers, _ = _register()
    r = client.post("/auth/resend-verification", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert len(outbox) == 2
    client.post("/auth/verify-email", json={"token": _token(outbox[1], "verify")})
    assert client.post("/auth/resend-verification", headers=headers).json()["status"] == "already_verified"
    assert len(outbox) == 2


def test_verify_rejects_garbage_and_wrong_purpose(outbox):
    assert client.post("/auth/verify-email", json={"token": "nope"}).status_code == 400
    # A login token must not pass as a verification link …
    _, headers, body = _register()
    assert client.post("/auth/verify-email", json={"token": body["token"]}).status_code == 400
    # … and an emailed link must not pass as a login token.
    link_token = _token(outbox[-1], "verify")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {link_token}"}).status_code == 401


# ── Password recovery ─────────────────────────────────────────────
def test_forgot_password_full_flow(outbox):
    email, old_headers, _ = _register("original-pass-1")
    outbox.clear()

    assert client.post("/auth/forgot-password", json={"email": email}).status_code == 200
    assert len(outbox) == 1 and outbox[0]["To"] == email
    token = _token(outbox[0], "reset")

    weak = client.post("/auth/reset-password", json={"token": token, "new_password": "short"})
    assert weak.status_code == 422

    r = client.post("/auth/reset-password", json={"token": token, "new_password": "brand-new-pass-2"})
    assert r.status_code == 200, r.text
    new_headers = {"Authorization": f"Bearer {r.json()['token']}"}

    assert client.get("/auth/me", headers=old_headers).status_code == 401       # old sessions end
    me = client.get("/auth/me", headers=new_headers).json()
    assert me["email"] == email and me["email_verified"] is True                 # link proved ownership
    assert client.post("/auth/login", json={"email": email, "password": "original-pass-1"}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": "brand-new-pass-2"}).status_code == 200
    assert "changed" in outbox[-1]["Subject"]                                    # security notice

    # The link is single-use.
    again = client.post("/auth/reset-password", json={"token": token, "new_password": "third-pass-3"})
    assert again.status_code == 400


def test_forgot_password_does_not_reveal_accounts(outbox):
    known, _, _ = _register()
    outbox.clear()
    a = client.post("/auth/forgot-password", json={"email": known})
    b = client.post("/auth/forgot-password", json={"email": _email()})
    c = client.post("/auth/forgot-password", json={"email": "not-an-email"})
    assert a.status_code == b.status_code == c.status_code == 200
    assert a.json() == b.json() == c.json()
    assert len(outbox) == 1                      # only the real account got mail


def test_reset_links_use_configured_origin_not_host_header(outbox):
    email, _, _ = _register()
    outbox.clear()
    client.post("/auth/forgot-password", json={"email": email}, headers={"Host": "evil.example"})
    text = _text(outbox[0])
    assert "https://app.tiko.test/#reset=" in text and "evil.example" not in text


def test_forgot_password_email_is_throttled_per_address(outbox, monkeypatch):
    from backend import ratelimit
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    ratelimit.reset()
    email, _, _ = _register()
    outbox.clear()
    codes = [client.post("/auth/forgot-password", json={"email": email}).status_code for _ in range(5)]
    assert codes == [200] * 5                    # never a tell-tale 429 for a real address
    assert len(outbox) == 3                      # but only three mails an hour
    ratelimit.reset()


# ── Notifications ─────────────────────────────────────────────────
def test_password_change_sends_security_notice(outbox):
    email, headers, _ = _register("first-pass-1")
    outbox.clear()
    r = client.post("/auth/change-password", headers=headers,
                    json={"current_password": "first-pass-1", "new_password": "second-pass-2"})
    assert r.status_code == 200
    assert len(outbox) == 1 and outbox[0]["To"] == email


def test_plan_activation_emails_the_merchant(outbox, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "operator-key-for-tests")
    email, _, _ = _register()
    outbox.clear()
    r = client.post("/admin/plan", json={"email": email, "plan": "pro"},
                    headers={"X-Admin-Key": "operator-key-for-tests"})
    assert r.status_code == 200
    assert len(outbox) == 1 and outbox[0]["To"] == email and "Pro" in outbox[0]["Subject"]


# ── Email switched off ────────────────────────────────────────────
def test_everything_degrades_cleanly_without_smtp(monkeypatch):
    monkeypatch.setattr(mailer, "SMTP_HOST", "")
    assert client.get("/app-config").json()["email_enabled"] is False
    email, headers, body = _register()
    assert body["verification_sent"] is False
    assert client.post("/auth/forgot-password", json={"email": email}).status_code == 503
    assert client.post("/auth/resend-verification", headers=headers).status_code == 503


def test_delivery_failure_never_breaks_signup(outbox, monkeypatch):
    def boom(_msg):
        raise OSError("relay down")
    monkeypatch.setattr(mailer, "_deliver", boom)
    email, headers, _ = _register()
    assert client.get("/auth/me", headers=headers).status_code == 200
    assert repo.user_find(email) is not None


def test_production_requires_base_url_with_smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.delenv("APP_BASE_URL", raising=False)
    assert any("APP_BASE_URL" in p for p in settings.production_problems())
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.com")
    assert not any("APP_BASE_URL" in p for p in settings.production_problems())
