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
    Calibration,
    Campaign,
    Kol,
    KolSnapshot,
    SessionLocal,
    Tenant,
    TenantPlan,
    UsageCounter,
    User,
    engine,
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
            "tenant_id": u.tenant_id, "created_at": u.created_at}


def user_find(email: str) -> Optional[dict]:
    with _session() as s:
        _ensure_users_imported(s)
        u = s.scalar(select(User).where(User.email == email.strip().lower()))
        return _user_to_dict(u) if u else None


def user_find_by_tenant(tenant_id: str) -> Optional[dict]:
    with _session() as s:
        _ensure_users_imported(s)
        u = s.scalar(select(User).where(User.tenant_id == tenant_id))
        return _user_to_dict(u) if u else None


def user_create(email: str, password_hash: str, tenant_id: str, created_at: str) -> dict:
    with _write_lock, _session() as s:
        _ensure_users_imported(s)
        u = User(email=email, password_hash=password_hash,
                 tenant_id=tenant_id, created_at=created_at)
        s.add(u)
        s.flush()
        return _user_to_dict(u)


# ════════════════════════════════════════════════════════════════
#  Creators (KOLs)
# ════════════════════════════════════════════════════════════════
def kols_all(tenant_id: str, countries: Optional[List[str]] = None,
             category: Optional[str] = None) -> List[dict]:
    _ensure_migrated(tenant_id)
    with _session() as s:
        q = select(Kol).where(Kol.tenant_id == tenant_id)
        if countries:
            q = q.where(Kol.country.in_(countries))
        if category:
            q = q.where(Kol.category == category)
        rows = s.scalars(q.order_by(Kol.kol_id)).all()
        return [_row_to_dict(r) for r in rows]


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
def plan_get(tenant_id: str) -> str:
    """Tenant's subscription tier; no row means the free tier."""
    with SessionLocal() as s:
        row = s.get(TenantPlan, tenant_id)
        return row.plan if row else "free"


def plan_set(tenant_id: str, plan: str, updated_at: str = ""):
    _ensure_migrated(tenant_id)
    with _write_lock, _session() as s:
        row = s.get(TenantPlan, tenant_id)
        if row is None:
            s.add(TenantPlan(tenant_id=tenant_id, plan=plan, updated_at=updated_at))
        else:
            row.plan = plan
            row.updated_at = updated_at


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
