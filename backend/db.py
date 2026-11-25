"""
SQLite persistence layer.

Everything that used to live in loose JSON files (data/users.json and
data/tenants/<id>/*.json) now lives in one SQLite database, so concurrent
requests can't corrupt a half-written file and the whole app state is a single
file that is easy to back up and to mount on a persistent disk when deployed.

Tables
  users            accounts (email, password hash, tenant id, plan)
  tenant_docs      one JSON document per (tenant, name), e.g. the creator library
  password_resets  single-use, hashed, expiring reset tokens
  usage            per-tenant monthly counters for plan limits
  payment_requests manual QR payments waiting for / decided by an admin
  meta             key/value flags (e.g. whether legacy JSON was imported)

Location: env DATABASE_PATH, else <KOL_DATA_DIR or data/>/app.db.
On first start any legacy JSON files are imported automatically.
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from backend import tenancy

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE,
    password_hash  TEXT    NOT NULL,
    tenant_id      TEXT    NOT NULL UNIQUE,
    plan           TEXT    NOT NULL DEFAULT 'free',
    created_at     TEXT    NOT NULL,
    password_changed_at INTEGER NOT NULL DEFAULT 0   -- unix seconds; older tokens are revoked
);
CREATE TABLE IF NOT EXISTS tenant_docs (
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    body        TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, name)
);
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    expires_at  TEXT NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    tenant_id  TEXT    NOT NULL,
    period     TEXT    NOT NULL,          -- YYYY-MM
    metric     TEXT    NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, period, metric)
);
CREATE TABLE IF NOT EXISTS payment_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    plan            TEXT    NOT NULL,
    months          INTEGER NOT NULL,
    amount_myr      INTEGER NOT NULL,
    code            TEXT    NOT NULL,
    reference       TEXT    NOT NULL,
    note            TEXT,
    proof           BLOB,
    proof_type      TEXT,
    status          TEXT    NOT NULL,      -- pending | approved | rejected | cancelled
    reject_reason   TEXT,
    created_at      TEXT    NOT NULL,
    decided_at      TEXT,
    decided_by      TEXT,
    plan_expires_at TEXT
);
CREATE INDEX IF NOT EXISTS payment_requests_status ON payment_requests(status);
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

_init_lock = threading.Lock()
_initialized_for = None


def db_path() -> str:
    return os.environ.get("DATABASE_PATH") or os.path.join(tenancy.DATA_DIR, "app.db")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init():
    """Create tables and import legacy JSON once. Safe to call repeatedly."""
    global _initialized_for
    path = db_path()
    if _initialized_for == path:
        return
    with _init_lock:
        if _initialized_for == path:
            return
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            _migrate(conn)
            _import_legacy_json(conn)
        finally:
            conn.close()
        _initialized_for = path


@contextmanager
def connect():
    """A connection; wrap writes in `with tx(conn):` for atomicity."""
    init()
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    """IMMEDIATE transaction: takes the write lock up front (no upgrade deadlocks)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ── Tenant documents ──────────────────────────────────────────────
def read_doc(name: str, default=None, tenant_id: str = None):
    tid = tenant_id or tenancy.current_tenant()
    with connect() as conn:
        row = conn.execute(
            "SELECT body FROM tenant_docs WHERE tenant_id=? AND name=?", (tid, name)
        ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["body"])
    except json.JSONDecodeError:
        return default


def write_doc(name: str, data, tenant_id: str = None):
    tid = tenant_id or tenancy.current_tenant()
    body = json.dumps(data, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            "INSERT INTO tenant_docs (tenant_id, name, body, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(tenant_id, name) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
            (tid, name, body, now_iso()),
        )


def doc_exists(name: str, tenant_id: str = None) -> bool:
    tid = tenant_id or tenancy.current_tenant()
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM tenant_docs WHERE tenant_id=? AND name=?", (tid, name)
        ).fetchone() is not None


def tenant_exists(tenant_id: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM tenant_docs WHERE tenant_id=? LIMIT 1", (tenant_id,)
        ).fetchone() is not None


# ── Additive column migrations (safe on existing databases) ──────
_USER_COLUMNS = {
    "plan_expires_at": "TEXT",
}


def _migrate(conn: sqlite3.Connection):
    have = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for col, typ in _USER_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")


# ── Legacy import ─────────────────────────────────────────────────
def _import_legacy_json(conn: sqlite3.Connection):
    if conn.execute("SELECT 1 FROM meta WHERE key='legacy_json_imported'").fetchone():
        return
    with tx(conn):
        if os.path.exists(tenancy.USERS_PATH):
            with open(tenancy.USERS_PATH, encoding="utf-8") as f:
                for u in json.load(f):
                    conn.execute(
                        "INSERT OR IGNORE INTO users (email, password_hash, tenant_id, plan, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (u["email"], u["password_hash"], u["tenant_id"],
                         u.get("plan", "free"), u.get("created_at") or now_iso()),
                    )
        if os.path.isdir(tenancy.TENANTS_DIR):
            for tid in sorted(os.listdir(tenancy.TENANTS_DIR)):
                d = os.path.join(tenancy.TENANTS_DIR, tid)
                if not os.path.isdir(d):
                    continue
                for fn in sorted(os.listdir(d)):
                    if not fn.endswith(".json"):
                        continue
                    with open(os.path.join(d, fn), encoding="utf-8") as f:
                        body = f.read()
                    try:
                        json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO tenant_docs (tenant_id, name, body, updated_at) VALUES (?,?,?,?)",
                        (tid, fn[:-5], body, now_iso()),
                    )
        conn.execute("INSERT INTO meta (key, value) VALUES ('legacy_json_imported', ?)", (now_iso(),))
