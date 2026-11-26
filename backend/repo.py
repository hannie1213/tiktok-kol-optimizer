"""
Repository layer — all tenant-scoped database access.

Every function opens one short transaction, scopes every statement by
`tenant_id`, and speaks plain dicts in the exact shapes the API modules used in
the JSON-file era, so the endpoint code above this layer is storage-agnostic.

Legacy migration: the first time a tenant (or the user store) is touched, any
pre-database JSON files for it under data/tenants/{id}/ (and data/users.json)
are imported automatically, so existing deployments upgrade in place with no
manual migration step.
"""
import contextlib
import json
import os
import threading
from typing import List, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from backend import tenancy
from backend.db import (
    AdminAudit,
    ApiInterest,
    Calibration,
    Campaign,
    Kol,
    KolSnapshot,
    PaymentOrder,
    SessionLocal,
    Tenant,
    TenantPlan,
    UsageCounter,
    User,
    engine,
    TeamInvite,
    TenantDoc,
)

# Canonical creator fields stored as real columns; anything else round-trips
# through Kol.extra.
_KOL_COLUMNS = (
    "name", "country", "category", "followers", "engagement_rate", "fit_score",
    "commission_rate", "cost", "avg_views", "avg_likes", "gender_ratio",
    "age_group", "tiktok_url", "source", "cost_estimated",
)

# SQLite is a single-writer database: serialise this process's writers so they
# queue on a lock instead of colliding on "database is locked" (cross-process
# safety comes from WAL + busy_timeout). Server databases (PostgreSQL) handle
# concurrent writers natively, so the lock becomes a no-op there.
_write_lock = (threading.Lock() if engine.url.get_backend_name() == "sqlite"
               else contextlib.nullcontext())


def _session():
    return SessionLocal.begin()


# ════════════════════════════════════════════════════════════════
#  Dict ⇄ row mapping
# ════════════════════════════════════════════════════════════════
def _row_to_dict(row: Kol) -> dict:
    d = {"id": row.kol_id}
    for f in _KOL_COLUMNS:
        d[f] = getattr(row, f)
    if row.extra:
        for k, v in row.extra.items():
            d.setdefault(k, v)
    return d


def _dict_to_row_fields(record: dict) -> dict:
    fields = {f: record.get(f) for f in _KOL_COLUMNS if f in record}
    fields.setdefault("source", "fastmoss")
    fields.setdefault("cost_estimated", False)
    extra = {k: v for k, v in record.items() if k not in _KOL_COLUMNS and k != "id"}
    if extra:
        fields["extra"] = extra
    return fields


# ════════════════════════════════════════════════════════════════
#  Tenant lifecycle + legacy JSON import
# ════════════════════════════════════════════════════════════════
def _read_legacy_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _import_legacy_tenant(s, tenant_id: str):
    """One-time import of a pre-database tenant folder into the tables."""
    d = tenancy.tenant_dir(tenant_id)
    for rec in _read_legacy_json(os.path.join(d, "sample_kols.json"), []):
        s.add(Kol(tenant_id=tenant_id, kol_id=rec.get("id", 0), **_dict_to_row_fields(rec)))
    for entry in _read_legacy_json(os.path.join(d, "kol_history.json"), []):
        for snap in entry.get("snapshots", []):
            s.add(KolSnapshot(
                tenant_id=tenant_id,
                kol_id=entry.get("kol_id", 0),
                recorded_at=snap.get("recorded_at", ""),
                metrics={k: v for k, v in snap.items() if k != "recorded_at"},
            ))
    for doc in _read_legacy_json(os.path.join(d, "campaigns.json"), []):
        s.add(Campaign(
            tenant_id=tenant_id,
            campaign_id=doc.get("id", 0),
            created_at=doc.get("created_at", ""),
            status=doc.get("status", "active"),
            doc=doc,
        ))
    cal = _read_legacy_json(os.path.join(d, "calibration.json"), None)
    if cal:
        s.add(Calibration(tenant_id=tenant_id, doc=cal))
    return _read_legacy_json(os.path.join(d, "seed_kols.json"), [])


def _ensure_tenant(s, tenant_id: str) -> Tenant:
    """Fetch the tenant row, creating it (and importing any legacy JSON data)
    on first touch."""
    t = s.get(Tenant, tenant_id)
    if t is not None:
        return t
    seed = []
    if os.path.isdir(tenancy.tenant_dir(tenant_id)):
        seed = _import_legacy_tenant(s, tenant_id)
    t = Tenant(id=tenant_id, seed=seed)
    s.add(t)
    return t


# Tenants known to have a row (and any legacy import done) — lets the hot path
# skip the check after first touch. The create runs in its own transaction so
# read queries stay read-only; a concurrent create in another process is
# tolerated via the primary-key conflict.
_migrated: set = set()
_migrate_lock = threading.Lock()


def _ensure_migrated(tenant_id: str):
    if tenant_id in _migrated:
        return
    with _migrate_lock:
        if tenant_id in _migrated:
            return
        try:
            with _session() as s:
                _ensure_tenant(s, tenant_id)
        except IntegrityError:
            pass  # created concurrently by another process
        _migrated.add(tenant_id)


def tenant_exists(tenant_id: str) -> bool:
    """Pure check — must NOT create the tenant, so auth's heal-on-login can
    still tell a never-provisioned tenant apart and seed it."""
    if tenant_id in _migrated:
        return True
    with SessionLocal() as s:
        if s.get(Tenant, tenant_id) is not None:
            return True
    return os.path.isdir(tenancy.tenant_dir(tenant_id))


def provision_tenant(tenant_id: str, seed_records: List[dict], created_at: str = ""):
    """Create a tenant seeded from the template library. Idempotent: an
    existing tenant (including one imported from legacy JSON) is left as-is."""
    with _write_lock, _session() as s:
        existed = s.get(Tenant, tenant_id) is not None
        t = _ensure_tenant(s, tenant_id)
        if not existed:
            if created_at:
                t.created_at = created_at
            has_kols = s.scalar(select(func.count()).select_from(Kol).where(Kol.tenant_id == tenant_id))
            if not has_kols and not t.seed:
                t.seed = seed_records
                for rec in seed_records:
                    s.add(Kol(tenant_id=tenant_id, kol_id=rec.get("id", 0),
                              **_dict_to_row_fields(rec)))
    _migrated.add(tenant_id)


def tenant_seed(tenant_id: str) -> List[dict]:
    _ensure_migrated(tenant_id)
    with SessionLocal() as s:
        t = s.get(Tenant, tenant_id)
        return list(t.seed or []) if t else []


# ════════════════════════════════════════════════════════════════
#  Users
# ════════════════════════════════════════════════════════════════
_users_imported = False


def _ensure_users_imported(s):
    """One-time import of the legacy users.json store."""
    global _users_imported
    if _users_imported:
        return
    if s.scalar(select(func.count()).select_from(User)) == 0:
        for u in _read_legacy_json(tenancy.USERS_PATH, []):
            s.add(User(
                email=u["email"],
                password_hash=u["password_hash"],
                tenant_id=u["tenant_id"],
                created_at=u.get("created_at", ""),
            ))
    _users_imported = True


def _user_to_dict(u: User) -> dict:
    return {"id": u.id, "email": u.email, "password_hash": u.password_hash,
            "tenant_id": u.tenant_id, "created_at": u.created_at,
            "email_verified": bool(u.email_verified), "lang": u.lang or "en",
            "role": u.role or "owner", "disabled": bool(u.disabled)}


def user_find(email: str) -> Optional[dict]:
    with _session() as s:
        _ensure_users_imported(s)
        u = s.scalar(select(User).where(User.email == email.strip().lower()))
        return _user_to_dict(u) if u else None


def user_find_by_tenant(tenant_id: str) -> Optional[dict]:
    """The workspace OWNER — the account that billing mail and orders belong to."""
    with _session() as s:
        _ensure_users_imported(s)
        # "owner" sorts after "member", so descending puts the owner first.
        u = s.scalars(select(User).where(User.tenant_id == tenant_id)
                      .order_by(User.role.desc(), User.id)).first()
        return _user_to_dict(u) if u else None


def users_for_tenant(tenant_id: str) -> List[dict]:
    """Everyone in a workspace, owner first."""
    with _session() as s:
        rows = s.scalars(select(User).where(User.tenant_id == tenant_id)
                         .order_by(User.role.desc(), User.id)).all()
        return [_user_to_dict(u) for u in rows]


def user_create(email: str, password_hash: str, tenant_id: str, created_at: str,
                lang: str = "en", role: str = "owner", email_verified: bool = False) -> dict:
    with _write_lock, _session() as s:
        _ensure_users_imported(s)
        u = User(email=email, password_hash=password_hash,
                 tenant_id=tenant_id, created_at=created_at,
                 email_verified=email_verified, lang=lang, role=role)
        s.add(u)
        s.flush()
        return _user_to_dict(u)


def users_all() -> List[dict]:
    """Every account, newest first (operator console)."""
    with _session() as s:
        _ensure_users_imported(s)
        rows = s.query(User).order_by(User.id.desc()).all()
        return [_user_to_dict(u) for u in rows]


def user_delete(email: str) -> Optional[str]:
    """Delete a user account; returns their tenant_id (None if not found)."""
    with _write_lock, _session() as s:
        _ensure_users_imported(s)
        u = s.scalar(select(User).where(User.email == email.strip().lower()))
        if not u:
            return None
        tenant_id = u.tenant_id
        s.delete(u)
        return tenant_id


def tenant_wipe(tenant_id: str) -> None:
    """Remove every row belonging to a tenant — its data, and every account
    in the workspace (owner and invited members alike)."""
    try:                                   # clips kept in object storage go too
        from backend import aivideo
        aivideo.delete_bucket_files(tenant_id)
    except Exception:
        pass
    try:                                   # and its (hashed) pool capture records
        from backend import pool
        pool.forget_tenant(tenant_id)
    except Exception:
        pass
    with _write_lock, _session() as s:
        for model in (Kol, KolSnapshot, Campaign, TenantPlan, UsageCounter, PaymentOrder,
                      TeamInvite, TenantDoc, User):
            s.query(model).filter(model.tenant_id == tenant_id).delete(synchronize_session=False)
        s.query(Calibration).filter(Calibration.tenant_id == tenant_id).delete(synchronize_session=False)
        s.query(Tenant).filter(Tenant.id == tenant_id).delete(synchronize_session=False)
    _migrated.discard(tenant_id)
    # generated video files live on disk, outside the database
    import shutil
    from backend.db import VIDEO_DIR
    shutil.rmtree(os.path.join(VIDEO_DIR, tenant_id), ignore_errors=True)


def user_update(email: str, **fields) -> bool:
    """Set simple account attributes (email_verified, lang)."""
    with _write_lock, _session() as s:
        u = s.scalar(select(User).where(User.email == email.strip().lower()))
        if not u:
            return False
        for k, v in fields.items():
            if k in ("email_verified", "lang", "disabled"):
                setattr(u, k, v)
        return True


def user_set_password(email: str, password_hash: str) -> bool:
    with _write_lock, _session() as s:
        u = s.scalar(select(User).where(User.email == email.strip().lower()))
        if not u:
            return False
        u.password_hash = password_hash
        return True


# ── Team invitations ──────────────────────────────────────────────
def _invite_to_dict(i: TeamInvite) -> dict:
    return {"id": i.id, "tenant_id": i.tenant_id, "email": i.email,
            "invited_by": i.invited_by, "created_at": i.created_at, "status": i.status}


def invite_create(tenant_id: str, email: str, invited_by: str, created_at: str) -> dict:
    with _write_lock, _session() as s:
        i = TeamInvite(tenant_id=tenant_id, email=email, invited_by=invited_by,
                       created_at=created_at, status="pending")
        s.add(i)
        s.flush()
        return _invite_to_dict(i)


def invite_get(invite_id: int) -> Optional[dict]:
    with _session() as s:
        i = s.get(TeamInvite, invite_id)
        return _invite_to_dict(i) if i else None


def invites_pending(tenant_id: str) -> List[dict]:
    with _session() as s:
        rows = s.scalars(select(TeamInvite).where(TeamInvite.tenant_id == tenant_id,
                                                  TeamInvite.status == "pending")
                         .order_by(TeamInvite.id)).all()
        return [_invite_to_dict(i) for i in rows]


def invite_close(invite_id: int, status: str) -> bool:
    """pending → accepted | revoked. False when it was no longer pending, so
    an invitation can be used (or withdrawn) exactly once."""
    with _write_lock, _session() as s:
        i = s.get(TeamInvite, invite_id)
        if not i or i.status != "pending":
            return False
        i.status = status
        return True


def users_demo_before(suffix: str, cutoff_iso: str, limit: int = 200) -> List[dict]:
    """Throwaway demo accounts created before `cutoff_iso` (expiry sweep)."""
    with _session() as s:
        rows = s.scalars(
            select(User)
            .where(User.email.like("%" + suffix), User.created_at < cutoff_iso)
            .limit(limit)
        ).all()
        return [_user_to_dict(u) for u in rows]


# ════════════════════════════════════════════════════════════════
#  Early-access interest registrations (operator lead list)
# ════════════════════════════════════════════════════════════════
def interest_add(email: str, integration_type: str, registered_at: str):
    with _write_lock, _session() as s:
        s.add(ApiInterest(email=email, integration_type=integration_type,
                          registered_at=registered_at))


def interests_all(limit: int = 1000) -> List[dict]:
    """Newest first."""
    with SessionLocal() as s:
        rows = s.scalars(select(ApiInterest).order_by(ApiInterest.id.desc()).limit(limit)).all()
        return [{"email": r.email, "integration_type": r.integration_type,
                 "registered_at": r.registered_at} for r in rows]


# ════════════════════════════════════════════════════════════════
#  Creators (KOLs)
# ════════════════════════════════════════════════════════════════
def kols_all(tenant_id: str, countries: Optional[List[str]] = None,
             category=None,
             content_type: Optional[str] = None) -> List[dict]:
    _ensure_migrated(tenant_id)
    with _session() as s:
        q = select(Kol).where(Kol.tenant_id == tenant_id)
        if countries:
            q = q.where(Kol.country.in_(countries))
        if category:                                  # one category, or several (see categories.pool)
            wanted = [category] if isinstance(category, str) else list(category)
            q = q.where(Kol.category.in_(wanted))
        rows = s.scalars(q.order_by(Kol.kol_id)).all()
        out = [_row_to_dict(r) for r in rows]
        # content_type lives in Kol.extra (no schema migration): filter in Python.
        # A creator marked "both" (or unmarked) can run live or video campaigns.
        if content_type in ("live", "video"):
            out = [d for d in out if (d.get("content_type") or "both") in (content_type, "both")]
        return out


def kols_get(tenant_id: str, kol_id: int) -> Optional[dict]:
    _ensure_migrated(tenant_id)
    with _session() as s:
        row = s.scalar(select(Kol).where(Kol.tenant_id == tenant_id, Kol.kol_id == kol_id))
        return _row_to_dict(row) if row else None


def _next_kol_id(s, tenant_id: str) -> int:
    return (s.scalar(select(func.max(Kol.kol_id)).where(Kol.tenant_id == tenant_id)) or 0) + 1


def kols_add(tenant_id: str, record: dict) -> dict:
    """Insert one creator, assigning the next per-tenant id. Retries once on a
    concurrent id collision (unique constraint on tenant_id+kol_id)."""
    _ensure_migrated(tenant_id)
    for attempt in (0, 1):
        try:
            with _write_lock, _session() as s:
                row = Kol(tenant_id=tenant_id, kol_id=_next_kol_id(s, tenant_id),
                          **_dict_to_row_fields(record))
                s.add(row)
                s.flush()
                return _row_to_dict(row)
        except IntegrityError:
            if attempt:
                raise


def kols_add_many(tenant_id: str, records: List[dict]) -> int:
    """Bulk insert with sequential per-tenant ids, one transaction."""
    _ensure_migrated(tenant_id)
    for attempt in (0, 1):
        try:
            with _write_lock, _session() as s:
                nid = _next_kol_id(s, tenant_id)
                for i, rec in enumerate(records):
                    s.add(Kol(tenant_id=tenant_id, kol_id=nid + i,
                              **_dict_to_row_fields(rec)))
                return len(records)
        except IntegrityError:
            if attempt:
                raise


def kols_update(tenant_id: str, kol_id: int, changes: dict) -> Optional[dict]:
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        row = s.scalar(select(Kol).where(Kol.tenant_id == tenant_id, Kol.kol_id == kol_id))
        if row is None:
            return None
        extra = dict(row.extra or {})
        for k, v in changes.items():
            if k in _KOL_COLUMNS:
                setattr(row, k, v)
            elif k != "id":
                extra[k] = v
        if extra != (row.extra or {}):
            row.extra = extra
        return _row_to_dict(row)


def kols_update_many(tenant_id: str, changes_by_id: dict) -> int:
    """Apply {kol_id: changes} in one transaction (price backfill)."""
    updated = 0
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        for kid, changes in changes_by_id.items():
            row = s.scalar(select(Kol).where(Kol.tenant_id == tenant_id, Kol.kol_id == kid))
            if row is None:
                continue
            for k, v in changes.items():
                if k in _KOL_COLUMNS:
                    setattr(row, k, v)
            updated += 1
    return updated


def kols_delete(tenant_id: str, kol_id: int) -> Optional[int]:
    """Delete one creator; returns the remaining count, or None if not found."""
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        res = s.execute(delete(Kol).where(Kol.tenant_id == tenant_id, Kol.kol_id == kol_id))
        if res.rowcount == 0:
            return None
        s.execute(delete(KolSnapshot).where(KolSnapshot.tenant_id == tenant_id,
                                            KolSnapshot.kol_id == kol_id))
        return s.scalar(select(func.count()).select_from(Kol).where(Kol.tenant_id == tenant_id))


def kols_replace(tenant_id: str, records: List[dict]) -> int:
    """Replace the whole library (seed reset), keeping the records' own ids."""
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        s.execute(delete(Kol).where(Kol.tenant_id == tenant_id))
        for rec in records:
            s.add(Kol(tenant_id=tenant_id, kol_id=rec.get("id", 0),
                      **_dict_to_row_fields(rec)))
        return len(records)


def kols_count(tenant_id: str) -> int:
    _ensure_migrated(tenant_id)
    with _session() as s:
        return s.scalar(select(func.count()).select_from(Kol).where(Kol.tenant_id == tenant_id))


def kols_exists(tenant_id: str, kol_id: int) -> bool:
    _ensure_migrated(tenant_id)
    with _session() as s:
        return s.scalar(select(func.count()).select_from(Kol).where(
            Kol.tenant_id == tenant_id, Kol.kol_id == kol_id)) > 0


# ════════════════════════════════════════════════════════════════
#  Metric history
# ════════════════════════════════════════════════════════════════
def history_for(tenant_id: str, kol_id: int) -> List[dict]:
    """Snapshots for one creator, oldest first (matches JSON-era order)."""
    _ensure_migrated(tenant_id)
    with _session() as s:
        rows = s.scalars(
            select(KolSnapshot)
            .where(KolSnapshot.tenant_id == tenant_id, KolSnapshot.kol_id == kol_id)
            .order_by(KolSnapshot.id)
        ).all()
        return [{"recorded_at": r.recorded_at, **(r.metrics or {})} for r in rows]


def history_append(tenant_id: str, kol_id: int, recorded_at: str, metrics: dict):
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        s.add(KolSnapshot(tenant_id=tenant_id, kol_id=kol_id,
                          recorded_at=recorded_at, metrics=metrics))


def history_clear(tenant_id: str):
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        s.execute(delete(KolSnapshot).where(KolSnapshot.tenant_id == tenant_id))


# ════════════════════════════════════════════════════════════════
#  Campaigns
# ════════════════════════════════════════════════════════════════
def campaigns_all(tenant_id: str) -> List[dict]:
    _ensure_migrated(tenant_id)
    with _session() as s:
        rows = s.scalars(select(Campaign).where(Campaign.tenant_id == tenant_id)).all()
        return [dict(r.doc) for r in rows]


# ── Operator-wide settings (one row per key, outside any workspace) ──
SYSTEM_TENANT = "__system__"


def system_get(key: str) -> Optional[dict]:
    rows = docs_list(SYSTEM_TENANT, key, limit=1)
    return rows[0] if rows else None


def system_set(key: str, doc: Optional[dict], created_at: str = ""):
    """Replace the setting; None clears it."""
    with _write_lock, _session() as s:
        s.query(TenantDoc).filter(TenantDoc.tenant_id == SYSTEM_TENANT, TenantDoc.kind == key).delete(
            synchronize_session=False)
        if doc is not None:
            s.add(TenantDoc(tenant_id=SYSTEM_TENANT, kind=key, created_at=created_at, doc=doc))


# ── Workspace documents (templates, contracts, message log, activity) ──
def _doc_to_dict(r: TenantDoc) -> dict:
    return {**(r.doc or {}), "id": r.id, "created_at": r.created_at}


def docs_list(tenant_id: str, kind: str, limit: int = 500) -> List[dict]:
    """Newest first."""
    with _session() as s:
        rows = s.scalars(select(TenantDoc).where(TenantDoc.tenant_id == tenant_id, TenantDoc.kind == kind)
                         .order_by(TenantDoc.id.desc()).limit(limit)).all()
        return [_doc_to_dict(r) for r in rows]


def docs_count(tenant_id: str, kind: str) -> int:
    with _session() as s:
        return s.scalar(select(func.count()).select_from(TenantDoc)
                        .where(TenantDoc.tenant_id == tenant_id, TenantDoc.kind == kind)) or 0


def doc_add(tenant_id: str, kind: str, doc: dict, created_at: str) -> dict:
    with _write_lock, _session() as s:
        r = TenantDoc(tenant_id=tenant_id, kind=kind, created_at=created_at, doc=doc)
        s.add(r)
        s.flush()
        return _doc_to_dict(r)


def doc_get(tenant_id: str, kind: str, doc_id: int) -> Optional[dict]:
    with _session() as s:
        r = s.get(TenantDoc, doc_id)
        return _doc_to_dict(r) if r and r.tenant_id == tenant_id and r.kind == kind else None


def doc_put(tenant_id: str, kind: str, doc_id: int, doc: dict) -> Optional[dict]:
    with _write_lock, _session() as s:
        r = s.get(TenantDoc, doc_id)
        if not r or r.tenant_id != tenant_id or r.kind != kind:
            return None
        r.doc = {k: v for k, v in doc.items() if k not in ("id", "created_at")}
        s.flush()
        return _doc_to_dict(r)


def doc_delete(tenant_id: str, kind: str, doc_id: int) -> bool:
    with _write_lock, _session() as s:
        r = s.get(TenantDoc, doc_id)
        if not r or r.tenant_id != tenant_id or r.kind != kind:
            return False
        s.delete(r)
        return True


def campaigns_get(tenant_id: str, campaign_id: int) -> Optional[dict]:
    _ensure_migrated(tenant_id)
    with _session() as s:
        row = s.scalar(select(Campaign).where(Campaign.tenant_id == tenant_id,
                                              Campaign.campaign_id == campaign_id))
        return dict(row.doc) if row else None


def campaigns_add(tenant_id: str, doc: dict) -> dict:
    """Insert a campaign, assigning the next per-tenant id into doc['id']."""
    _ensure_migrated(tenant_id)
    for attempt in (0, 1):
        try:
            with _write_lock, _session() as s:
                cid = (s.scalar(select(func.max(Campaign.campaign_id)).where(
                    Campaign.tenant_id == tenant_id)) or 0) + 1
                doc = {**doc, "id": cid}
                s.add(Campaign(tenant_id=tenant_id, campaign_id=cid,
                               created_at=doc.get("created_at", ""),
                               status=doc.get("status", "active"), doc=doc))
                return doc
        except IntegrityError:
            if attempt:
                raise


def campaigns_put(tenant_id: str, campaign_id: int, doc: dict) -> bool:
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        row = s.scalar(select(Campaign).where(Campaign.tenant_id == tenant_id,
                                              Campaign.campaign_id == campaign_id))
        if row is None:
            return False
        row.doc = doc
        row.status = doc.get("status", row.status)
        return True


def campaigns_delete(tenant_id: str, campaign_id: int) -> bool:
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        res = s.execute(delete(Campaign).where(Campaign.tenant_id == tenant_id,
                                               Campaign.campaign_id == campaign_id))
        return res.rowcount > 0


# ════════════════════════════════════════════════════════════════
#  Subscription plan & metered usage
# ════════════════════════════════════════════════════════════════
def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _plan_active(row) -> bool:
    """A paid period that has run out counts as Free. Timestamps are all
    written by _now_iso-style UTC isoformat, so string order is time order."""
    return not row.expires_at or row.expires_at > _now_iso()


def plan_get(tenant_id: str) -> str:
    """Tenant's subscription tier; no row — or a lapsed paid period — means
    the free tier."""
    with SessionLocal() as s:
        row = s.get(TenantPlan, tenant_id)
        return row.plan if row and _plan_active(row) else "free"


def plan_info(tenant_id: str) -> dict:
    """{plan, expires_at} with the same lapse rule as plan_get."""
    with SessionLocal() as s:
        row = s.get(TenantPlan, tenant_id)
        if not row or not _plan_active(row):
            return {"plan": "free", "expires_at": None}
        return {"plan": row.plan, "expires_at": row.expires_at}


def plan_set(tenant_id: str, plan: str, updated_at: str = "", expires_at: Optional[str] = None):
    """Set the plan. expires_at=None means it never lapses (operator grant)."""
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        row = s.get(TenantPlan, tenant_id)
        if row is None:
            s.add(TenantPlan(tenant_id=tenant_id, plan=plan, updated_at=updated_at,
                             expires_at=expires_at))
        else:
            row.plan = plan
            row.updated_at = updated_at
            row.expires_at = expires_at


def plans_with_expiry() -> List[dict]:
    """Every paid term that has an end date (renewal-reminder scan)."""
    with SessionLocal() as s:
        rows = s.scalars(select(TenantPlan).where(TenantPlan.expires_at.is_not(None),
                                                  TenantPlan.plan != "free")).all()
        return [{"tenant_id": r.tenant_id, "plan": r.plan, "expires_at": r.expires_at,
                 "reminded": r.reminded} for r in rows]


def plan_claim_reminder(tenant_id: str, key: str) -> bool:
    """Record that notice `key` is being sent. False if it already was — the
    guard that keeps several workers from mailing the same reminder."""
    with _write_lock, _session() as s:
        row = s.get(TenantPlan, tenant_id)
        if not row or row.reminded == key:
            return False
        row.reminded = key
        return True


# ════════════════════════════════════════════════════════════════
#  QR-payment orders
# ════════════════════════════════════════════════════════════════
_ORDER_FIELDS = ("order_no", "tenant_id", "email", "plan", "months", "amount", "currency",
                 "status", "reference", "note", "created_at", "submitted_at", "decided_at")
ORDER_OPEN = ("pending", "submitted")


def _order_to_dict(o: PaymentOrder) -> dict:
    return {f: getattr(o, f) for f in _ORDER_FIELDS}


def order_create(**fields) -> dict:
    with _write_lock, _session() as s:
        o = PaymentOrder(**fields)
        s.add(o)
        s.flush()
        return _order_to_dict(o)


def order_get(order_no: str) -> Optional[dict]:
    with SessionLocal() as s:
        o = s.scalar(select(PaymentOrder).where(PaymentOrder.order_no == order_no))
        return _order_to_dict(o) if o else None


def order_update(order_no: str, expect_status: Optional[tuple] = None, **changes) -> Optional[dict]:
    """Apply changes; with expect_status, only if the order is still in one
    of those states (so two operators can't both confirm it). Returns the
    updated order, or None when it is missing / in another state."""
    with _write_lock, _session() as s:
        o = s.scalar(select(PaymentOrder).where(PaymentOrder.order_no == order_no))
        if o is None or (expect_status and o.status not in expect_status):
            return None
        for k, v in changes.items():
            if k in _ORDER_FIELDS:
                setattr(o, k, v)
        s.flush()
        return _order_to_dict(o)


def orders_for_tenant(tenant_id: str, limit: int = 20) -> List[dict]:
    with SessionLocal() as s:
        rows = s.scalars(select(PaymentOrder).where(PaymentOrder.tenant_id == tenant_id)
                         .order_by(PaymentOrder.id.desc()).limit(limit)).all()
        return [_order_to_dict(o) for o in rows]


def orders_all(statuses: Optional[tuple] = None, limit: int = 200) -> List[dict]:
    with SessionLocal() as s:
        q = select(PaymentOrder)
        if statuses:
            q = q.where(PaymentOrder.status.in_(statuses))
        rows = s.scalars(q.order_by(PaymentOrder.id.desc()).limit(limit)).all()
        return [_order_to_dict(o) for o in rows]


def order_open_amounts(currency: str) -> set:
    """Amounts of every order still awaiting payment/confirmation — a new
    order must not reuse one, so each can be matched in the wallet history."""
    with SessionLocal() as s:
        rows = s.scalars(select(PaymentOrder.amount).where(
            PaymentOrder.status.in_(ORDER_OPEN), PaymentOrder.currency == currency)).all()
        return {round(a, 2) for a in rows}


def order_reference_used(reference: str, exclude_order_no: str) -> bool:
    """True when another live or paid order already claims this transaction."""
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(PaymentOrder).where(
            func.lower(PaymentOrder.reference) == reference.lower(),
            PaymentOrder.order_no != exclude_order_no,
            PaymentOrder.status.in_(("submitted", "paid")))) > 0


def orders_paid_totals(since_iso: Optional[str] = None) -> dict:
    """{currency: {"amount": total, "count": n}} of confirmed payments,
    optionally only those confirmed at/after since_iso."""
    with SessionLocal() as s:
        q = select(PaymentOrder.currency, func.sum(PaymentOrder.amount), func.count()).where(
            PaymentOrder.status == "paid")
        if since_iso:
            q = q.where(PaymentOrder.decided_at >= since_iso)
        return {cur: {"amount": round(total or 0, 2), "count": n}
                for cur, total, n in s.execute(q.group_by(PaymentOrder.currency))}


def orders_count(status: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(func.count()).select_from(PaymentOrder).where(
            PaymentOrder.status == status))


# ════════════════════════════════════════════════════════════════
#  Operator audit trail
# ════════════════════════════════════════════════════════════════
def audit_add(action: str, target: str = "", detail: str = "", ip: str = ""):
    with _write_lock, _session() as s:
        s.add(AdminAudit(at=_now_iso(), action=action[:32], target=target[:255],
                         detail=detail[:500], ip=ip[:64]))


def audit_list(limit: int = 100) -> List[dict]:
    """Newest first."""
    with SessionLocal() as s:
        rows = s.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(limit)).all()
        return [{"at": r.at, "action": r.action, "target": r.target,
                 "detail": r.detail, "ip": r.ip} for r in rows]


def orders_expire_pending(cutoff_iso: str) -> int:
    """Mark never-submitted orders created before the cutoff as expired."""
    with _write_lock, _session() as s:
        rows = s.scalars(select(PaymentOrder).where(
            PaymentOrder.status == "pending", PaymentOrder.created_at < cutoff_iso)).all()
        for o in rows:
            o.status = "expired"
        return len(rows)


def optimize_runs(tenant_id: str, month: str) -> int:
    with SessionLocal() as s:
        row = s.get(UsageCounter, (tenant_id, month))
        return row.optimize_runs if row else 0


def optimize_consume(tenant_id: str, month: str) -> int:
    """Count one optimization run against this month's quota; returns the new
    total. Retries once if two first-runs of the month race on the insert."""
    _ensure_migrated(tenant_id)
    for attempt in (0, 1):
        try:
            with _write_lock, _session() as s:
                row = s.get(UsageCounter, (tenant_id, month))
                if row is None:
                    s.add(UsageCounter(tenant_id=tenant_id, month=month, optimize_runs=1))
                    return 1
                row.optimize_runs += 1
                return row.optimize_runs
        except IntegrityError:
            if attempt:
                raise


def agent_messages(tenant_id: str, month: str) -> int:
    with SessionLocal() as s:
        row = s.get(UsageCounter, (tenant_id, month))
        return row.agent_messages if row else 0


def agent_refund(tenant_id: str, month: str):
    """Give back one AI Copilot chat turn (the LLM call failed, so the
    merchant got nothing for it)."""
    with _write_lock, _session() as s:
        row = s.get(UsageCounter, (tenant_id, month))
        if row is not None and row.agent_messages > 0:
            row.agent_messages -= 1


def agent_consume(tenant_id: str, month: str) -> int:
    """Count one AI Copilot chat turn against this month's quota; returns the new
    total. Mirrors optimize_consume — shares the per-month usage row."""
    _ensure_migrated(tenant_id)
    for attempt in (0, 1):
        try:
            with _write_lock, _session() as s:
                row = s.get(UsageCounter, (tenant_id, month))
                if row is None:
                    s.add(UsageCounter(tenant_id=tenant_id, month=month, agent_messages=1))
                    return 1
                row.agent_messages += 1
                return row.agent_messages
        except IntegrityError:
            if attempt:
                raise


# ════════════════════════════════════════════════════════════════
#  Operator console aggregates
# ════════════════════════════════════════════════════════════════
def admin_stats(month: str) -> dict:
    """Per-tenant stats for the operator console in one session (four grouped
    queries) instead of five queries per user. Returns
    {tenant_id: {plan, optimize_runs, agent_messages, creators, campaigns}};
    tenants absent from a table simply keep the zero/free defaults. Reads only
    what is in the database — it does not trigger legacy JSON migration."""
    stats: dict = {}

    def entry(tid: str) -> dict:
        return stats.setdefault(tid, {"plan": "free", "optimize_runs": 0,
                                      "agent_messages": 0, "creators": 0,
                                      "campaigns": 0})

    with SessionLocal() as s:
        now = _now_iso()
        for tid, plan, exp in s.execute(
                select(TenantPlan.tenant_id, TenantPlan.plan, TenantPlan.expires_at)):
            lapsed = bool(exp) and exp <= now
            entry(tid)["plan"] = "free" if lapsed else plan
            entry(tid)["plan_expires_at"] = None if lapsed else exp
        for tid, runs, chats in s.execute(
                select(UsageCounter.tenant_id, UsageCounter.optimize_runs,
                       UsageCounter.agent_messages)
                .where(UsageCounter.month == month)):
            entry(tid)["optimize_runs"] = runs
            entry(tid)["agent_messages"] = chats
        for tid, n in s.execute(
                select(Kol.tenant_id, func.count()).group_by(Kol.tenant_id)):
            entry(tid)["creators"] = n
        for tid, n in s.execute(
                select(Campaign.tenant_id, func.count()).group_by(Campaign.tenant_id)):
            entry(tid)["campaigns"] = n
    return stats


def admin_engagement(since_iso: str) -> dict:
    """Per-tenant engagement for the operator console, in one session:
    {tenant_id: {ever_optimized, last_active, active_recent, messages,
    contracts, contacted}}. `contacted` = any creator moved past "shortlisted"."""
    out: dict = {}

    def entry(tid: str) -> dict:
        return out.setdefault(tid, {"ever_optimized": False, "last_active": None, "active_recent": False,
                                    "messages": 0, "contracts": 0, "contacted": False})

    with SessionLocal() as s:
        for tid, runs in s.execute(select(UsageCounter.tenant_id, func.sum(UsageCounter.optimize_runs))
                                   .group_by(UsageCounter.tenant_id)):
            entry(tid)["ever_optimized"] = (runs or 0) > 0
        for tid, kind, n, latest in s.execute(
                select(TenantDoc.tenant_id, TenantDoc.kind, func.count(), func.max(TenantDoc.created_at))
                .where(TenantDoc.kind.in_(("activity", "message", "contract")))
                .group_by(TenantDoc.tenant_id, TenantDoc.kind)):
            e = entry(tid)
            if kind == "activity":
                e["last_active"] = latest
                e["active_recent"] = bool(latest) and latest >= since_iso
            elif kind == "message":
                e["messages"] = n
            else:
                e["contracts"] = n
        for tid, doc in s.execute(select(Campaign.tenant_id, Campaign.doc)):
            if any((p.get("status") or "shortlisted") != "shortlisted"
                   for p in ((doc or {}).get("pipeline") or {}).values()):
                entry(tid)["contacted"] = True
    return out


# ════════════════════════════════════════════════════════════════
#  Calibration
# ════════════════════════════════════════════════════════════════
def calibration_get(tenant_id: str) -> Optional[dict]:
    _ensure_migrated(tenant_id)
    with _session() as s:
        row = s.get(Calibration, tenant_id)
        return dict(row.doc) if row else None


def calibration_put(tenant_id: str, doc: dict):
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        row = s.get(Calibration, tenant_id)
        if row is None:
            s.add(Calibration(tenant_id=tenant_id, doc=doc))
        else:
            row.doc = doc


# ════════════════════════════════════════════════════════════════
#  Data hygiene
# ════════════════════════════════════════════════════════════════
def purge_synthetic_creators() -> int:
    """Remove generated placeholder creators (source='synthetic') from every
    library and from each tenant's reset baseline, so merchants only ever see
    real creators. Idempotent; returns how many library rows were removed."""
    with _write_lock, _session() as s:
        removed = s.execute(delete(Kol).where(Kol.source == "synthetic")).rowcount or 0
        for t in s.scalars(select(Tenant)):
            seed = t.seed or []
            kept = [r for r in seed if (r or {}).get("source") != "synthetic"]
            if len(kept) != len(seed):
                t.seed = kept
    return removed
