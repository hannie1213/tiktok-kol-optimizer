"""
Workspace activity trail — who in the team did what, newest first.

Not to be confused with the operator's audit log (backend/admin.py): this one
belongs to the merchant's workspace and is visible to everyone in it.
Recording never raises: losing a log line must not fail the action itself.
"""
import logging
from datetime import datetime, timezone

from backend import repo

log = logging.getLogger("kol.activity")
KIND = "activity"


def record(tenant_id: str, action: str, target: str = "", detail: str = ""):
    try:
        from backend.auth import current_user          # deferred: auth imports nothing from here
        user = current_user()
        repo.doc_add(tenant_id, KIND, {
            "actor": user["email"] if user else "",
            "action": action, "target": str(target)[:200], "detail": str(detail)[:300],
        }, datetime.now(timezone.utc).isoformat())
    except Exception:                                   # noqa: BLE001
        log.exception("could not record workspace activity")


def recent(tenant_id: str, limit: int = 200) -> list:
    return repo.docs_list(tenant_id, KIND, limit=limit)
