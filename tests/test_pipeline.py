"""
Collaboration pipeline, analytics and the AI script tools.
"""
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import agent, repo

client = TestClient(app)


def _account():
    email = f"pipe_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    return h, client.get("/auth/me", headers=h).json()["tenant_id"]


def _campaign(h, name="Raya push", kols=None, category="beauty"):
    kols = kols or [
        {"id": 1, "name": "Aina Beauty", "country": "MY", "category": category, "followers": 480000,
         "cost": 900.0, "predicted_gmv": 6000.0, "tier": "Macro"},
        {"id": 2, "name": "Budi Tech", "country": "ID", "category": category, "followers": 52000,
         "cost": 300.0, "predicted_gmv": 2000.0, "tier": "Micro"},
    ]
    r = client.post("/campaigns", headers=h, json={
        "name": name, "countries": ["MY", "ID"], "category": category, "budget": 2000,
        "best_algorithm": "sa", "selected_kols": kols,
        "total_cost": sum(k["cost"] for k in kols),
        "predicted_gmv": sum(k["predicted_gmv"] for k in kols)})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_every_campaign_creator_starts_shortlisted():
    h, _ = _account()
    cid = _campaign(h)
    d = client.get("/pipeline", headers=h).json()
    assert d["total"] == 2 and d["counts"]["shortlisted"] == 2
    assert d["campaigns"] == [{"id": cid, "name": "Raya push"}]
    assert {i["name"] for i in d["items"]} == {"Aina Beauty", "Budi Tech"}
    assert all(i["status"] == "shortlisted" and i["fee"] is None for i in d["items"])


def test_moving_a_creator_through_the_stages():
    h, tenant = _account()
    cid = _campaign(h)
    r = client.put(f"/pipeline/{cid}/1", headers=h,
                   json={"status": "invited", "contact": "wa.me/60123", "note": "sent brief"})
    assert r.status_code == 200 and r.json()["status"] == "invited"
    r = client.put(f"/pipeline/{cid}/1", headers=h, json={"status": "contracted", "fee": 850})
    assert r.json()["fee"] == 850 and r.json()["note"] == "sent brief"      # untouched fields stay
    r = client.put(f"/pipeline/{cid}/1", headers=h,
                   json={"status": "live", "content_url": "https://example.com/v/1"})
    assert r.json()["content_url"] == "https://example.com/v/1"

    d = client.get("/pipeline", headers=h).json()
    assert d["counts"]["live"] == 1 and d["counts"]["shortlisted"] == 1
    history = repo.campaigns_get(tenant, cid)["pipeline"]["1"]["history"]
    assert [x["status"] for x in history] == ["invited", "contracted", "live"]
    # the campaign itself is untouched
    assert len(client.get(f"/campaigns/{cid}", headers=h).json()["selected_kols"]) == 2


def test_filters_and_search():
    h, _ = _account()
    a = _campaign(h, "Alpha")
    b = _campaign(h, "Beta")
    client.put(f"/pipeline/{a}/1", headers=h, json={"status": "invited"})
    get = lambda q: client.get("/pipeline" + q, headers=h).json()["items"]
    assert len(get("?status=invited")) == 1
    assert len(get(f"?campaign_id={b}")) == 2
    assert len(get("?q=budi")) == 2 and len(get("?q=alpha")) == 2
    assert len(get("?status=&q=")) == 4                           # empty filters = no filter
    assert client.get("/pipeline?status=bogus", headers=h).status_code == 422


def test_validation():
    h, _ = _account()
    cid = _campaign(h)
    put = lambda body, kol=1, c=cid: client.put(f"/pipeline/{c}/{kol}", headers=h, json=body).status_code
    assert put({"status": "teleported"}) == 422
    assert put({"fee": -5}) == 422
    assert put({"content_url": "javascript:alert(1)"}) == 422
    assert put({"status": "invited"}, kol=99) == 404              # not in this campaign
    assert put({"status": "invited"}, c=cid + 999) == 404


def test_pipeline_is_private_to_the_workspace():
    h, _ = _account()
    cid = _campaign(h)
    other, _ = _account()
    assert client.get("/pipeline", headers=other).json()["total"] == 0
    assert client.put(f"/pipeline/{cid}/1", headers=other, json={"status": "paid"}).status_code == 404
    assert client.get("/pipeline", headers=h).json()["counts"]["paid"] == 0


def test_csv_export_respects_filters_and_neutralises_formulas():
    h, _ = _account()
    cid = _campaign(h)
    client.put(f"/pipeline/{cid}/1", headers=h, json={"status": "invited", "note": "=HYPERLINK(1)"})
    r = client.get("/pipeline/export.csv?status=invited", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = r.text.lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("campaign_name,name,") and len(lines) == 2
    assert "Aina Beauty" in lines[1] and "'=HYPERLINK(1)" in lines[1]


def test_analytics_snapshot():
    h, _ = _account()
    empty = client.get("/analytics", headers=h).json()
    assert empty["kpis"]["campaigns"] == 0 and empty["kpis"]["predicted_roi"] is None
    assert len(empty["monthly"]) == 6 and empty["top_creators"] == []

    a = _campaign(h, "Alpha")
    _campaign(h, "Beta", category="fashion")
    client.put(f"/pipeline/{a}/1", headers=h, json={"status": "contracted", "fee": 800})
    client.put(f"/pipeline/{a}/2", headers=h, json={"status": "declined"})
    client.put(f"/campaigns/{a}/actual", headers=h, json={
        "actual_total_gmv": 10000,
        "kol_actuals": [{"id": 1, "actual_gmv": 9000}, {"id": 2, "actual_gmv": 1000}]})

    d = client.get("/analytics", headers=h).json()
    k = d["kpis"]
    assert k["campaigns"] == 2 and k["campaigns_completed"] == 1 and k["campaigns_active"] == 1
    assert k["planned_spend"] == 2400 and k["predicted_gmv"] == 16000 and k["actual_gmv"] == 10000
    assert k["predicted_roi"] == round(16000 / 2400, 2)
    assert k["actual_roi"] == round(10000 / 1200, 2)              # only campaigns with results
    assert k["avg_accuracy_pct"] == 125.0
    assert k["collaborations"] == 4 and k["committed_fees"] == 800 and k["acceptance_rate_pct"] == 25.0
    assert d["funnel"]["contracted"] == 1 and d["funnel"]["declined"] == 1 and d["funnel"]["shortlisted"] == 2

    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert d["monthly"][-1]["month"] == this_month and d["monthly"][-1]["campaigns"] == 2
    assert d["deltas"]["campaigns"] is None                       # nothing last month to compare
    assert d["top_ranked_by"] == "actual_gmv" and d["top_creators"][0]["name"] == "Aina Beauty"
    assert {c["category"] for c in d["categories"]} == {"beauty", "fashion"}


# ── AI endpoints (LLM mocked) ─────────────────────────────────────
@pytest.fixture
def llm(monkeypatch):
    calls = []

    def fake(messages, tools):
        calls.append(messages)
        return {"choices": [{"message": {"content": "AI says hello"}}]}

    monkeypatch.setattr(agent, "_API_KEY", "test-key")
    monkeypatch.setattr(agent, "_llm", fake)
    return calls


def test_insights_use_the_real_numbers_and_the_copilot_quota(llm):
    h, tenant = _account()
    assert client.post("/analytics/insights", headers=h, json={"lang": "zh"}).status_code == 409
    _campaign(h)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    before = repo.agent_messages(tenant, month)
    r = client.post("/analytics/insights", headers=h, json={"lang": "zh"})
    assert r.status_code == 200 and r.json()["insights"] == "AI says hello"
    assert repo.agent_messages(tenant, month) == before + 1
    system, user = llm[-1][0]["content"], llm[-1][1]["content"]
    assert "Simplified Chinese" in system and '"predicted_gmv": 8000.0' in user


def test_script_generate_and_check(llm):
    h, _ = _account()
    r = client.post("/scripts/generate", headers=h, json={
        "product": "Vitamin C serum", "selling_points": "15% vitamin C, fragrance free",
        "format": "video", "duration_sec": 30, "lang": "en", "script_lang": "Bahasa Melayu"})
    assert r.status_code == 200 and r.json()["script"] == "AI says hello"
    assert "Bahasa Melayu" in llm[-1][1]["content"] and "Vitamin C serum" in llm[-1][1]["content"]

    assert client.post("/scripts/generate", headers=h,
                       json={"product": "x", "format": "video"}).status_code == 422
    assert client.post("/scripts/generate", headers=h,
                       json={"product": "Serum", "format": "podcast"}).status_code == 422
    assert client.post("/scripts/generate", headers=h,
                       json={"product": "Serum", "creator_id": 99999999}).status_code == 404

    r = client.post("/scripts/check", headers=h, json={
        "script": "This serum is the best in the world and cures acne in one day, guaranteed!",
        "product": "Serum", "lang": "en"})
    assert r.status_code == 200 and r.json()["review"] == "AI says hello"
    assert "cures acne" in llm[-1][1]["content"]
    assert client.post("/scripts/check", headers=h, json={"script": "too short"}).status_code == 422


def test_ai_failure_refunds_the_quota_and_missing_key_is_reported(monkeypatch):
    from fastapi import HTTPException
    h, tenant = _account()
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    body = {"script": "A perfectly ordinary script about a serum that people might like."}

    monkeypatch.setattr(agent, "_API_KEY", "")
    assert client.post("/scripts/check", headers=h, json=body).status_code == 503

    def boom(messages, tools):
        raise HTTPException(status_code=502, detail="down")

    monkeypatch.setattr(agent, "_API_KEY", "test-key")
    monkeypatch.setattr(agent, "_llm", boom)
    before = repo.agent_messages(tenant, month)
    assert client.post("/scripts/check", headers=h, json=body).status_code == 502
    assert repo.agent_messages(tenant, month) == before
