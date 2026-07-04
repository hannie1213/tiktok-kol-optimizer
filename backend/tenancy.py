"""
Tenant context & provisioning.

The request-scoped current tenant is stored in a contextvar (set by
backend.auth.require_tenant); the repository layer (backend/repo.py) scopes
every database query by it. The legacy per-tenant JSON folders under
data/tenants/{id}/ are no longer written — they are only read once, on a
tenant's first database access, to migrate pre-database deployments in place.
"""
import contextvars
import json
import os

# Root data directory (overridable for tests). Holds the seed template, the
# default SQLite database file, and any legacy JSON tenant folders.
DATA_DIR = os.environ.get("KOL_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
TENANTS_DIR = os.path.join(DATA_DIR, "tenants")
USERS_PATH = os.path.join(DATA_DIR, "users.json")   # legacy store, import-only

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


def tenant_dir(tenant_id: str = None) -> str:
    """Legacy JSON folder for a tenant — read-only, used for one-time import."""
    return os.path.join(TENANTS_DIR, tenant_id or current_tenant())


def load_template_seed() -> list:
    """The template creator library new tenants start from."""
    if not os.path.exists(TEMPLATE_SEED):
        return []
    try:
        with open(TEMPLATE_SEED, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


# ── Provisioning ──────────────────────────────────────────────────
def provision_tenant(tenant_id: str):
    """Create a fresh tenant seeded from the template library. Idempotent."""
    from backend import repo  # local import: repo imports this module
    from datetime import datetime, timezone
    repo.provision_tenant(tenant_id, load_template_seed(),
                          created_at=datetime.now(timezone.utc).isoformat())


def tenant_exists(tenant_id: str) -> bool:
    from backend import repo
    return repo.tenant_exists(tenant_id)
