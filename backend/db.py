"""
Database layer — SQLAlchemy engine, session factory, and ORM models.

Persistence for the multi-tenant SaaS backend. Every business table carries a
`tenant_id` column (indexed) so any number of merchants share one database with
row-level isolation; the repository layer (backend/repo.py) scopes every query
by the request's tenant.

Configuration (env vars)
------------------------
  DATABASE_URL      Full SQLAlchemy URL. Unset → SQLite file at
                    {SQLITE_DIR|KOL_DATA_DIR|data}/kol_saas.db (zero-config
                    dev default). SQLITE_DIR lets a container keep the
                    database on a writable volume while the seed data stays
                    read-only in the image.
                    Production example:
                      postgresql+psycopg://user:pass@host:5432/kol_saas
  DB_POOL_SIZE      Connection-pool size per worker process (default 10).
  DB_MAX_OVERFLOW   Extra connections allowed under burst load (default 20).

Concurrency notes
-----------------
- Server-based databases (PostgreSQL) get a QueuePool with pre-ping and
  recycling, sized per uvicorn worker: total connections =
  workers × (pool_size + max_overflow).
- SQLite runs in WAL mode with a busy timeout, so concurrent readers never
  block and short writer waits are absorbed instead of erroring. Fine for
  a single-node deployment; point DATABASE_URL at PostgreSQL to scale out.
"""
import os

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from backend import tenancy


_SQLITE_DIR = os.environ.get("SQLITE_DIR") or tenancy.DATA_DIR
# Generated AI video clips (backend/aivideo.py), one folder per workspace.
VIDEO_DIR = os.environ.get("VIDEO_DIR") or os.path.join(_SQLITE_DIR, "videos")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return "sqlite:///" + os.path.join(_SQLITE_DIR, "kol_saas.db").replace("\\", "/")


def _make_engine():
    url = _database_url()
    if url.startswith("sqlite"):
        if not os.environ.get("DATABASE_URL"):
            os.makedirs(_SQLITE_DIR, exist_ok=True)
        eng = create_engine(
            url,
            # FastAPI sync endpoints run in a threadpool → connections hop threads.
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return eng

    return create_engine(
        url,
        pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "20")),
        pool_pre_ping=True,     # drop dead connections instead of erroring a request
        pool_recycle=1800,      # stay ahead of server/LB idle timeouts
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """One row per merchant workspace. `seed` snapshots the library the tenant
    was provisioned with, so /kols/reset can restore that exact baseline."""
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    seed: Mapped[list] = mapped_column(JSON, default=list)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    # Set once the owner clicks the link we emailed (or resets a password by email).
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # UI language last seen for this user — the language emails are written in.
    lang: Mapped[str] = mapped_column(String(8), default="en")
    # "owner" created the workspace (and pays for it); a "member" was invited
    # into it and shares its tenant_id.
    role: Mapped[str] = mapped_column(String(16), default="owner")
    # Set by the operator to lock an abusive account out without deleting its data.
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class TeamInvite(Base):
    """An owner's invitation for someone to join their workspace."""
    __tablename__ = "team_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(255))
    invited_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending | accepted | revoked


class Kol(Base):
    """A creator in one tenant's library. `kol_id` is the per-tenant public id
    the API exposes (unchanged from the JSON era); the surrogate `id` is
    internal only."""
    __tablename__ = "kols"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kol_id", name="uq_kols_tenant_kol"),
        # /optimize and /kols always filter on (tenant, country, category).
        Index("ix_kols_tenant_market", "tenant_id", "country", "category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kol_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(8))
    category: Mapped[str] = mapped_column(String(32))
    followers: Mapped[int] = mapped_column(Integer, default=0)
    engagement_rate: Mapped[float] = mapped_column(Float, default=0.0)
    fit_score: Mapped[float] = mapped_column(Float, default=0.75)
    commission_rate: Mapped[float] = mapped_column(Float, default=0.15)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    avg_views: Mapped[int] = mapped_column(Integer, nullable=True)
    avg_likes: Mapped[int] = mapped_column(Integer, nullable=True)
    gender_ratio: Mapped[float] = mapped_column(Float, nullable=True)
    age_group: Mapped[str] = mapped_column(String(16), nullable=True)
    tiktok_url: Mapped[str] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="fastmoss")
    cost_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    # Lossless round-trip for any fields the schema doesn't model explicitly
    # (e.g. extra columns carried by an imported seed file).
    extra: Mapped[dict] = mapped_column(JSON, nullable=True)


class KolSnapshot(Base):
    """Timestamped metric history for one creator (trend sparklines)."""
    __tablename__ = "kol_snapshots"
    __table_args__ = (Index("ix_snapshots_tenant_kol", "tenant_id", "kol_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    kol_id: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[str] = mapped_column(String(40))
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class Campaign(Base):
    """One saved optimization run + its eventual actual results. The full
    API-facing document (nested selected_kols, kol_actuals, …) lives in `doc`;
    the promoted columns exist for indexing and list ordering."""
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("tenant_id", "campaign_id", name="uq_campaigns_tenant_campaign"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    campaign_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    doc: Mapped[dict] = mapped_column(JSON, default=dict)


class Calibration(Base):
    """Per-tenant prediction-calibration table (global / segment / creator)."""
    __tablename__ = "calibrations"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc: Mapped[dict] = mapped_column(JSON, default=dict)


class TenantPlan(Base):
    """Which subscription tier a tenant is on. No row → free tier. The plan
    catalog itself (limits, prices) lives in backend/billing.py."""
    __tablename__ = "tenant_plans"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan: Mapped[str] = mapped_column(String(16), default="free")
    updated_at: Mapped[str] = mapped_column(String(40), default="")
    # UTC ISO timestamp the paid period ends; NULL = no expiry (operator
    # grant). Past it, the tenant is treated as Free (see repo.plan_get).
    expires_at: Mapped[str] = mapped_column(String(40), nullable=True)
    # Which renewal notice was last sent for this term ("<expires_at>|<stage>"),
    # so the scheduler never sends the same one twice.
    reminded: Mapped[str] = mapped_column(String(64), nullable=True)


class UsageCounter(Base):
    """Per-tenant, per-calendar-month metered usage (quota enforcement)."""
    __tablename__ = "usage_counters"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)   # "YYYY-MM"
    optimize_runs: Mapped[int] = mapped_column(Integer, default=0)
    # AI Copilot chat turns — the second metered axis (see backend/billing.py).
    agent_messages: Mapped[int] = mapped_column(Integer, default=0)


class PaymentOrder(Base):
    """One QR-payment order: a merchant asks for a plan, pays the exact
    amount by scanning the operator's QR code, submits the transaction
    reference, and the operator confirms it against their wallet history."""
    __tablename__ = "payment_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(255))
    plan: Mapped[str] = mapped_column(String(16))
    months: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="MYR")
    # pending → submitted → paid | rejected ; pending → expired | cancelled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reference: Mapped[str] = mapped_column(String(64), nullable=True)
    note: Mapped[str] = mapped_column(String(255), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default="")
    submitted_at: Mapped[str] = mapped_column(String(40), nullable=True)
    decided_at: Mapped[str] = mapped_column(String(40), nullable=True)


class AdminAudit(Base):
    """Append-only trail of operator actions (who got which plan, which
    payment was confirmed, which account was deleted) — the record to check
    when a merchant disputes something."""
    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(String(500), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class TenantDoc(Base):
    """Small per-workspace documents that don't warrant a table each: message
    and contract templates, generated contracts, the outreach message log and
    the workspace activity trail. `kind` says which; the payload is `doc`."""
    __tablename__ = "tenant_docs"
    __table_args__ = (Index("ix_tenant_docs_kind", "tenant_id", "kind", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[str] = mapped_column(String(40), default="")
    doc: Mapped[dict] = mapped_column(JSON, default=dict)


class PoolCreator(Base):
    """A creator in the shared Explore pool — one catalogue for every workspace,
    filled by the browser extension from public profile pages. Holds only what
    the profile page shows; never who captured it or any workspace's own data."""
    __tablename__ = "pool_creators"
    __table_args__ = (UniqueConstraint("platform", "handle_key", name="uq_pool_platform_handle"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(16), default="tiktok")
    handle: Mapped[str] = mapped_column(String(80))
    handle_key: Mapped[str] = mapped_column(String(80), index=True)      # lower-cased, for lookups
    name: Mapped[str] = mapped_column(String(120), default="")
    profile_url: Mapped[str] = mapped_column(String(255), default="")
    followers: Mapped[int] = mapped_column(Integer, default=0, index=True)
    likes: Mapped[int] = mapped_column(Integer, nullable=True)
    videos: Mapped[int] = mapped_column(Integer, nullable=True)
    bio: Mapped[str] = mapped_column(String(300), default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    country: Mapped[str] = mapped_column(String(4), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=True)
    first_seen: Mapped[str] = mapped_column(String(40), default="")
    last_seen: Mapped[str] = mapped_column(String(40), default="", index=True)
    captures: Mapped[int] = mapped_column(Integer, default=0)


class ApiInterest(Base):
    """Early-access interest registration for a coming-soon integration.
    Not tenant data — it is a lead list for the operator console."""
    __tablename__ = "api_interests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255))
    integration_type: Mapped[str] = mapped_column(String(32))
    registered_at: Mapped[str] = mapped_column(String(40), default="")


# Columns added after the first release. create_all() never ALTERs an existing
# table, so bring older SQLite/Postgres databases up to date in place. Each entry
# is (table, column, DDL type + default); the ADD COLUMN is skipped when the
# column already exists so this is safe to run on every boot.
_ADDED_COLUMNS = [
    ("usage_counters", "agent_messages", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "email_verified", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("users", "lang", "VARCHAR(8) NOT NULL DEFAULT 'en'"),
    ("tenant_plans", "expires_at", "VARCHAR(40)"),
    ("tenant_plans", "reminded", "VARCHAR(64)"),
    ("users", "role", "VARCHAR(16) NOT NULL DEFAULT 'owner'"),
    ("users", "disabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def _migrate_added_columns():
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            if table not in existing_tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db():
    Base.metadata.create_all(engine)
    _migrate_added_columns()


init_db()
