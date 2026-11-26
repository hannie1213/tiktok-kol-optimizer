"""
Explore pool: extension connection codes, capture, search, add to library.
"""
import io
import os
import sys
import uuid
import zipfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import repo

client = TestClient(app)


def _account():
    email = f"pool_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    return h, client.get("/auth/me", headers=h).json()["tenant_id"]


def _ext(h):
    r = client.post("/extension/token", headers=h)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _handle():
    return "creator_" + uuid.uuid4().hex[:10]


def _capture(ext, handle=None, **over):
    body = {"platform": "tiktok", "handle": handle or _handle(), "name": "Aina Glow", "followers": 480000,
            "likes": 9_600_000, "videos": 320, "bio": "Skincare reviews · KL", "verified": True, "country": "MY"}
    body.update(over)
    return client.post("/pool/capture", headers=ext, json=body)


def test_connection_code_can_capture_but_cannot_open_the_workspace():
    h, _ = _account()
    ext = _ext(h)
    assert _capture(ext).status_code == 200
    # the code is not a session …
    for path in ("/auth/me", "/kols?limit=1", "/pool/search", "/billing/info"):
        assert client.get(path, headers=ext).status_code == 401, path
    # … and a session token is not a connection code
    assert _capture(h).status_code == 401
    assert _capture({}).status_code == 401
    assert _capture({"Authorization": "Bearer nonsense"}).status_code == 401


def test_emailed_link_tokens_are_not_sessions_either():
    from backend import auth
    h, tenant = _account()
    user = repo.user_find_by_tenant(tenant)
    reset = auth._action_token("reset", user, auth._RESET_TTL)
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {reset}"}).status_code == 401
    assert client.get("/auth/me", headers=h).status_code == 200


def test_changing_the_password_revokes_the_connection_code():
    h, _ = _account()
    ext = _ext(h)
    assert _capture(ext).status_code == 200
    r = client.post("/auth/change-password", headers=h,
                    json={"current_password": "secret123", "new_password": "brandnew456"})
    assert r.status_code == 200
    assert _capture(ext).status_code == 401


def test_demo_sessions_cannot_connect_the_extension():
    demo = client.post("/auth/demo").json()
    assert client.post("/extension/token",
                       headers={"Authorization": f"Bearer {demo['token']}"}).status_code == 403


def test_capture_creates_then_updates_one_shared_record():
    a, _ = _account()
    b, _ = _account()
    handle = _handle()
    r = _capture(_ext(a), handle)
    assert r.json()["status"] == "created"
    c = r.json()["creator"]
    assert c["profile_url"] == f"https://www.tiktok.com/@{handle}" and c["captures"] == 1 and c["country"] == "MY"

    # someone in another workspace sees a newer follower count; case differences are the same creator
    r = _capture(_ext(b), "@" + handle.upper(), followers=500000, likes=None, country="US")
    assert r.json()["status"] == "updated"
    c = r.json()["creator"]
    assert c["followers"] == 500000 and c["captures"] == 2
    assert c["likes"] == 9_600_000          # a capture without likes keeps the old figure
    assert c["country"] == "MY"             # a country outside our markets does not overwrite

    found = client.get(f"/pool/search?q={handle}", headers=a).json()
    assert found["total"] == 1 and found["items"][0]["followers"] == 500000
    assert "captured_by" not in found["items"][0] and "tenant" not in str(found["items"][0])


def test_capture_validation():
    h, _ = _account()
    ext = _ext(h)
    assert _capture(ext, "not a handle!").status_code == 422
    assert _capture(ext, platform="myspace").status_code == 422
    assert _capture(ext, followers=-1).status_code == 422
    long_bio = _capture(ext, bio="x" * 600).json()["creator"]
    assert len(long_bio["bio"]) == 300


def test_search_filters_sorts_and_marks_library_members():
    h, tenant = _account()
    ext = _ext(h)
    tag = uuid.uuid4().hex[:8]
    big, small = f"{tag}_big", f"{tag}_small"
    _capture(ext, big, followers=2_000_000, country="ID", name="Big One")
    _capture(ext, small, followers=30_000, country="MY", name="Small One")

    get = lambda qs: client.get("/pool/search?" + qs, headers=h).json()
    both = get(f"q={tag}")
    assert [i["handle"] for i in both["items"]] == [big, small] and both["pool_size"] >= 2
    assert [i["handle"] for i in get(f"q={tag}&country=MY")["items"]] == [small]
    assert [i["handle"] for i in get(f"q={tag}&min_followers=100000")["items"]] == [big]
    assert [i["handle"] for i in get(f"q={tag}&max_followers=100000")["items"]] == [small]
    assert get(f"q={tag}&limit=1&offset=1")["items"][0]["handle"] == small
    assert get("q=zz_no_such_creator_zz")["items"] == []
    assert client.get("/pool/search?country=XX", headers=h).status_code == 422
    assert not any(i["in_library"] for i in both["items"])


def test_add_to_library_estimates_what_the_page_did_not_show():
    h, tenant = _account()
    handle = _handle()
    cid = _capture(_ext(h), handle).json()["creator"]["id"]
    before = repo.kols_count(tenant)

    r = client.post(f"/pool/{cid}/add", headers=h, json={"country": "my", "category": "beauty"})
    assert r.status_code == 200, r.text
    k = r.json()["creator"]
    assert k["name"] == "Aina Glow" and k["country"] == "MY" and k["category"] == "beauty"
    assert k["followers"] == 480000 and k["tiktok_url"].endswith("@" + handle)
    assert k["avg_likes"] == 30000 and k["engagement_rate"] == round(30000 / 480000, 4)
    assert k["cost"] > 0 and k["cost_estimated"] is True
    assert repo.kols_count(tenant) == before + 1

    assert client.get(f"/pool/search?q={handle}", headers=h).json()["items"][0]["in_library"] is True
    assert client.post(f"/pool/{cid}/add", headers=h,
                       json={"country": "MY", "category": "beauty"}).status_code == 409
    # another workspace's library is untouched, and it can add the same creator itself
    other, other_tenant = _account()
    assert client.get(f"/pool/search?q={handle}", headers=other).json()["items"] == []   # still unconfirmed
    _capture(_ext(other), handle)                                   # a second workspace confirms it
    assert client.get(f"/pool/search?q={handle}", headers=other).json()["items"][0]["in_library"] is False
    assert client.post(f"/pool/{cid}/add", headers=other,
                       json={"country": "SG", "category": "personal_care"}).status_code == 200

    assert client.post(f"/pool/{cid}/add", headers=h, json={"country": "XX", "category": "beauty"}).status_code == 422
    assert client.post(f"/pool/{cid}/add", headers=h, json={"country": "MY", "category": "nope"}).status_code == 422
    assert client.post("/pool/99999999/add", headers=h, json={"country": "MY", "category": "beauty"}).status_code == 404


def test_extension_zip_contains_a_loadable_extension():
    r = client.get("/extension/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    for f in ("manifest.json", "background.js", "content.js", "extract.js", "popup.html", "popup.js"):
        assert f"lumio-capture/{f}" in names


def test_extension_zip_includes_icons_and_ping_reports_account():
    names = zipfile.ZipFile(io.BytesIO(client.get("/extension/download").content)).namelist()
    assert "lumio-capture/icons/icon128.png" in names
    assert "lumio-capture/guide.html" in names and "lumio-capture/README.txt" in names
    h, _tenant = _account()
    email = client.get("/auth/me", headers=h).json()["email"]
    d = client.get("/extension/ping", headers=_ext(h)).json()
    assert d["ok"] and d["email"] == email and d["latest_version"]
    # a normal sign-in token is not an extension code
    assert client.get("/extension/ping", headers=h).status_code == 401


# ── Keeping fake creators out of the shared pool ──────────────────
ADMIN = {"X-Admin-Key": "pool-admin-key-for-tests"}


def _admin(monkeypatch):
    from backend import settings
    monkeypatch.setattr(settings, "ADMIN_KEY", ADMIN["X-Admin-Key"])


def test_one_workspace_alone_cannot_publish_a_creator():
    a, _ = _account()
    b, _ = _account()
    handle = _handle()
    c = _capture(_ext(a), handle, name="Totally Real", followers=9_000_000).json()["creator"]
    assert c["status"] == "pending"
    # the capturing workspace sees it (marked pending); nobody else does, and nobody else can add it
    mine = client.get(f"/pool/search?q={handle}", headers=a).json()
    assert mine["items"][0]["status"] == "pending"
    theirs = client.get(f"/pool/search?q={handle}", headers=b).json()
    assert theirs["items"] == [] and theirs["total"] == 0
    assert client.post(f"/pool/{c['id']}/add", headers=b,
                       json={"country": "MY", "category": "beauty"}).status_code == 404
    # capturing again from the same workspace does not count as a confirmation
    assert _capture(_ext(a), handle, followers=9_000_000).json()["creator"]["status"] == "pending"
    # an independent capture from another workspace confirms it
    assert _capture(_ext(b), handle, followers=9_100_000).json()["creator"]["status"] == "public"
    other, _ = _account()
    assert client.get(f"/pool/search?q={handle}", headers=other).json()["items"][0]["status"] == "public"


def test_a_confirmed_profile_ignores_wildly_different_numbers():
    a, _ = _account()
    b, _ = _account()
    handle = _handle()
    _capture(_ext(a), handle, followers=200_000)
    _capture(_ext(b), handle, followers=210_000)
    r = _capture(_ext(a), handle, followers=150_000_000, bio="DM me: scam.example").json()
    assert r["ignored"] is True
    assert r["creator"]["followers"] == 210_000 and "scam" not in r["creator"]["bio"]
    # ordinary growth is still accepted
    assert _capture(_ext(a), handle, followers=260_000).json()["creator"]["followers"] == 260_000


def test_implausible_profiles_are_rejected():
    h, _ = _account()
    ext = _ext(h)
    assert _capture(ext, followers=900_000_000).status_code == 422
    assert _capture(ext, likes=5000, videos=0).status_code == 422


def test_operator_can_approve_and_hide(monkeypatch):
    _admin(monkeypatch)
    a, _ = _account()
    b, _ = _account()
    handle = _handle()
    cid = _capture(_ext(a), handle).json()["creator"]["id"]
    pending = client.get("/admin/pool?status=pending&q=" + handle, headers=ADMIN).json()
    assert [i["handle"] for i in pending["items"]] == [handle] and pending["counts"]["pending"] >= 1

    assert client.post(f"/admin/pool/{cid}/status", headers=ADMIN, json={"status": "public"}).status_code == 200
    assert client.get(f"/pool/search?q={handle}", headers=b).json()["total"] == 1
    assert client.post(f"/admin/pool/{cid}/status", headers=ADMIN, json={"status": "hidden"}).status_code == 200
    assert client.get(f"/pool/search?q={handle}", headers=b).json()["total"] == 0
    assert client.get(f"/pool/search?q={handle}", headers=a).json()["total"] == 0    # hidden for everyone
    # more captures never bring a hidden creator back
    _capture(_ext(b), handle)
    assert client.get(f"/pool/search?q={handle}", headers=b).json()["total"] == 0
    assert client.post(f"/admin/pool/{cid}/status", headers=ADMIN, json={"status": "nope"}).status_code == 422
    assert client.post(f"/admin/pool/{cid}/status", json={"status": "public"}).status_code == 403
