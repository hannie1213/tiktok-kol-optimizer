"""Plans, usage limits and the Lemon Squeezy webhook."""
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from backend import billing
from backend.main import app

client = TestClient(app)

SECRET = "whsec_test"
OPT = {"countries": ["MY"], "category": "beauty", "budget": 3000}


@pytest.fixture(autouse=True)
def ls_env(monkeypatch):
    monkeypatch.setenv("LEMONSQUEEZY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("LEMONSQUEEZY_VARIANT_PRO", "111")
    monkeypatch.setenv("LEMONSQUEEZY_VARIANT_AGENCY", "222")


def _account():
    email = f"bill_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    token = r.json()["token"]
    tenant = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["tenant_id"]
    return {"Authorization": f"Bearer {token}"}, tenant


def _send(event_name, tenant, status="active", variant=111, sub_id="sub_1", secret=SECRET):
    body = json.dumps({
        "meta": {"event_name": event_name, "custom_data": {"tenant_id": tenant}},
        "data": {"id": sub_id, "type": "subscriptions", "attributes": {
            "status": status, "variant_id": variant, "renews_at": "2026-12-25T00:00:00Z",
            "ends_at": None, "urls": {"customer_portal": "https://shop.lemonsqueezy.com/billing"},
        }},
    }).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/billing/webhook", content=body, headers={"X-Signature": sig, "Content-Type": "application/json"})


def test_new_account_is_free_with_limits():
    h, _ = _account()
    s = client.get("/billing/status", headers=h).json()
    assert s["plan"] == "free"
    assert s["limits"]["optimizations_per_month"] == 10
    assert s["usage"]["optimizations"] == 0


def test_free_optimization_quota_enforced(monkeypatch):
    monkeypatch.setitem(billing.PLANS["free"], "optimizations_per_month", 2)
    h, _ = _account()
    assert client.post("/optimize", json=OPT, headers=h).status_code == 200
    assert client.post("/optimize", json=OPT, headers=h).status_code == 200
    r = client.post("/optimize", json=OPT, headers=h)
    assert r.status_code == 402 and r.json()["detail"]["upgrade"] is True
    assert client.get("/billing/status", headers=h).json()["usage"]["optimizations"] == 2


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
    first = client.post("/campaigns", json=camp, headers=h)
    assert first.status_code == 200, first.text
    assert client.post("/campaigns", json=camp, headers=h).status_code == 402


def test_webhook_rejects_bad_signature():
    _, tenant = _account()
    assert _send("subscription_created", tenant, secret="wrong").status_code == 401


def test_subscription_lifecycle_upgrades_and_downgrades():
    h, tenant = _account()
    sub = f"sub_{uuid.uuid4().hex[:6]}"
    assert _send("subscription_created", tenant, sub_id=sub).json()["plan"] == "pro"
    s = client.get("/billing/status", headers=h).json()
    assert s["plan"] == "pro" and s["portal_url"].startswith("https://")
    assert client.get("/kols/export", headers=h).status_code == 200

    # Cancelled keeps access until the period ends...
    assert _send("subscription_cancelled", tenant, status="cancelled", sub_id=sub).json()["plan"] == "pro"
    # ...then expiry drops back to free.
    assert _send("subscription_expired", tenant, status="expired", sub_id=sub).json()["plan"] == "free"
    assert client.get("/kols/export", headers=h).status_code == 402


def test_agency_variant_maps_to_agency():
    h, tenant = _account()
    _send("subscription_created", tenant, variant=222, sub_id=f"sub_{uuid.uuid4().hex[:6]}")
    assert client.get("/billing/status", headers=h).json()["plan"] == "agency"


def test_checkout_unavailable_until_configured(monkeypatch):
    monkeypatch.delenv("LEMONSQUEEZY_API_KEY", raising=False)
    h, _ = _account()
    assert client.post("/billing/checkout", json={"plan": "pro"}, headers=h).status_code == 503
    assert client.post("/billing/checkout", json={"plan": "gold"}, headers=h).status_code == 422


def test_checkout_calls_lemon_squeezy(monkeypatch):
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", "key")
    monkeypatch.setenv("LEMONSQUEEZY_STORE_ID", "9")
    seen = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"attributes": {"url": "https://x.lemonsqueezy.com/checkout/abc"}}}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, body=json)
        return Resp()

    monkeypatch.setattr(billing.httpx, "post", fake_post)
    h, tenant = _account()
    r = client.post("/billing/checkout", json={"plan": "pro"}, headers=h)
    assert r.json()["url"].startswith("https://")
    assert seen["body"]["data"]["attributes"]["checkout_data"]["custom"]["tenant_id"] == tenant
    assert seen["body"]["data"]["relationships"]["variant"]["data"]["id"] == "111"
