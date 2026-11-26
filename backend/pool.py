"""
Explore — a shared pool of creators that grows as people browse.

Each workspace has its own private creator library. The pool is different: it
is one catalogue for every workspace, filled by the browser extension
(extension/). When someone with the extension installed opens a creator's
public profile, the extension reads what that page shows — handle, name,
follower / like / video counts, bio — and posts it here. Anyone can then find
that creator under Explore and copy them into their own library.

What is and is not shared
-------------------------
Only what the public profile page displays is stored, plus when it was last
seen and how many times it was captured. Nothing from any workspace's library
(fees, ratings, notes, campaigns) ever enters the pool, and no workspace ever
sees who captured a creator.

Keeping fake creators out
-------------------------
Anyone with an account can call the capture endpoint, so a single submission
is never trusted. A new creator starts "pending" and is shown only to the
workspace that captured it. It turns "public" once POOL_MIN_WORKSPACES
different workspaces (default 2) have captured the same profile on their own,
or when the operator approves it in the admin console; the operator can also
hide any entry. Once a creator is public, a capture whose follower count
disagrees wildly with what is stored (more than 3× either way) is ignored, so
one account cannot rewrite a confirmed profile.

The extension authenticates with a "connection code": a long-lived token that
can do exactly one thing — submit captures. It cannot read a workspace, and
it stops working when the account's password changes.

Endpoints
---------
  POST /extension/token      Issue a connection code for the signed-in account
  GET  /extension/download   The extension as a .zip (load unpacked in Chrome)
  POST /pool/capture         Extension: submit one profile
  GET  /pool/search          Find creators in the pool
  POST /pool/{id}/add        Copy a pool creator into this workspace's library
"""
import hashlib
import hmac
import io
import json
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from backend import activity, billing, crud, ratelimit, repo, settings
from backend.auth import DEMO_SUFFIX, _JWT_ALG, _pw_fingerprint, current_user, require_owner, require_tenant
from backend.categories import VALID_CATEGORIES
from backend.db import PoolCapture, PoolCreator, SessionLocal

router = APIRouter()

PLATFORMS = ("tiktok", "instagram", "youtube")
VALID_COUNTRIES = crud.VALID_COUNTRIES
_EXT_TTL = timedelta(days=365)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._\-]{2,64}$")
# Independent workspaces that must capture a profile before everyone sees it.
POOL_MIN_WORKSPACES = max(1, int(os.environ.get("POOL_MIN_WORKSPACES") or 2))
_MAX_FOLLOWERS = 400_000_000          # above the largest real accounts
_DRIFT = 3.0                          # public rows ignore captures off by more than this factor
_EXT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension")
_PROFILE_URL = {
    "tiktok": "https://www.tiktok.com/@{h}",
    "instagram": "https://www.instagram.com/{h}/",
    "youtube": "https://www.youtube.com/@{h}",
}


def tenant_tag(tenant_id: str) -> str:
    """Keyed hash standing in for a workspace id in pool_captures (40 hex
    characters, so it can't be mistaken for a raw 32-character id)."""
    return hmac.new(settings.JWT_SECRET.encode(), b"pool:" + tenant_id.encode(), hashlib.sha256).hexdigest()[:40]


_SHARE = "pool_share"


def sharing(tenant_id: str) -> bool:
    """Has this workspace joined the shared creator pool? Off until it opts in."""
    rows = repo.docs_list(tenant_id, _SHARE, limit=1)
    return bool(rows and rows[0].get("on"))


def migrate_capture_tags() -> int:
    """One-off: replace raw workspace ids left in pool_captures by older
    versions with their hash. Idempotent."""
    changed = 0
    with SessionLocal.begin() as s:
        for row in s.scalars(select(PoolCapture).where(func.length(PoolCapture.tenant_id) != 40)).all():
            tag = tenant_tag(row.tenant_id)
            dup = s.scalar(select(PoolCapture).where(PoolCapture.creator_id == row.creator_id,
                                                     PoolCapture.tenant_id == tag))
            if dup:
                s.delete(row)
            else:
                row.tenant_id = tag
            changed += 1
    return changed


def forget_tenant(tenant_id: str) -> None:
    """Workspace deleted: drop its capture records (by hash) too."""
    from sqlalchemy import delete as _delete
    with SessionLocal.begin() as s:
        s.execute(_delete(PoolCapture).where(PoolCapture.tenant_id == tenant_tag(tenant_id)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_dict(r: PoolCreator) -> dict:
    return {"id": r.id, "platform": r.platform, "handle": r.handle, "name": r.name,
            "profile_url": r.profile_url, "followers": r.followers, "likes": r.likes,
            "videos": r.videos, "bio": r.bio, "verified": bool(r.verified),
            "country": r.country, "category": r.category,
            "first_seen": r.first_seen, "last_seen": r.last_seen, "captures": r.captures,
            "status": r.status or "pending"}


# ── Connection code for the extension ─────────────────────────────
@router.post("/extension/token")
def extension_token(tenant_id: str = Depends(require_tenant)):
    """A token that can only submit captures (see capture_user below)."""
    user = current_user()
    if not user or user["email"].endswith(DEMO_SUFFIX):
        raise HTTPException(status_code=403,
                            detail="Create a free account first — demo sessions can't connect the extension.")
    ratelimit.enforce("ext-token", tenant_id, limit=10, window=3600)
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": tenant_id, "email": user["email"], "purpose": "ext",
                        "pv": _pw_fingerprint(user["password_hash"]), "iat": now, "exp": now + _EXT_TTL},
                       settings.JWT_SECRET, algorithm=_JWT_ALG)
    return {"token": token, "expires_at": (now + _EXT_TTL).isoformat()}


def capture_user(authorization: Optional[str] = Header(None)) -> dict:
    """The account behind an extension connection code. Accepts only tokens
    issued for the extension, so a leaked code cannot open a workspace."""
    bad = HTTPException(status_code=401, detail="This connection code is invalid or has expired — "
                                                "create a new one under Explore.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise bad
    try:
        payload = jwt.decode(authorization.split(" ", 1)[1].strip(), settings.JWT_SECRET, algorithms=[_JWT_ALG])
    except jwt.PyJWTError:
        raise bad
    if payload.get("purpose") != "ext":
        raise bad
    user = repo.user_find(payload.get("email") or "")
    if (not user or user["tenant_id"] != payload.get("sub")
            or payload.get("pv") != _pw_fingerprint(user["password_hash"]) or user.get("disabled")):
        raise bad
    return user


@router.get("/extension/download")
def extension_download():
    """The extension's source as a zip — unzip and "Load unpacked" in Chrome."""
    if not os.path.isdir(_EXT_DIR):
        raise HTTPException(status_code=404, detail="The extension is not bundled with this server.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for folder, dirs, files in os.walk(_EXT_DIR):
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "__")))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, _EXT_DIR).replace(os.sep, "/")
                z.write(path, "lumio-capture/" + rel)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="lumio-capture.zip"'})


def extension_version() -> str:
    """Version in the bundled manifest — the extension compares its own to this."""
    try:
        with open(os.path.join(_EXT_DIR, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version", ""))
    except (OSError, ValueError):
        return ""


@router.get("/extension/ping")
def extension_ping(user: dict = Depends(capture_user)):
    """Lets the extension check its connection code when it connects, show
    which account it sends to, and notice when a newer build is available."""
    return {"ok": True, "email": user["email"], "latest_version": extension_version(),
            "lang": user.get("lang") or "en"}


# ── Capture ───────────────────────────────────────────────────────
class Capture(BaseModel):
    platform: str = Field("tiktok", max_length=16)
    handle: str = Field(..., max_length=80)
    name: str = Field("", max_length=120)
    followers: int = Field(..., ge=0, le=_MAX_FOLLOWERS)
    likes: Optional[int] = Field(None, ge=0, le=200_000_000_000)
    videos: Optional[int] = Field(None, ge=0, le=10_000_000)
    bio: str = Field("", max_length=600)
    verified: bool = False
    country: Optional[str] = Field(None, max_length=4)


@router.post("/pool/capture")
def capture(req: Capture, user: dict = Depends(capture_user)):
    ratelimit.enforce("pool-capture", user["tenant_id"], limit=600, window=3600,
                      detail="Capturing very quickly — the extension will try again later.")
    platform = req.platform.strip().lower()
    handle = req.handle.strip().lstrip("@")
    if platform not in PLATFORMS:
        raise HTTPException(status_code=422, detail=f"platform must be one of: {', '.join(PLATFORMS)}")
    if not _HANDLE_RE.match(handle):
        raise HTTPException(status_code=422, detail="That does not look like a creator handle.")
    country = (req.country or "").strip().upper() or None
    if country not in VALID_COUNTRIES:
        country = None                              # outside our markets, or unknown

    key = handle.lower()
    name = " ".join(req.name.split())[:120] or handle
    bio = " ".join(req.bio.split())[:300]
    if req.likes is not None and req.videos == 0 and req.likes > 0:
        raise HTTPException(status_code=422, detail="Those profile numbers don't add up.")
    tag = tenant_tag(user["tenant_id"])
    shared = sharing(user["tenant_id"])
    now = _now()
    with SessionLocal.begin() as s:
        row = s.scalar(select(PoolCreator).where(PoolCreator.platform == platform,
                                                 PoolCreator.handle_key == key))
        created = row is None
        if created:
            row = PoolCreator(platform=platform, handle=handle, handle_key=key, first_seen=now,
                              captures=0, status="pending", confirmations=0)
            s.add(row)
            s.flush()
        # A confirmed profile is not rewritten by a capture that disagrees wildly.
        stored = row.followers or 0
        drifted = (row.status == "public" and stored > 0
                   and not (stored / _DRIFT <= req.followers <= stored * _DRIFT))
        # A workspace outside the shared pool never edits what others see.
        drifted = drifted or (row.status == "public" and not shared)
        if not drifted:
            row.name, row.bio, row.verified = name, bio, bool(req.verified)
            row.followers = req.followers
            if req.likes is not None:
                row.likes = req.likes
            if req.videos is not None:
                row.videos = req.videos
            if country:
                row.country = country
        row.profile_url = _PROFILE_URL[platform].format(h=handle)
        row.last_seen = now
        row.captures = (row.captures or 0) + 1
        seen = s.scalar(select(PoolCapture).where(PoolCapture.creator_id == row.id,
                                                  PoolCapture.tenant_id == tag))
        if seen:
            seen.last_at, seen.shared = now, shared
        else:
            s.add(PoolCapture(creator_id=row.id, tenant_id=tag, first_at=now, last_at=now, shared=shared))
            s.flush()
        row.confirmations = s.scalar(select(func.count()).select_from(PoolCapture)
                                     .where(PoolCapture.creator_id == row.id,
                                            PoolCapture.shared.is_(True))) or 0
        if row.status == "pending" and row.confirmations >= POOL_MIN_WORKSPACES:
            row.status = "public"
        s.flush()
        out = _to_dict(row)
    return {"status": "created" if created else "updated", "creator": out, "ignored": drifted, "shared": shared}


class Sharing(BaseModel):
    on: bool


@router.get("/pool/sharing")
def get_sharing(tenant_id: str = Depends(require_tenant)):
    return {"on": sharing(tenant_id)}


@router.put("/pool/sharing")
def set_sharing(req: Sharing, tenant_id: str = Depends(require_owner)):
    """Join or leave the shared creator pool. Only public profile numbers are
    ever shared, never which workspace captured them."""
    now = datetime.now(timezone.utc).isoformat()
    rows = repo.docs_list(tenant_id, _SHARE, limit=5)
    for r in rows:
        repo.doc_delete(tenant_id, _SHARE, r["id"])
    repo.doc_add(tenant_id, _SHARE, {"on": bool(req.on)}, now)
    with SessionLocal.begin() as s:
        caps = s.scalars(select(PoolCapture).where(PoolCapture.tenant_id == tenant_tag(tenant_id))).all()
        for c in caps:
            c.shared = bool(req.on)
        s.flush()
        for cid in {c.creator_id for c in caps}:
            row = s.get(PoolCreator, cid)
            if row is None:
                continue
            row.confirmations = s.scalar(select(func.count()).select_from(PoolCapture)
                                         .where(PoolCapture.creator_id == cid, PoolCapture.shared.is_(True))) or 0
            if row.status == "pending" and row.confirmations >= POOL_MIN_WORKSPACES:
                row.status = "public"
    activity.record(tenant_id, "pool.sharing", "on" if req.on else "off")
    return {"on": bool(req.on)}


# ── Starter catalogue ─────────────────────────────────────────────
def seed_from_library(records=None) -> int:
    """Put the curated real creators (the seed library every workspace starts
    with) into the pool as confirmed entries, so Explore is not empty on day
    one. Only creators with a TikTok profile link; existing pool rows are left
    untouched. Idempotent; returns how many were added."""
    from backend import tenancy
    records = tenancy.load_template_seed() if records is None else records
    added, now = 0, _now()
    with SessionLocal.begin() as s:
        for rec in records:
            if rec.get("source") == "synthetic":
                continue
            m = re.match(r"^https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9._\-]{2,64})/?$",
                         (rec.get("tiktok_url") or "").strip())
            if not m:
                continue
            handle = m.group(1)
            key = handle.lower()
            if s.scalar(select(PoolCreator.id).where(PoolCreator.platform == "tiktok",
                                                     PoolCreator.handle_key == key)):
                continue
            country = (rec.get("country") or "").upper()
            category = rec.get("category")
            s.add(PoolCreator(
                platform="tiktok", handle=handle, handle_key=key,
                name=(rec.get("name") or handle)[:120], profile_url=_PROFILE_URL["tiktok"].format(h=handle),
                followers=int(rec.get("followers") or 0), verified=False,
                country=country if country in VALID_COUNTRIES else None,
                category=category if category in VALID_CATEGORIES else None,
                first_seen=now, last_seen=now, captures=0, status="public", confirmations=0))
            added += 1
    return added


# ── Explore ───────────────────────────────────────────────────────
def _library_urls(tenant_id: str) -> set:
    return {(k.get("tiktok_url") or "").strip().lower().rstrip("/")
            for k in repo.kols_all(tenant_id) if k.get("tiktok_url")}


@router.get("/pool/search")
def search(q: Optional[str] = Query(None, max_length=80), country: Optional[str] = Query(None),
           min_followers: Optional[int] = Query(None, ge=0), max_followers: Optional[int] = Query(None, ge=0),
           sort: str = Query("followers"), limit: int = Query(24, ge=1, le=60), offset: int = Query(0, ge=0),
           tenant_id: str = Depends(require_tenant)):
    country = (country or "").upper() or None
    if country and country not in VALID_COUNTRIES:
        raise HTTPException(status_code=422, detail=f"country must be one of: {', '.join(VALID_COUNTRIES)}")
    # Everyone sees confirmed creators; a pending one only by the workspace that captured it.
    mine_ids = select(PoolCapture.creator_id).where(PoolCapture.tenant_id == tenant_tag(tenant_id))
    visible = or_(PoolCreator.status == "public",
                  (PoolCreator.status == "pending") & PoolCreator.id.in_(mine_ids))
    stmt = select(PoolCreator).where(visible)
    if q and q.strip():
        like = "%" + q.strip().lower().lstrip("@").replace("%", "").replace("_", r"\_") + "%"
        stmt = stmt.where(or_(PoolCreator.handle_key.like(like, escape="\\"),
                              func.lower(PoolCreator.name).like(like, escape="\\"),
                              func.lower(PoolCreator.bio).like(like, escape="\\")))
    if country:
        stmt = stmt.where(PoolCreator.country == country)
    if min_followers is not None:
        stmt = stmt.where(PoolCreator.followers >= min_followers)
    if max_followers is not None:
        stmt = stmt.where(PoolCreator.followers <= max_followers)
    order = {"followers": PoolCreator.followers.desc(), "recent": PoolCreator.last_seen.desc(),
             "likes": PoolCreator.likes.desc()}.get(sort, PoolCreator.followers.desc())
    with SessionLocal() as s:
        total = s.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        pool_size = s.scalar(select(func.count()).select_from(PoolCreator).where(visible)) or 0
        rows = s.scalars(stmt.order_by(order, PoolCreator.id).limit(limit).offset(offset)).all()
        items = [_to_dict(r) for r in rows]
    mine = _library_urls(tenant_id)
    for it in items:
        it["in_library"] = it["profile_url"].lower().rstrip("/") in mine
    return {"items": items, "total": total, "pool_size": pool_size}


class AddToLibrary(BaseModel):
    country: str = Field(..., max_length=4)
    category: str = Field(..., max_length=32)


@router.post("/pool/{creator_id}/add")
def add_to_library(creator_id: int, req: AddToLibrary, tenant_id: str = Depends(require_tenant)):
    """Copy a pool creator into this workspace's library. The pool only knows
    public counts, so the fee is estimated from the audience size and marked as
    an estimate; engagement comes from likes per video when the page showed them."""
    country = req.country.strip().upper()
    if country not in VALID_COUNTRIES:
        raise HTTPException(status_code=422, detail=f"country must be one of: {', '.join(VALID_COUNTRIES)}")
    if req.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}")
    with SessionLocal() as s:
        row = s.get(PoolCreator, creator_id)
        ok = row is not None and (row.status == "public" or (row.status == "pending" and s.scalar(
            select(PoolCapture.id).where(PoolCapture.creator_id == row.id,
                                         PoolCapture.tenant_id == tenant_tag(tenant_id))) is not None))
        pc = _to_dict(row) if ok else None
    if not pc:
        raise HTTPException(status_code=404, detail="Creator not found")
    if pc["profile_url"].lower().rstrip("/") in _library_urls(tenant_id):
        raise HTTPException(status_code=409, detail="This creator is already in your library.")
    billing.check_creator_capacity(tenant_id, adding=1)

    followers = max(int(pc["followers"] or 0), 1)
    avg_likes = None
    engagement = 0.04                                   # typical rate when the page gave no likes
    if pc["likes"] and pc["videos"]:
        avg_likes = int(pc["likes"] / max(pc["videos"], 1))
        engagement = min(0.30, max(0.005, avg_likes / followers))
    record = crud._auto_fill({
        "name": pc["name"] or pc["handle"], "country": country, "category": req.category,
        "followers": followers, "engagement_rate": round(engagement, 4),
        "fit_score": 0.75, "commission_rate": 0.15,
        "cost": round(float(crud._estimate_cost(followers)), 2), "cost_estimated": True,
        "avg_views": None, "avg_likes": avg_likes, "gender_ratio": None, "age_group": None,
        "tiktok_url": pc["profile_url"], "content_type": "both", "source": "explore",
    })
    saved = repo.kols_add(tenant_id, record)
    activity.record(tenant_id, "creator.explore_add", record["name"], f"@{pc['handle']}")
    return {"status": "added", "creator": saved}
