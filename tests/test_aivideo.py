"""
AI video clips: the Replicate integration with the provider faked out.
"""
import os
import sys
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.main import app
from backend import aivideo, billing, repo

client = TestClient(app)
IMG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD"


def _account(plan=None):
    email = f"vid_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    tenant = client.get("/auth/me", headers=h).json()["tenant_id"]
    if plan:
        repo.plan_set(tenant, plan)
    return h, tenant


@pytest.fixture
def provider(monkeypatch):
    """A fake Replicate: records calls; `state` says what GET returns next."""
    box = {"created": [], "state": {"status": "processing"}, "downloads": [], "fail_create": None}

    def create(model_input):
        if box["fail_create"]:
            raise box["fail_create"]
        box["created"].append(model_input)
        return {"id": f"pred_{len(box['created'])}", "status": "starting"}

    def get(pid):
        return dict(box["state"])

    def download(url, path):
        box["downloads"].append(url)
        with open(path, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42fake-video")
        return 22

    monkeypatch.setattr(aivideo, "TOKEN", "r8_test")
    monkeypatch.setattr(aivideo, "provider_create", create)
    monkeypatch.setattr(aivideo, "provider_get", get)
    monkeypatch.setattr(aivideo, "provider_download", download)
    return box


def _start(h, **over):
    body = {"prompt": "A glass serum bottle on wet stone, slow push-in", "duration": 5, "aspect": "9:16"}
    body.update(over)
    return client.post("/studio/videos", headers=h, json=body)


def test_disabled_without_a_token(monkeypatch):
    monkeypatch.setattr(aivideo, "TOKEN", "")
    h, _ = _account()
    assert client.get("/studio/videos/config", headers=h).json()["enabled"] is False
    r = _start(h)
    assert r.status_code == 503
    assert client.post("/studio/videos", headers={**h, "X-Lang": "zh"},
                       json={"prompt": "abcd efgh"}).json()["detail"] == "服务器尚未开启 AI 视频功能。"


def test_full_lifecycle_downloads_once_and_serves_the_file(provider):
    h, tenant = _account("pro")
    r = _start(h, image=IMG)
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["status"] == "starting" and job["has_image"] is True and job["ready"] is False
    sent = provider["created"][0]
    assert sent["prompt"].startswith("A glass serum") and sent["duration"] == 5
    assert sent["aspect_ratio"] == "9:16" and sent["resolution"] == aivideo.RESOLUTION and sent["image"] == IMG
    assert "prediction_id" not in job                       # internal ids stay internal

    assert client.get(f"/studio/videos/{job['id']}", headers=h).json()["status"] == "processing"
    assert client.get(f"/studio/videos/{job['id']}/file", headers=h).status_code == 404

    provider["state"] = {"status": "succeeded", "output": "https://replicate.delivery/x/out.mp4"}
    done = client.get(f"/studio/videos/{job['id']}", headers=h).json()
    assert done["status"] == "succeeded" and done["ready"] is True and done["size"] == 22
    client.get(f"/studio/videos/{job['id']}", headers=h)    # polling again must not re-download
    assert provider["downloads"] == ["https://replicate.delivery/x/out.mp4"]

    f = client.get(f"/studio/videos/{job['id']}/file", headers=h)
    assert f.status_code == 200 and f.headers["content-type"] == "video/mp4" and b"ftyp" in f.content
    other, _ = _account()
    assert client.get(f"/studio/videos/{job['id']}/file", headers=other).status_code == 404
    assert client.get(f"/studio/videos/{job['id']}", headers=other).status_code == 404

    listing = client.get("/studio/videos", headers=h).json()["items"]
    assert [j["id"] for j in listing] == [job["id"]]
    path = aivideo._path(tenant, job["id"])
    assert os.path.isfile(path)
    assert client.delete(f"/studio/videos/{job['id']}", headers=h).status_code == 200
    assert not os.path.exists(path) and client.get("/studio/videos", headers=h).json()["items"] == []


def test_list_output_and_provider_failure(provider):
    h, _ = _account("pro")
    a = _start(h).json()
    provider["state"] = {"status": "succeeded", "output": ["https://replicate.delivery/y/clip.mp4"]}
    assert client.get(f"/studio/videos/{a['id']}", headers=h).json()["ready"] is True

    b = _start(h).json()
    provider["state"] = {"status": "failed", "error": "NSFW content detected in a very long technical message"}
    failed = client.get(f"/studio/videos/{b['id']}", headers=h).json()
    assert failed["status"] == "failed" and "NSFW" not in failed["error"]

    c = _start(h).json()
    provider["state"] = {"status": "succeeded", "output": None}
    assert client.get(f"/studio/videos/{c['id']}", headers=h).json()["status"] == "failed"


def test_quota_counts_only_clips_that_did_not_fail(provider, monkeypatch):
    monkeypatch.setitem(billing.PLANS["free"], "monthly_ai_videos", 1)
    h, _ = _account()
    cfg = client.get("/studio/videos/config", headers=h).json()
    assert cfg["enabled"] and cfg["limit"] == 1 and cfg["used"] == 0 and cfg["durations"] == [5, 10]

    first = _start(h).json()
    r = _start(h)
    assert r.status_code == 402 and "1 clips" in r.json()["detail"]
    provider["state"] = {"status": "failed", "error": "x"}
    client.get(f"/studio/videos/{first['id']}", headers=h)          # the failure frees the slot
    assert client.get("/studio/videos/config", headers=h).json()["used"] == 0
    assert _start(h).status_code == 200


def test_a_rejected_request_is_not_charged(provider):
    h, _ = _account("pro")
    provider["fail_create"] = urllib.error.URLError("down")
    r = _start(h)
    assert r.status_code == 502
    items = client.get("/studio/videos", headers=h).json()["items"]
    assert items[0]["status"] == "failed"
    assert client.get("/studio/videos/config", headers=h).json()["used"] == 0


def test_stuck_jobs_are_given_up(provider, monkeypatch):
    h, tenant = _account("pro")
    job = _start(h).json()
    doc = repo.doc_get(tenant, aivideo.KIND, job["id"])
    old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
    with repo._session() as s:                                     # age the job
        from backend.db import TenantDoc
        s.get(TenantDoc, job["id"]).created_at = old
    assert doc
    provider["state"] = {"status": "processing"}
    assert client.get(f"/studio/videos/{job['id']}", headers=h).json()["status"] == "failed"


def test_validation_and_concurrency(provider):
    h, _ = _account("team")
    assert _start(h, duration=7).status_code == 422
    assert _start(h, aspect="4:5").status_code == 422
    assert _start(h, prompt="hi").status_code == 422
    assert _start(h, image="data:text/html;base64,PGgxPg==").status_code == 422
    assert _start(h, image="https://example.com/a.jpg").status_code == 422
    for _ in range(3):
        assert _start(h).status_code == 200
    assert _start(h).status_code == 409                            # three already running


def test_deleting_the_workspace_removes_its_video_files(provider):
    h, tenant = _account("pro")
    job = _start(h).json()
    provider["state"] = {"status": "succeeded", "output": "https://replicate.delivery/z.mp4"}
    client.get(f"/studio/videos/{job['id']}", headers=h)
    folder = os.path.dirname(aivideo._path(tenant, job["id"]))
    assert os.path.isdir(folder)
    assert client.post("/auth/delete-account", headers=h, json={"password": "secret123"}).status_code == 200
    assert not os.path.exists(folder)


def test_finished_clips_go_to_the_bucket_when_one_is_configured(provider, monkeypatch):
    from backend import objstore
    stored, deleted = {}, []

    def put_file(key, path, content_type="application/octet-stream"):
        with open(path, "rb") as f:
            stored[key] = f.read()

    monkeypatch.setattr(objstore, "enabled", lambda: True)
    monkeypatch.setattr(objstore, "put_file", put_file)
    monkeypatch.setattr(objstore, "delete", lambda key: deleted.append(key))
    monkeypatch.setattr(objstore, "presigned_get",
                        lambda key, expires=600, filename=None: f"https://bucket.example/{key}?sig=1")
    h, tenant = _account("pro")
    job = _start(h).json()
    provider["state"] = {"status": "succeeded", "output": "https://replicate.delivery/b.mp4"}
    assert client.get(f"/studio/videos/{job['id']}", headers=h).json()["ready"] is True
    key = f"videos/{tenant}/{job['id']}.mp4"
    assert b"ftyp" in stored[key]
    assert not os.path.exists(aivideo._path(tenant, job["id"]))      # the local copy is not kept

    f = client.get(f"/studio/videos/{job['id']}/file", headers=h, follow_redirects=False)
    assert f.status_code == 307 and f.headers["location"].startswith("https://bucket.example/" + key)
    assert client.delete(f"/studio/videos/{job['id']}", headers=h).status_code == 200
    assert deleted == [key]


def test_presigned_urls_are_well_formed(monkeypatch):
    from backend import objstore
    monkeypatch.setattr(objstore, "ENDPOINT", "https://acc.r2.cloudflarestorage.com")
    monkeypatch.setattr(objstore, "BUCKET", "lumio")
    monkeypatch.setattr(objstore, "ACCESS_KEY", "AKID")
    monkeypatch.setattr(objstore, "SECRET_KEY", "secret")
    url = objstore.presigned_get("videos/t1/5.mp4", expires=300, filename="clip.mp4")
    assert url.startswith("https://acc.r2.cloudflarestorage.com/lumio/videos/t1/5.mp4?")
    for part in ("X-Amz-Expires=300", "X-Amz-Credential=AKID%2F", "X-Amz-Signature=", "response-content-disposition="):
        assert part in url
