"""
Team seats, invitations, renewal reminders and workspace export.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import jobs, mailer, repo, settings

client = TestClient(app)
KEY = "test-admin-key-0123456789"


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_KEY", KEY)


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "SMTP_HOST", "smtp.test")
    monkeypatch.setattr(mailer, "SMTP_FROM", "tiko <no-reply@tiko.test>")
    monkeypatch.setattr(mailer, "APP_BASE_URL", "https://app.tiko.test")
    monkeypatch.setattr(mailer, "_deliver", sent.append)
    return sent


def _email(prefix="ws"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}@example.com"


def _owner(plan="pro"):
    email = _email()
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    tenant = client.get("/auth/me", headers=h).json()["tenant_id"]
    if plan != "free":
        repo.plan_set(tenant, plan)
    return email, h, tenant


def _invite(h, email=None):
    email = email or _email("member")
    r = client.post("/team/invites", json={"email": email}, headers=h)
    assert r.status_code == 200, r.text
    return email, r.json()["invite"]


def _join(link, password="memberpass1"):
    token = link.split("#invite=")[1]
    r = client.post("/team/accept", json={"token": token, "password": password})
    return r, token


def test_invited_member_shares_the_workspace():
    owner_email, h, tenant = _owner()
    member_email, inv = _invite(h)

    info = client.post("/team/invite-info", json={"token": inv["link"].split("#invite=")[1]})
    assert info.json() == {"email": member_email, "invited_by": owner_email}

    r, token = _join(inv["link"])
    assert r.status_code == 200, r.text
    mh = {"Authorization": f"Bearer {r.json()['token']}"}

    me = client.get("/auth/me", headers=mh).json()
    assert me["tenant_id"] == tenant and me["role"] == "member"
    assert me["email"] == member_email and me["workspace_owner"] == owner_email

    # Same library, and a change by one is seen by the other.
    kols = client.get("/kols", headers=mh).json()
    assert kols == client.get("/kols", headers=h).json()

    team = client.get("/team", headers=h).json()
    assert [m["role"] for m in team["members"]] == ["owner", "member"]
    assert team["used"] == 2 and team["seats"] == 3 and team["invites"] == []

    # The link works once; the member can sign in normally afterwards.
    assert client.post("/team/accept", json={"token": token, "password": "another123"}).status_code == 400
    assert client.post("/auth/login", json={"email": member_email,
                                            "password": "memberpass1"}).status_code == 200


def test_members_cannot_touch_billing_or_the_team():
    _, h, _ = _owner()
    _, inv = _invite(h)
    mh = {"Authorization": f"Bearer {_join(inv['link'])[0].json()['token']}"}

    assert client.post("/billing/checkout", json={"plan": "free"}, headers=mh).status_code == 403
    assert client.post("/billing/orders", json={"plan": "team", "months": 1}, headers=mh).status_code == 403
    assert client.post("/team/invites", json={"email": _email()}, headers=mh).status_code == 403
    assert client.get("/account/export", headers=mh).status_code == 403
    view = client.get("/team", headers=mh).json()
    assert view["can_manage"] is False and view["invites"] == []
    # ...but they can read the plan and use the product.
    assert client.get("/billing/info", headers=mh).status_code == 200


def test_seat_limit_counts_members_and_pending_invites():
    _, h, _ = _owner("free")                       # 1 seat: the owner
    r = client.post("/team/invites", json={"email": _email()}, headers=h)
    assert r.status_code == 402 and "seat" in r.json()["detail"]

    _, h, _ = _owner("pro")                        # 3 seats
    _invite(h)
    again_email, first = _invite(h)
    assert _invite(h, again_email)[1]["id"] == first["id"]      # re-inviting is idempotent
    assert client.post("/team/invites", json={"email": _email()}, headers=h).status_code == 402


def test_invite_rules():
    owner_email, h, _ = _owner()
    other_email, _, _ = _owner("free")
    assert client.post("/team/invites", json={"email": other_email}, headers=h).status_code == 409
    assert client.post("/team/invites", json={"email": owner_email}, headers=h).status_code == 409
    assert client.post("/team/invites", json={"email": "not-an-email"}, headers=h).status_code == 422

    _, inv = _invite(h)
    assert client.post(f"/team/invites/{inv['id']}/revoke", headers=h).status_code == 200
    assert _join(inv["link"])[0].status_code == 400              # withdrawn link is dead
    assert client.get("/team", headers=h).json()["used"] == 1

    # Another workspace can't revoke it, and garbage tokens are rejected.
    _, inv2 = _invite(h)
    _, h2, _ = _owner()
    assert client.post(f"/team/invites/{inv2['id']}/revoke", headers=h2).status_code == 404
    assert client.post("/team/accept", json={"token": "nope", "password": "secret123"}).status_code == 400
    # A login token is not an invitation.
    login_token = h["Authorization"].split()[1]
    assert client.post("/team/accept", json={"token": login_token,
                                             "password": "secret123"}).status_code == 400


def test_removing_a_member_signs_them_out_and_keeps_the_data():
    _, h, tenant = _owner()
    member_email, inv = _invite(h)
    mh = {"Authorization": f"Bearer {_join(inv['link'])[0].json()['token']}"}
    before = repo.kols_count(tenant)

    assert client.post("/team/remove", json={"email": member_email}, headers=h).status_code == 200
    assert client.get("/auth/me", headers=mh).status_code == 401
    assert repo.kols_count(tenant) == before
    assert client.get("/auth/me", headers=h).status_code == 200

    # The owner can't be removed, nor someone from another workspace.
    owner_email = client.get("/auth/me", headers=h).json()["email"]
    assert client.post("/team/remove", json={"email": owner_email}, headers=h).status_code == 422
    stranger, _, _ = _owner()
    assert client.post("/team/remove", json={"email": stranger}, headers=h).status_code == 404


def test_member_leaving_keeps_workspace_owner_leaving_removes_everyone():
    _, h, tenant = _owner()
    m1, inv1 = _invite(h)
    m2, inv2 = _invite(h)
    h1 = {"Authorization": f"Bearer {_join(inv1['link'])[0].json()['token']}"}
    h2 = {"Authorization": f"Bearer {_join(inv2['link'])[0].json()['token']}"}

    assert client.post("/auth/delete-account", json={"password": "memberpass1"},
                       headers=h1).status_code == 200
    assert repo.user_find(m1) is None
    assert repo.kols_count(tenant) > 0 and client.get("/auth/me", headers=h).status_code == 200

    assert client.post("/auth/delete-account", json={"password": "secret123"},
                       headers=h).status_code == 200
    assert repo.user_find(m2) is None
    assert client.get("/auth/me", headers=h2).status_code == 401
    assert repo.kols_count(tenant) == 0


def test_operator_deleting_a_member_does_not_wipe_the_owner():
    owner_email, h, tenant = _owner()
    member_email, inv = _invite(h)
    _join(inv["link"])
    admin = {"X-Admin-Key": KEY}

    rows = {u["email"]: u for u in client.get("/admin/users", headers=admin).json()["users"]}
    assert rows[member_email]["role"] == "member" and rows[owner_email]["role"] == "owner"

    assert client.post("/admin/delete-user", json={"email": member_email},
                       headers=admin).status_code == 200
    assert repo.user_find(owner_email) and repo.kols_count(tenant) > 0
    assert client.get("/auth/me", headers=h).status_code == 200


def test_members_are_locked_out_when_the_plan_lapses():
    _, h, tenant = _owner()
    member_email, inv = _invite(h)
    mh = {"Authorization": f"Bearer {_join(inv['link'])[0].json()['token']}"}
    assert client.get("/kols", headers=mh).status_code == 200

    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    repo.plan_set(tenant, "pro", expires_at=past)             # lapsed -> Free, 1 seat
    r = client.get("/kols", headers=mh)
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    assert client.post("/auth/login", json={"email": member_email,
                                            "password": "memberpass1"}).status_code == 403
    assert client.get("/kols", headers=h).status_code == 200  # the owner keeps working

    repo.plan_set(tenant, "pro")                              # renewed
    assert client.get("/kols", headers=mh).status_code == 200


def test_invitation_email_carries_a_working_link(outbox):
    owner_email, h, _ = _owner()
    member_email = _email("member")
    r = client.post("/team/invites", json={"email": member_email}, headers={**h, "X-Lang": "zh"})
    assert r.json()["emailed"] is True
    msg = outbox[-1]
    assert msg["To"] == member_email and owner_email in msg["Subject"]
    body = msg.get_body(preferencelist=("plain",)).get_content()
    link = next(w for w in body.split() if "#invite=" in w)
    joined, _ = _join(link)
    assert joined.status_code == 200
    assert repo.user_find(member_email)["email_verified"] is True


def test_password_change_only_affects_that_account():
    _, h, _ = _owner()
    _, inv = _invite(h)
    mh = {"Authorization": f"Bearer {_join(inv['link'])[0].json()['token']}"}
    r = client.post("/auth/change-password", headers=mh,
                    json={"current_password": "memberpass1", "new_password": "brandnew123"})
    assert r.status_code == 200
    assert client.get("/auth/me", headers=mh).status_code == 401      # old member token dead
    assert client.get("/auth/me", headers=h).status_code == 200       # owner unaffected


# ── Renewal reminders ─────────────────────────────────────────────
def _expiring(days):
    email, h, tenant = _owner("free")
    end = (datetime.now(timezone.utc) + timedelta(days=days, minutes=5)).isoformat()
    repo.plan_set(tenant, "pro", expires_at=end)
    return email, tenant, end


def _to(outbox, email):
    return [m for m in outbox if m["To"] == email]


def test_reminders_go_out_once_per_stage(outbox):
    week, _, _ = _expiring(6)
    tomorrow, _, _ = _expiring(0)
    far, _, _ = _expiring(30)
    permanent, _, _ = _owner("pro")
    outbox.clear()                                                # drop the sign-up emails

    jobs.send_renewal_reminders()
    assert len(_to(outbox, week)) == 1 and "7 days" in _to(outbox, week)[0]["Subject"]
    assert len(_to(outbox, tomorrow)) == 1 and "tomorrow" in _to(outbox, tomorrow)[0]["Subject"]
    assert not _to(outbox, far) and not _to(outbox, permanent)

    jobs.send_renewal_reminders()                                 # second pass: nothing new
    assert len(_to(outbox, week)) == 1 and len(_to(outbox, tomorrow)) == 1

    # Six days later the same term is due its final notice, then the "ended" one.
    later = datetime.now(timezone.utc) + timedelta(days=5, hours=12)
    jobs.send_renewal_reminders(later)
    assert len(_to(outbox, week)) == 2
    jobs.send_renewal_reminders(later + timedelta(days=1))
    assert len(_to(outbox, week)) == 3 and "ended" in _to(outbox, week)[-1]["Subject"]
    jobs.send_renewal_reminders(later + timedelta(days=2))
    assert len(_to(outbox, week)) == 3


def test_renewing_resets_the_reminder_cycle(outbox):
    email, tenant, _ = _expiring(3)
    outbox.clear()
    jobs.send_renewal_reminders()
    assert len(_to(outbox, email)) == 1
    new_end = (datetime.now(timezone.utc) + timedelta(days=33)).isoformat()
    repo.plan_set(tenant, "pro", expires_at=new_end)
    jobs.send_renewal_reminders()
    assert len(_to(outbox, email)) == 1
    jobs.send_renewal_reminders(datetime.now(timezone.utc) + timedelta(days=30))
    assert len(_to(outbox, email)) == 2


def test_no_reminders_without_smtp_and_nothing_is_marked_sent():
    email, tenant, _ = _expiring(3)
    assert jobs.send_renewal_reminders() == 0
    row = next(p for p in repo.plans_with_expiry() if p["tenant_id"] == tenant)
    assert row["reminded"] is None


def test_old_lapses_are_not_mailed(outbox):
    email, _, tenant = _owner("free")
    repo.plan_set(tenant, "pro",
                  expires_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat())
    outbox.clear()
    jobs.send_renewal_reminders()
    assert not _to(outbox, email)


def test_run_once_reports_every_job_and_shows_in_status():
    result = jobs.run_once()
    assert set(result) == {"reminders", "orders_expired", "demos_swept"}
    st = client.get("/admin/status", headers={"X-Admin-Key": KEY}).json()["jobs"]
    assert st["last_run"] and st["last_result"] == result


# ── Export ────────────────────────────────────────────────────────
def test_export_contains_only_this_workspace():
    owner_email, h, tenant = _owner()
    _owner()                                                      # someone else's data
    r = client.get("/account/export", headers=h)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    doc = r.json()
    assert doc["workspace"]["plan"] == "pro"
    assert [m["email"] for m in doc["members"]] == [owner_email]
    assert len(doc["creators"]) == repo.kols_count(tenant) > 0
    assert "password" not in r.text and tenant not in r.text
