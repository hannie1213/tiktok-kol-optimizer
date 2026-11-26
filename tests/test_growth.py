"""
Free Pro trial, referral rewards, payment screenshots, Telegram alerts and
the annual price.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import aivideo, billing, growth, notify, payments, ratelimit, repo, settings

client = TestClient(app)
ADMIN = {"X-Admin-Key": "operator-key-for-tests"}
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", "operator-key-for-tests")
    monkeypatch.setattr(settings, "BILLING_SELF_SERVE", False)
    ratelimit.reset()
    if not payments.enabled():
        payments.store_qr(PNG)


def _register(ref=None):
    email = f"grow_{uuid.uuid4().hex[:10]}@example.com"
    body = {"email": email, "password": "secret123"}
    if ref:
        body["ref"] = ref
    r = client.post("/auth/register", json=body)
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    return email, h, client.get("/auth/me", headers=h).json()["tenant_id"]


def _pay(h, plan="pro", months=1):
    o = client.post("/billing/orders", json={"plan": plan, "months": months}, headers=h).json()
    ref = "TNG" + uuid.uuid4().hex[:10].upper()
    assert client.post(f"/billing/orders/{o['order_no']}/submit", json={"reference": ref}, headers=h).status_code == 200
    r = client.post(f"/admin/orders/{o['order_no']}/confirm", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


# ── Trial ─────────────────────────────────────────────────────────
def test_trial_once_per_workspace_then_lapses_to_free():
    _, h, tenant = _register()
    info = client.get("/billing/info", headers=h).json()
    assert info["trial"] == {"days": 7, "available": True, "active": False, "ends_at": None}

    r = client.post("/billing/trial", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True and r.json()["available"] is False
    info = client.get("/billing/info", headers=h).json()
    assert info["plan"] == "pro" and info["expires_at"] == r.json()["ends_at"]
    ends = datetime.fromisoformat(info["expires_at"])
    assert timedelta(days=6, hours=23) < ends - datetime.now(timezone.utc) <= timedelta(days=7)

    assert client.post("/billing/trial", headers=h).status_code == 409       # only once
    # AI clips cost real money: a trial keeps the Free allowance.
    assert aivideo._limit(tenant) == billing.PLANS["free"]["monthly_ai_videos"]

    # When the week is over the plan is Free again, and no second trial.
    repo.plan_set(tenant, "pro", "", expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    info = client.get("/billing/info", headers=h).json()
    assert info["plan"] == "free" and info["trial"]["available"] is False
    assert client.post("/billing/trial", headers=h).status_code == 409


def test_no_trial_for_paying_or_demo_workspaces():
    _, h, _ = _register()
    _pay(h)
    assert client.get("/billing/info", headers=h).json()["trial"]["available"] is False
    assert client.post("/billing/trial", headers=h).status_code == 409

    demo = client.post("/auth/demo").json()
    dh = {"Authorization": f"Bearer {demo['token']}"}
    assert client.post("/billing/trial", headers=dh).status_code == 403


# ── Referrals ─────────────────────────────────────────────────────
def test_referral_rewards_both_sides_once_after_a_confirmed_payment():
    _, ha, ta = _register()
    me = client.get("/billing/referral", headers=ha).json()
    code = me["code"]
    assert me["path"] == f"/?ref={code}" and me["invited"] == 0 and me["reward_days"] == 30
    assert client.get("/billing/referral", headers=ha).json()["code"] == code      # stable

    _, hb, tb = _register(ref=code.lower())                                       # case-insensitive
    assert client.get("/billing/referral", headers=ha).json()["invited"] == 1
    assert repo.plan_get(ta) == "free"                                            # signing up earns nothing

    r = _pay(hb)                                                                  # B pays one month of Pro
    assert r["referral_reward"] is True
    b = repo.plan_info(tb)
    assert b["plan"] == "pro"
    days_b = (datetime.fromisoformat(b["expires_at"]) - datetime.now(timezone.utc)).days
    assert 58 <= days_b <= 60                                                     # 30 paid + 30 reward
    a = repo.plan_info(ta)
    assert a["plan"] == "pro"                                                     # Free referrer → Pro for 30 days
    assert 29 <= (datetime.fromisoformat(a["expires_at"]) - datetime.now(timezone.utc)).days <= 30
    assert client.get("/billing/referral", headers=ha).json()["rewarded"] == 1

    assert _pay(hb)["referral_reward"] is False                                   # only the first payment


def test_unknown_or_own_codes_are_ignored():
    _, h, t = _register(ref="NOPE99")
    assert client.get("/billing/info", headers=h).json()["plan"] == "free"
    code = client.get("/billing/referral", headers=h).json()["code"]
    assert growth.record_signup(t, code) is False                                 # can't refer yourself
    assert client.post("/auth/register", json={"email": f"x_{uuid.uuid4().hex[:8]}@example.com",
                                               "password": "secret123", "ref": "<script>"}).status_code == 200


# ── Annual price ──────────────────────────────────────────────────
def test_a_year_costs_ten_months():
    _, h, _ = _register()
    o = client.post("/billing/orders", json={"plan": "pro", "months": 12}, headers=h).json()
    assert 289.5 <= o["amount"] < 290.0


# ── Payment screenshot + Telegram ─────────────────────────────────
def test_screenshot_can_replace_the_reference_and_pings_telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "TOKEN", "123:abc")
    monkeypatch.setattr(notify, "CHAT_ID", "42")
    monkeypatch.setattr(notify, "_post", lambda method, body: sent.append((method, body)))
    _, h, _ = _register()
    o = client.post("/billing/orders", json={"plan": "team", "months": 1}, headers=h).json()
    no = o["order_no"]

    # Without a reference or a screenshot the submission is refused.
    assert client.post(f"/billing/orders/{no}/submit", json={}, headers=h).status_code == 422
    bad = client.post(f"/billing/orders/{no}/proof", files={"file": ("x.txt", b"hello", "text/plain")}, headers=h)
    assert bad.status_code == 422
    up = client.post(f"/billing/orders/{no}/proof", files={"file": ("r.png", PNG, "image/png")}, headers=h)
    assert up.status_code == 200 and up.json()["has_proof"] is True
    assert [x["has_proof"] for x in client.get("/billing/orders", headers=h).json()["orders"] if x["order_no"] == no] == [True]

    r = client.post(f"/billing/orders/{no}/submit", json={}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "submitted" and r.json()["reference"] is None
    assert sent and sent[0][0] == "sendMessage" and no in sent[0][1]["text"] and sent[0][1]["chat_id"] == "42"

    todo = [x for x in client.get("/admin/orders", headers=ADMIN).json()["orders"] if x["order_no"] == no]
    assert todo[0]["has_proof"] is True
    img = client.get(f"/admin/orders/{no}/proof", headers=ADMIN)
    assert img.status_code == 200 and img.content == PNG and "no-store" in img.headers["cache-control"]
    assert client.get(f"/admin/orders/{no}/proof").status_code in (401, 403)

    # Another workspace can't attach a screenshot to this order.
    _, other, _ = _register()
    assert client.post(f"/billing/orders/{no}/proof", files={"file": ("r.png", PNG, "image/png")},
                       headers=other).status_code == 404


def test_telegram_failures_never_raise(monkeypatch):
    monkeypatch.setattr(notify, "TOKEN", "123:abc")
    monkeypatch.setattr(notify, "CHAT_ID", "42")

    def boom(method, body):
        raise OSError("network down")
    monkeypatch.setattr(notify, "_post", boom)
    assert notify.telegram("hi") is False
    monkeypatch.setattr(notify, "TOKEN", "")
    assert notify.enabled() is False and notify.telegram("hi") is False


# ── Creator reputation ────────────────────────────────────────────
def test_reputation_blends_delivery_and_rating(monkeypatch):
    from backend import calibration, crud
    table = {"creators": {"7": {"factor": 1.2, "n": 2}, "8": {"factor": 0.5, "n": 4}}}
    monkeypatch.setattr(calibration, "load", lambda: table)
    assert crud.reputation(99, None) is None                                      # nothing known yet
    assert crud.reputation(99, 4) == {"score": 80, "campaigns_measured": 0, "delivery": None, "rating": 4}
    assert crud.reputation(7, None)["score"] == 84                                # 1.2× → 84
    both = crud.reputation(7, 2)                                                  # (84·2 + 40·1) / 3
    assert both["score"] == 69 and both["delivery"] == 1.2 and both["rating"] == 2
    assert crud.reputation(8, 5)["score"] == round((35 * 3 + 100) / 4)            # weak delivery outweighs 5★
