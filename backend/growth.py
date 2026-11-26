"""
Growth: a free Pro trial and referral rewards.

Trial
-----
Every workspace may try Pro once for TRIAL_DAYS days, with no payment. The
trial is an ordinary Pro term with an end date, so the plan drops back to Free
by itself when it ends (repo.plan_get treats a lapsed term as Free). AI video
clips are paid to the video provider per clip, so during a trial they stay at
the Free allowance.

Referrals
---------
Each workspace has a short code; sign-up links look like /?ref=CODE. When a
workspace that signed up with a code has its first payment confirmed by the
operator, both workspaces get REWARD_DAYS more days: the paid plan is
extended, and a Free workspace gets Pro for that long. A reward needs a real,
operator-confirmed payment, so throwaway sign-ups earn nothing.

Storage: TenantDoc kinds "trial", "referral" (the workspace's own code) and
"referred_by" (who invited it), plus one system doc per code ("ref:<CODE>")
mapping the code to its workspace with invite and reward counts.
"""
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from backend import ratelimit, repo
from backend.auth import DEMO_SUFFIX, require_owner, require_tenant

log = logging.getLogger("growth")
router = APIRouter()

TRIAL_DAYS = 7
REWARD_DAYS = 30
_TRIAL, _MINE, _REFERRED = "trial", "referral", "referred_by"
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"^[A-Z2-9]{4,12}$")
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_demo(tenant_id: str) -> bool:
    user = repo.user_find_by_tenant(tenant_id)
    return not user or user["email"].endswith(DEMO_SUFFIX)


# ── Trial ─────────────────────────────────────────────────────────
def trial_state(tenant_id: str) -> dict:
    info = repo.plan_info(tenant_id)
    used = repo.docs_list(tenant_id, _TRIAL, limit=1)
    if used:
        ends = used[0].get("ends_at")
        return {"days": TRIAL_DAYS, "available": False,
                "active": info["plan"] == "pro" and info["expires_at"] == ends, "ends_at": ends}
    ever_paid = any(o["status"] == "paid" for o in repo.orders_for_tenant(tenant_id, limit=50))
    return {"days": TRIAL_DAYS, "active": False, "ends_at": None,
            "available": info["plan"] == "free" and not ever_paid and not _is_demo(tenant_id)}


def trial_active(tenant_id: str) -> bool:
    return trial_state(tenant_id)["active"]


@router.post("/billing/trial")
def start_trial(request: Request, tenant_id: str = Depends(require_owner)):
    if _is_demo(tenant_id):
        raise HTTPException(status_code=403, detail="Create a free account first — demo sessions can't start a trial.")
    ratelimit.enforce("trial", ratelimit.client_ip(request), limit=3, window=86400,
                      detail="Too many trials from this network — please try again tomorrow.")
    with _lock:
        state = trial_state(tenant_id)
        if not state["available"]:
            raise HTTPException(status_code=409, detail="This workspace has already used its free trial.")
        now = _now()
        ends = (now + timedelta(days=TRIAL_DAYS)).isoformat()
        repo.doc_add(tenant_id, _TRIAL, {"started_at": now.isoformat(), "ends_at": ends}, now.isoformat())
        repo.plan_set(tenant_id, "pro", now.isoformat(), expires_at=ends)
    user = repo.user_find_by_tenant(tenant_id)
    repo.audit_add("trial.start", user["email"] if user else tenant_id, f"pro until {ends[:10]}",
                   ratelimit.client_ip(request))
    return trial_state(tenant_id)


# ── Referrals ─────────────────────────────────────────────────────
def _code_doc(code: str) -> Optional[dict]:
    return repo.system_get("ref:" + code)


def my_code(tenant_id: str) -> str:
    with _lock:
        mine = repo.docs_list(tenant_id, _MINE, limit=1)
        if mine:
            return mine[0]["code"]
        now = _now().isoformat()
        while True:
            code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
            if _code_doc(code) is None:
                break
        repo.system_set("ref:" + code, {"tenant_id": tenant_id, "invited": 0, "rewarded": 0}, now)
        repo.doc_add(tenant_id, _MINE, {"code": code}, now)
        return code


def record_signup(new_tenant: str, code: Optional[str]) -> bool:
    """Remember who invited a new workspace. Unknown codes are ignored."""
    code = (code or "").strip().upper()
    if not _CODE_RE.match(code):
        return False
    with _lock:
        entry = _code_doc(code)
        if not entry or entry["tenant_id"] == new_tenant or repo.docs_list(new_tenant, _REFERRED, limit=1):
            return False
        now = _now().isoformat()
        repo.doc_add(new_tenant, _REFERRED, {"code": code, "referrer": entry["tenant_id"], "rewarded": False}, now)
        repo.system_set("ref:" + code, {**_strip(entry), "invited": int(entry.get("invited", 0)) + 1}, now)
    return True


def _strip(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in ("id", "created_at")}


def extend_bonus(tenant_id: str, days: int = REWARD_DAYS) -> Optional[str]:
    """Add `days` to the workspace's plan: a running paid term gets longer,
    Free becomes Pro for that long. A permanent plan is left as it is.
    Returns the new end date, or None when nothing changed."""
    info = repo.plan_info(tenant_id)
    now = _now()
    if info["plan"] == "free":
        plan, start = "pro", now
    elif info["expires_at"] is None:
        return None
    else:
        plan, start = info["plan"], max(now, datetime.fromisoformat(info["expires_at"]))
    ends = (start + timedelta(days=days)).isoformat()
    repo.plan_set(tenant_id, plan, now.isoformat(), expires_at=ends)
    return ends


def reward_first_payment(tenant_id: str) -> Optional[dict]:
    """Called when the operator confirms a payment. Rewards both sides once."""
    with _lock:
        rows = repo.docs_list(tenant_id, _REFERRED, limit=1)
        if not rows or rows[0].get("rewarded"):
            return None
        ref = rows[0]
        repo.doc_put(tenant_id, _REFERRED, ref["id"], {**_strip(ref), "rewarded": True})
        entry = _code_doc(ref["code"])
        if entry:
            repo.system_set("ref:" + ref["code"], {**_strip(entry), "rewarded": int(entry.get("rewarded", 0)) + 1},
                            _now().isoformat())
    referrer = ref["referrer"]
    out = {"referred_until": extend_bonus(tenant_id), "referrer": referrer, "referrer_until": None}
    if repo.user_find_by_tenant(referrer):
        out["referrer_until"] = extend_bonus(referrer)
    log.info("referral reward %s -> %s", referrer, tenant_id)
    return out


@router.get("/billing/referral")
def referral_info(tenant_id: str = Depends(require_tenant)):
    if _is_demo(tenant_id):
        return {"code": None, "path": None, "invited": 0, "rewarded": 0, "reward_days": REWARD_DAYS}
    code = my_code(tenant_id)
    entry = _code_doc(code) or {}
    return {"code": code, "path": f"/?ref={code}", "invited": int(entry.get("invited", 0)),
            "rewarded": int(entry.get("rewarded", 0)), "reward_days": REWARD_DAYS}
