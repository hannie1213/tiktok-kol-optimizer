"""
AI Studio (storyboards, operations copy, saved pieces) and the operator
console's moderation tools (suspend, announcement, pool).
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import agent, repo, settings

client = TestClient(app)
KEY = "test-admin-key-0123456789"
ADMIN = {"X-Admin-Key": KEY}


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", KEY)


def _account():
    email = f"studio_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    return h, email, client.get("/auth/me", headers=h).json()["tenant_id"]


@pytest.fixture
def llm(monkeypatch):
    box = {"reply": "", "calls": []}

    def fake(messages, tools):
        box["calls"].append(messages)
        return {"choices": [{"message": {"content": box["reply"]}}]}

    monkeypatch.setattr(agent, "_API_KEY", "test-key")
    monkeypatch.setattr(agent, "_llm", fake)
    return box


BOARD = {"title": "Glow in 20 seconds", "cta": "Tap the yellow basket", "music": "upbeat pop",
         "hashtags": ["#skincare", "vitamin c", "glow"],
         "scenes": [{"seconds": 2, "caption": "Dull skin?", "voiceover": "Tired of dull skin?", "visual": "Close-up"},
                    {"seconds": 4, "caption": "15% vitamin C", "voiceover": "This serum has 15% vitamin C.", "visual": "Bottle"},
                    {"seconds": 4, "caption": "Shop now", "voiceover": "Get yours today.", "visual": "Basket"}]}


def _month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def test_storyboard_is_parsed_cleaned_and_scaled_to_the_requested_length(llm):
    h, _, tenant = _account()
    llm["reply"] = "Here you go:\n```json\n" + json.dumps(BOARD) + "\n```"
    r = client.post("/studio/storyboard", headers=h, json={
        "product": "Vitamin C serum", "selling_points": "15% vitamin C", "duration_sec": 20,
        "offer": "RM39 this week", "lang": "zh", "script_lang": "Bahasa Melayu"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "Glow in 20 seconds" and len(d["scenes"]) == 3
    assert abs(d["duration_sec"] - 20) < 0.3 and d["scenes"][0]["seconds"] == 4.0   # 2:4:4 scaled to 20s
    assert d["hashtags"] == ["skincare", "vitaminc", "glow"]
    system, user = llm["calls"][-1][0]["content"], llm["calls"][-1][1]["content"]
    assert "Simplified Chinese" in system and "Bahasa Melayu" in user and "RM39 this week" in user
    assert repo.agent_messages(tenant, _month()) == 1


def test_unreadable_storyboard_is_refunded(llm):
    h, _, tenant = _account()
    for bad in ("Sorry, I cannot help with that.", '{"scenes": []}', '{"scenes": [{"seconds": 3}]}'):
        llm["reply"] = bad
        r = client.post("/studio/storyboard", headers=h, json={"product": "Serum"})
        assert r.status_code == 502, bad
    assert repo.agent_messages(tenant, _month()) == 0
    assert client.post("/studio/storyboard", headers=h, json={"product": "x"}).status_code == 422
    assert client.post("/studio/storyboard", headers=h,
                       json={"product": "Serum", "duration_sec": 600}).status_code == 422


def test_operations_copy_covers_each_kind(llm):
    h, _, _ = _account()
    llm["reply"] = "Ready-to-use copy"
    for kind in ("live_script", "listing", "caption", "outreach", "faq", "calendar"):
        r = client.post("/studio/ops", headers=h, json={"kind": kind, "product": "Vitamin C serum",
                                                        "audience": "students", "out_lang": "ไทย"})
        assert r.status_code == 200 and r.json() == {"kind": kind, "text": "Ready-to-use copy"}
        assert "ไทย" in llm["calls"][-1][0]["content"] and "students" in llm["calls"][-1][1]["content"]
    assert client.post("/studio/ops", headers=h, json={"kind": "poem", "product": "Serum"}).status_code == 422


def test_saved_pieces_are_private_and_capped():
    h, email, _ = _account()
    other, _, _ = _account()
    r = client.post("/studio/saved", headers=h, json={"type": "ops", "title": " Listing v1 ", "body": "Title: …"})
    assert r.status_code == 200 and r.json()["title"] == "Listing v1" and r.json()["created_by"] == email
    sid = r.json()["id"]
    assert [i["id"] for i in client.get("/studio/saved", headers=h).json()["items"]] == [sid]
    assert client.get("/studio/saved", headers=other).json()["items"] == []
    assert client.delete(f"/studio/saved/{sid}", headers=other).status_code == 404
    assert client.post("/studio/saved", headers=h, json={"type": "meme", "title": "x", "body": "y"}).status_code == 422
    assert client.delete(f"/studio/saved/{sid}", headers=h).status_code == 200
    assert client.get("/studio/saved", headers=h).json()["items"] == []


# ── Operator console ──────────────────────────────────────────────
def test_suspended_account_is_locked_out_everywhere_and_can_be_restored():
    h, email, tenant = _account()
    ext = {"Authorization": f"Bearer {client.post('/extension/token', headers=h).json()['token']}"}
    cap = {"platform": "tiktok", "handle": "sus_" + uuid.uuid4().hex[:8], "followers": 10}
    assert client.post("/pool/capture", headers=ext, json=cap).status_code == 200

    r = client.post("/admin/suspend", headers=ADMIN, json={"email": email.upper()})
    assert r.status_code == 200 and r.json()["disabled"] is True
    assert client.get("/auth/me", headers=h).status_code == 403
    assert client.post("/auth/login", json={"email": email, "password": "secret123"}).status_code == 403
    assert client.post("/pool/capture", headers=ext, json=cap).status_code == 401
    # a wrong password still says "invalid", not "suspended"
    assert client.post("/auth/login", json={"email": email, "password": "wrong-one"}).status_code == 401
    row = next(u for u in client.get("/admin/users", headers=ADMIN).json()["users"] if u["email"] == email)
    assert row["disabled"] is True
    assert repo.kols_count(tenant) > 0                              # data untouched

    assert client.post("/admin/suspend", headers=ADMIN, json={"email": email, "suspended": False}).status_code == 200
    assert client.get("/auth/me", headers=h).status_code == 200
    actions = [e["action"] for e in client.get("/admin/audit", headers=ADMIN).json()["entries"][:2]]
    assert actions == ["user.unsuspend", "user.suspend"]
    assert client.post("/admin/suspend", headers=ADMIN, json={"email": "nobody@example.com"}).status_code == 404
    assert client.post("/admin/suspend", json={"email": email}).status_code in (401, 403)


def test_announcement_reaches_the_public_config_and_can_be_cleared():
    client.post("/admin/announcement", headers=ADMIN, json={"text": "", "text_zh": ""})
    assert client.get("/app-config").json()["announcement"] is None
    r = client.post("/admin/announcement", headers=ADMIN,
                    json={"text": "Maintenance tonight 02:00", "text_zh": "今晚 02:00 维护", "level": "warn"})
    assert r.status_code == 200
    a = client.get("/app-config").json()["announcement"]
    assert a["text"] == "Maintenance tonight 02:00" and a["text_zh"] == "今晚 02:00 维护" and a["level"] == "warn"
    first_id = a["id"]
    client.post("/admin/announcement", headers=ADMIN, json={"text": "All done", "level": "info"})
    b = client.get("/app-config").json()["announcement"]
    assert b["text"] == "All done" and b["id"] != first_id            # a new notice gets a new id
    assert client.post("/admin/announcement", headers=ADMIN, json={"text": "x", "level": "party"}).status_code == 422
    assert client.post("/admin/announcement", json={"text": "x"}).status_code in (401, 403)
    client.post("/admin/announcement", headers=ADMIN, json={"text": "", "text_zh": ""})
    assert client.get("/app-config").json()["announcement"] is None


def test_operator_can_review_and_prune_the_pool():
    h, _, _ = _account()
    ext = {"Authorization": f"Bearer {client.post('/extension/token', headers=h).json()['token']}"}
    handle = "prune_" + uuid.uuid4().hex[:8]
    cid = client.post("/pool/capture", headers=ext, json={"platform": "tiktok", "handle": handle,
                                                          "name": "Spam", "followers": 5}).json()["creator"]["id"]
    listing = client.get(f"/admin/pool?q={handle}", headers=ADMIN).json()
    assert [i["id"] for i in listing["items"]] == [cid] and listing["total"] >= 1
    assert client.get("/admin/pool").status_code in (401, 403)
    assert client.delete(f"/admin/pool/{cid}", headers=ADMIN).status_code == 200
    assert client.get(f"/pool/search?q={handle}", headers=h).json()["total"] == 0
    assert client.delete(f"/admin/pool/{cid}", headers=ADMIN).status_code == 404
    assert client.get("/admin/audit", headers=ADMIN).json()["entries"][0]["action"] == "pool.delete"
