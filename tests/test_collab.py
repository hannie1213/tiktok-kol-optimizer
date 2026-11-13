"""
Templates, message log, contracts, assignments, activity trail and onboarding.
"""
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import repo

client = TestClient(app)


def _account(lang="en"):
    email = f"collab_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['token']}", "X-Lang": lang}
    return h, email, client.get("/auth/me", headers=h).json()["tenant_id"]


def _campaign(h, name="Raya push"):
    kols = [{"id": 1, "name": "Aina Beauty", "country": "MY", "category": "beauty", "followers": 480000,
             "cost": 900.0, "predicted_gmv": 6000.0, "tier": "Macro"},
            {"id": 2, "name": "Budi Tech", "country": "ID", "category": "beauty", "followers": 52000,
             "cost": 300.0, "predicted_gmv": 2000.0, "tier": "Micro"}]
    r = client.post("/campaigns", headers=h, json={
        "name": name, "countries": ["MY", "ID"], "category": "beauty", "budget": 2000,
        "best_algorithm": "sa", "selected_kols": kols, "total_cost": 1200, "predicted_gmv": 8000})
    return r.json()["id"]


def test_starter_templates_are_seeded_once_in_the_users_language():
    h, _, _ = _account("zh")
    d = client.get("/templates?kind=message", headers=h).json()
    assert len(d["templates"]) == 3 and d["templates"][0]["name"] == "首次邀约"
    assert "creator_name" in d["variables"] and "deliverables" not in d["variables"]
    contract = client.get("/templates?kind=contract", headers=h).json()
    assert len(contract["templates"]) == 1 and "deliverables" in contract["variables"]

    # deleting them all must not bring them back
    for t in d["templates"]:
        assert client.delete(f"/templates/{t['id']}?kind=message", headers=h).status_code == 200
    assert client.get("/templates?kind=message", headers=h).json()["templates"] == []

    en, _, _ = _account("en")
    assert client.get("/templates?kind=message", headers=en).json()["templates"][0]["name"] == "First invitation"


def test_template_crud_and_isolation():
    h, _, _ = _account()
    r = client.post("/templates", headers=h, json={"kind": "message", "name": " Promo ", "body": "Hi {creator_name}"})
    assert r.status_code == 200 and r.json()["name"] == "Promo"
    tid = r.json()["id"]
    r = client.put(f"/templates/{tid}", headers=h, json={"kind": "message", "name": "Promo 2", "body": "Yo {creator_name}"})
    assert r.json()["body"] == "Yo {creator_name}"

    assert client.post("/templates", headers=h, json={"kind": "nope", "name": "x", "body": "y"}).status_code == 422
    assert client.post("/templates", headers=h, json={"kind": "message", "name": "", "body": "y"}).status_code == 422
    # a message template is not a contract template, and other workspaces can't touch it
    assert client.put(f"/templates/{tid}", headers=h,
                      json={"kind": "contract", "name": "a", "body": "b"}).status_code == 404
    other, _, _ = _account()
    assert client.delete(f"/templates/{tid}?kind=message", headers=other).status_code == 404
    assert client.delete(f"/templates/{tid}?kind=message", headers=h).status_code == 200


def test_render_fills_variables_from_the_collaboration():
    h, email, _ = _account()
    cid = _campaign(h, "Raya push")
    client.put(f"/pipeline/{cid}/1", headers=h, json={"fee": 850, "contact": "+60 12-345 6789"})
    t = client.post("/templates", headers=h, json={
        "kind": "message", "name": "T",
        "body": "Hi {creator_name}, {campaign_name}, {fee}, {followers}, {country}, {sender}, {mystery}"}).json()
    r = client.post(f"/templates/{t['id']}/render", headers=h,
                    json={"kind": "message", "campaign_id": cid, "kol_id": 1})
    assert r.status_code == 200
    assert r.json()["text"] == f"Hi Aina Beauty, Raya push, $850, 480,000, MY, {email}, {{mystery}}"
    assert r.json()["contact"] == "+60 12-345 6789"
    # no fee agreed yet -> the planned cost stands in
    r2 = client.post(f"/templates/{t['id']}/render", headers=h,
                     json={"kind": "message", "campaign_id": cid, "kol_id": 2})
    assert "$300" in r2.json()["text"]
    assert client.post(f"/templates/{t['id']}/render", headers=h,
                       json={"kind": "message", "campaign_id": cid, "kol_id": 99}).status_code == 404


def test_logging_a_message_marks_the_creator_invited_once():
    h, email, _ = _account()
    cid = _campaign(h)
    r = client.post("/messages", headers=h, json={"campaign_id": cid, "kol_id": 1, "channel": "whatsapp",
                                                  "body": "Hi Aina!", "template_name": "First invitation"})
    assert r.status_code == 200 and r.json()["advanced_to_invited"] is True
    assert r.json()["message"]["sender"] == email
    items = {i["kol_id"]: i for i in client.get("/pipeline", headers=h).json()["items"]}
    assert items[1]["status"] == "invited" and items[2]["status"] == "shortlisted"

    # a later message never moves a creator backwards
    client.put(f"/pipeline/{cid}/1", headers=h, json={"status": "contracted"})
    r = client.post("/messages", headers=h, json={"campaign_id": cid, "kol_id": 1, "channel": "email", "body": "Brief"})
    assert r.json()["advanced_to_invited"] is False
    assert client.get("/pipeline?status=contracted", headers=h).json()["items"][0]["kol_id"] == 1

    log = client.get("/messages", headers=h).json()
    assert log["total"] == 2 and log["messages"][0]["body"] == "Brief"       # newest first
    assert client.get("/messages?q=aina!", headers=h).json()["total"] == 1
    assert client.post("/messages", headers=h, json={"campaign_id": cid, "kol_id": 1, "channel": "sms",
                                                     "body": "x"}).status_code == 422
    other, _, _ = _account()
    assert client.get("/messages", headers=other).json()["total"] == 0


def test_contract_lifecycle():
    h, email, _ = _account()
    cid = _campaign(h)
    client.put(f"/pipeline/{cid}/1", headers=h, json={"fee": 850})
    tpl = client.get("/templates?kind=contract", headers=h).json()["templates"][0]
    r = client.post("/contracts", headers=h, json={
        "campaign_id": cid, "kol_id": 1, "template_id": tpl["id"],
        "deliverables": "2 short videos", "start_date": "2026-12-01", "end_date": "2026-12-31"})
    assert r.status_code == 200
    c = r.json()
    assert c["status"] == "draft" and c["number"].startswith("C-") and c["fee"] == 850
    for needle in ("Aina Beauty", "2 short videos", "2026-12-01", "$850", email):
        assert needle in c["body"]

    listing = client.get("/contracts", headers=h).json()["contracts"]
    assert len(listing) == 1 and "body" not in listing[0]
    assert client.get(f"/contracts/{c['id']}", headers=h).json()["body"] == c["body"]

    assert client.put(f"/contracts/{c['id']}", headers=h, json={"body": "Edited"}).json()["body"] == "Edited"
    assert client.put(f"/contracts/{c['id']}", headers=h, json={"status": "framed"}).status_code == 422
    assert client.put(f"/contracts/{c['id']}", headers=h, json={"status": "signed"}).json()["status"] == "signed"
    # signed means frozen
    assert client.put(f"/contracts/{c['id']}", headers=h, json={"body": "sneaky"}).status_code == 409
    assert client.delete(f"/contracts/{c['id']}", headers=h).status_code == 409
    assert client.put(f"/contracts/{c['id']}", headers=h, json={"status": "cancelled"}).status_code == 200

    other, _, _ = _account()
    assert client.get(f"/contracts/{c['id']}", headers=other).status_code == 404
    # missing details become visible placeholders rather than blanks
    c2 = client.post("/contracts", headers=h, json={"campaign_id": cid, "kol_id": 2,
                                                    "template_id": tpl["id"]}).json()
    assert "[deliverables]" in c2["body"] and c2["number"] != c["number"]
    assert client.delete(f"/contracts/{c2['id']}", headers=h).status_code == 200


def test_assignments_and_my_filter():
    h, email, _ = _account()
    cid = _campaign(h)
    inv = client.post("/team/invites", headers=h, json={"email": f"mate_{uuid.uuid4().hex[:8]}@example.com"})
    assert inv.status_code == 402                                   # free plan: nobody to assign yet
    assert client.put(f"/pipeline/{cid}/1", headers=h,
                      json={"assignee": "stranger@example.com"}).status_code == 422
    r = client.put(f"/pipeline/{cid}/1", headers=h, json={"assignee": email.upper()})
    assert r.status_code == 200 and r.json()["assignee"] == email

    d = client.get("/pipeline?mine=1", headers=h).json()
    assert [i["kol_id"] for i in d["items"]] == [1] and d["mine_count"] == 1 and d["members"] == [email]
    assert len(client.get("/pipeline", headers=h).json()["items"]) == 2
    client.put(f"/pipeline/{cid}/1", headers=h, json={"assignee": ""})
    assert client.get("/pipeline?mine=1", headers=h).json()["items"] == []


def test_activity_trail_records_who_did_what():
    h, email, tenant = _account()
    cid = _campaign(h, "Launch")
    client.put(f"/pipeline/{cid}/1", headers=h, json={"status": "invited"})
    client.put(f"/pipeline/{cid}/1", headers=h, json={"note": "just a note"})     # not an event
    client.post("/messages", headers=h, json={"campaign_id": cid, "kol_id": 2, "channel": "copy", "body": "Hi"})
    entries = client.get("/activity", headers=h).json()["entries"]
    assert [e["action"] for e in entries] == ["message.send", "collab.stage", "campaign.save"]
    assert all(e["actor"] == email for e in entries)
    assert entries[1]["target"] == "Aina Beauty" and entries[1]["detail"] == "shortlisted -> invited"
    other, _, _ = _account()
    assert client.get("/activity", headers=other).json()["entries"] == []
    # deleting the workspace removes its documents too
    client.post("/auth/delete-account", headers=h, json={"password": "secret123"})
    assert repo.docs_list(tenant, "activity") == [] and repo.docs_list(tenant, "message") == []


def test_onboarding_checklist_tracks_progress():
    h, _, _ = _account()
    d = client.get("/onboarding", headers=h).json()
    state = {s["id"]: s["done"] for s in d["steps"]}
    assert d["total"] == 7 and state["creators"] is True and state["campaign"] is False

    cid = _campaign(h)
    client.post("/messages", headers=h, json={"campaign_id": cid, "kol_id": 1, "channel": "copy", "body": "Hi"})
    client.put(f"/campaigns/{cid}/actual", headers=h, json={"actual_total_gmv": 9000})
    state = {s["id"]: s["done"] for s in client.get("/onboarding", headers=h).json()["steps"]}
    assert state["campaign"] and state["optimize"] and state["outreach"] and state["results"]
    assert not state["contract"] and not state["team"]


def test_export_includes_templates_and_contracts():
    h, _, _ = _account()
    client.get("/templates?kind=message", headers=h)
    doc = client.get("/account/export", headers=h).json()
    assert len(doc["message_templates"]) == 3 and doc["contracts"] == [] and doc["messages"] == []
