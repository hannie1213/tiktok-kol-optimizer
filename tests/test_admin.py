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


# ── Console v2: terms, revenue, status, audit ─────────────────────
def test_plan_can_be_granted_for_a_term_and_renewed():
    from datetime import datetime, timedelta, timezone
    email = _register()
    r = client.post("/admin/plan", json={"email": email, "plan": "pro", "months": 3}, headers=H)
    assert r.status_code == 200, r.text
    first = datetime.fromisoformat(r.json()["expires_at"])
    assert timedelta(days=89) < first - datetime.now(timezone.utc) < timedelta(days=91)

    row = next(u for u in client.get("/admin/users", headers=H).json()["users"] if u["email"] == email)
    assert row["plan"] == "pro" and row["plan_expires_at"] == r.json()["expires_at"]
    assert row["email_verified"] is False

    # Same plan + a term = renewal, stacked on the current end.
    r2 = client.post("/admin/plan", json={"email": email, "plan": "pro", "months": 1}, headers=H)
    assert datetime.fromisoformat(r2.json()["expires_at"]) - first == timedelta(days=30)

    # No term = permanent; Free never carries an expiry.
    assert client.post("/admin/plan", json={"email": email, "plan": "team"}, headers=H).json()["expires_at"] is None
    assert client.post("/admin/plan", json={"email": email, "plan": "free", "months": 3}, headers=H).json()["expires_at"] is None
    assert client.post("/admin/plan", json={"email": email, "plan": "pro", "months": 5}, headers=H).status_code == 422


def test_overview_counts_revenue_pending_and_expiring():
    from datetime import datetime, timedelta, timezone
    from backend import repo
    before = client.get("/admin/overview", headers=H).json()

    email = _register()
    tenant = repo.user_find(email)["tenant_id"]
    token = client.post("/auth/login", json={"email": email, "password": "secret123"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    order = client.post("/billing/orders", json={"plan": "pro", "months": 1}, headers=auth).json()
    client.post(f"/billing/orders/{order['order_no']}/submit",
                json={"reference": "REF" + uuid.uuid4().hex[:10]}, headers=auth)

    mid = client.get("/admin/overview", headers=H).json()
    assert mid["pending_orders"] == before["pending_orders"] + 1

    assert client.post(f"/admin/orders/{order['order_no']}/confirm", headers=H).status_code == 200
    after = client.get("/admin/overview", headers=H).json()
    assert after["pending_orders"] == before["pending_orders"]
    cur = order["currency"]
    prev = before["revenue_month"].get(cur, {"amount": 0, "count": 0})
    assert round(after["revenue_month"][cur]["amount"] - prev["amount"], 2) == order["amount"]
    assert after["revenue_month"][cur]["count"] == prev["count"] + 1
    assert after["revenue_total"][cur]["amount"] >= after["revenue_month"][cur]["amount"]

    # A term ending in 3 days shows up in the renewal follow-up count.
    soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    repo.plan_set(tenant, "pro", soon, expires_at=soon)
    assert client.get("/admin/overview", headers=H).json()["expiring_7d"] == after["expiring_7d"] + 1


def test_order_history_lists_every_status():
    statuses = {o["status"] for o in client.get("/admin/orders?status=all", headers=H).json()["orders"]}
    assert "paid" in statuses                      # from the test above
    todo = client.get("/admin/orders", headers=H).json()["orders"]
    assert all(o["status"] == "submitted" for o in todo)


def test_status_reports_switches_without_leaking_secrets():
    r = client.get("/admin/status", headers=H)
    assert r.status_code == 200
    d = r.json()
    assert d["payments"]["enabled"] is True and d["payments"]["prices"]["pro"] > 0
    assert d["email"]["enabled"] is False and d["database"] == "sqlite"
    assert any("DEFAULT_TENANT" in p for p in d["launch_blockers"])     # test env bypasses auth
    assert KEY not in r.text and settings.JWT_SECRET not in r.text
    assert client.get("/admin/status").status_code == 403


def test_audit_trail_records_operator_actions():
    email = _register()
    client.post("/admin/plan", json={"email": email, "plan": "pro", "months": 1}, headers=H)
    client.post("/admin/reset-password", json={"email": email}, headers=H)
    client.post("/admin/delete-user", json={"email": email}, headers=H)

    entries = client.get("/admin/audit", headers=H).json()["entries"]
    mine = [e for e in entries if e["target"] == email]
    assert [e["action"] for e in mine] == ["user.delete", "user.reset_password", "plan.set"]   # newest first
    assert "free -> pro" in mine[-1]["detail"] and "1mo" in mine[-1]["detail"]
    # The temporary password must never be written to the log.
    assert all("temporary" not in e["detail"].lower() for e in mine)
    assert client.get("/admin/audit").status_code == 403


# ── Console v3: growth, workspace detail, run jobs ──
def _owner_with_campaign():
    import uuid
    email = f"adm_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    kols = [{"id": 1, "name": "Aina", "country": "MY", "category": "beauty", "followers": 1000,
             "cost": 100.0, "predicted_gmv": 900.0, "tier": "Nano"}]
    cid = client.post("/campaigns", headers=h, json={
        "name": "Adm", "countries": ["MY"], "category": "beauty", "budget": 500, "best_algorithm": "sa",
        "selected_kols": kols, "total_cost": 100, "predicted_gmv": 900}).json()["id"]
    return email, h, cid


def test_growth_funnel_counts_real_owners():
    before = client.get("/admin/growth", headers=H).json()
    email, h, cid = _owner_with_campaign()
    client.post("/messages", headers=h, json={"campaign_id": cid, "kol_id": 1, "channel": "copy", "body": "Hi"})
    client.post("/auth/demo")
    after = client.get("/admin/growth", headers=H).json()
    step = lambda d, s: next(x["count"] for x in d["funnel"] if x["step"] == s)   # noqa: E731
    assert [x["step"] for x in after["funnel"]] == ["registered", "verified", "optimized", "campaign", "outreach", "paid"]
    for s in ("registered", "optimized", "campaign", "outreach"):
        assert step(after, s) == step(before, s) + 1, s          # the demo account is not counted
    assert step(after, "paid") == step(before, "paid")
    assert len(after["signups"]) == 14 and after["signups"][-1]["count"] >= 1
    assert after["active_7d"] == before["active_7d"] + 1
    assert after["adoption"]["messages"] == before["adoption"]["messages"] + 1


def test_workspace_detail_shows_counts_but_no_content():
    email, h, cid = _owner_with_campaign()
    client.put(f"/pipeline/{cid}/1", headers=h, json={"status": "invited", "note": "private note"})
    r = client.get("/admin/workspace", headers=H, params={"email": email.upper()})
    assert r.status_code == 200
    d = r.json()
    assert d["email"] == email and d["plan"] == "free" and d["seats"] == 1
    assert [m["role"] for m in d["members"]] == ["owner"]
    assert d["counts"]["campaigns"] == 1 and d["counts"]["creators"] > 0
    assert d["pipeline"]["invited"] == 1 and d["usage"]["optimize_limit"] == 10
    assert d["onboarding"]["total"] == 7
    assert [a["action"] for a in d["activity"]][:2] == ["collab.stage", "campaign.save"]
    assert "private note" not in r.text and "password" not in r.text
    assert client.get("/admin/workspace", headers=H, params={"email": "nobody@example.com"}).status_code == 404
    assert client.get("/admin/workspace", params={"email": email}).status_code in (401, 403)


def test_user_rows_carry_members_and_last_active():
    email, _, _ = _owner_with_campaign()
    row = next(u for u in client.get("/admin/users", headers=H).json()["users"] if u["email"] == email)
    assert row["members"] == 1 and row["last_active"]


def test_run_jobs_now_is_audited():
    r = client.post("/admin/jobs/run", headers=H)
    assert r.status_code == 200 and set(r.json()["result"]) == {"reminders", "orders_expired", "demos_swept"}
    assert client.get("/admin/audit", headers=H).json()["entries"][0]["action"] == "jobs.run"
