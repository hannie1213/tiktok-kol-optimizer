import contextvars
import json
import os

# Root data directory (overridable for tests).
DATA_DIR = os.environ.get("KOL_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
TENANTS_DIR = os.path.join(DATA_DIR, "tenants")
USERS_PATH = os.path.join(DATA_DIR, "users.json")

# The library every new tenant is seeded from (the curated FastMoss seed + fill).
TEMPLATE_SEED = os.path.join(DATA_DIR, "seed_kols.json")

# Unauthenticated fallback tenant (tests set this so the suite needs no tokens).
DEFAULT_TENANT = os.environ.get("DEFAULT_TENANT")

# Request-scoped current tenant id.
_current_tenant: contextvars.ContextVar = contextvars.ContextVar("current_tenant", default=None)


def set_current_tenant(tenant_id):
    _current_tenant.set(tenant_id)


def current_tenant() -> str:
    tid = _current_tenant.get() or DEFAULT_TENANT
    if not tid:
        # Should never happen on a route guarded by require_tenant.
        raise RuntimeError("No tenant in request context")
    return tid


# ── Per-tenant document names (stored in SQLite, see backend/db.py) ──
LIBRARY = "sample_kols"       # the merchant's working creator library
SEED = "seed_kols"            # reset baseline for the library
HISTORY = "kol_history"       # metric snapshots
CAMPAIGNS = "campaigns"
CALIBRATION = "calibration"


def tenant_dir(tenant_id: str = None) -> str:
    """Legacy on-disk folder (only read once, by the JSON importer)."""
    return os.path.join(TENANTS_DIR, tenant_id or current_tenant())


# ── Provisioning ──────────────────────────────────────────────────
def provision_tenant(tenant_id: str):
    """Seed a new tenant's documents from the template library."""
    from backend import db

    seed = []
    if os.path.exists(TEMPLATE_SEED):
        with open(TEMPLATE_SEED, encoding="utf-8") as f:
            seed = json.load(f)

    for name, content in ((LIBRARY, seed), (SEED, seed), (HISTORY, []), (CAMPAIGNS, [])):
        if not db.doc_exists(name, tenant_id=tenant_id):
            db.write_doc(name, content, tenant_id=tenant_id)


def tenant_exists(tenant_id: str) -> bool:
    from backend import db
    return db.tenant_exists(tenant_id)
