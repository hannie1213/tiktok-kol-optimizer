"""
Database layer — SQLAlchemy engine, session factory, and ORM models.

Persistence for the multi-tenant SaaS backend. Every business table carries a
`tenant_id` column (indexed) so any number of merchants share one database with
row-level isolation; the repository layer (backend/repo.py) scopes every query
by the request's tenant.

Configuration (env vars)
------------------------
  DATABASE_URL      Full SQLAlchemy URL. Unset → SQLite file at
                    {KOL_DATA_DIR|data}/kol_saas.db (zero-config dev default).
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


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return "sqlite:///" + os.path.join(tenancy.DATA_DIR, "kol_saas.db").replace("\\", "/")


def _make_engine():
    url = _database_url()
    if url.startswith("sqlite"):
        os.makedirs(tenancy.DATA_DIR, exist_ok=True)
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


class UsageCounter(Base):
    """Per-tenant, per-calendar-month metered usage (quota enforcement)."""
    __tablename__ = "usage_counters"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)   # "YYYY-MM"
    optimize_runs: Mapped[int] = mapped_column(Integer, default=0)


def init_db():
    Base.metadata.create_all(engine)


init_db()
