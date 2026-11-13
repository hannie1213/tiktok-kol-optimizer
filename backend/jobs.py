"""
Background housekeeping — the things a subscription product has to do on its
own, with nobody clicking anything:

  - renewal reminders: 7 days and 1 day before a paid term ends, and a notice
    once it has ended (needs SMTP; without it the scan is skipped and nothing
    is marked as sent)
  - expiring unpaid QR orders after 24 h
  - deleting demo accounts past their retention window

A daemon thread in each worker runs `run_once()` every JOBS_INTERVAL_SECONDS
(default hourly). Every job is idempotent and reminders are claimed in the
database before sending (repo.plan_claim_reminder), so several workers — or a
restart in the middle of a run — never mail the same notice twice.

JOBS_ENABLED=0 switches the thread off (e.g. when a cron job calls run_once).
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from backend import mailer, repo, settings

log = logging.getLogger("kol.jobs")

INTERVAL = max(60, int(os.environ.get("JOBS_INTERVAL_SECONDS") or 3600))
_FIRST_RUN_DELAY = 30
# A term that ended longer ago than this gets no "it has ended" notice — so
# switching email on for the first time doesn't mail every old lapse.
_EXPIRED_NOTICE_WINDOW = timedelta(days=3)

_state = {"running": False, "last_run": None, "last_result": None, "last_error": None}
_started = threading.Lock()
_stop = threading.Event()


def _stage(expires_at: str, now: datetime) -> Optional[str]:
    """Which notice a term is due: "7", "1", "0" (ended) or None."""
    try:
        left = datetime.fromisoformat(expires_at) - now
    except ValueError:
        return None
    if left > timedelta(days=7):
        return None
    if left > timedelta(days=1):
        return "7"
    if left > timedelta(0):
        return "1"
    return "0" if -left <= _EXPIRED_NOTICE_WINDOW else None


def send_renewal_reminders(now: Optional[datetime] = None) -> int:
    from backend.auth import DEMO_SUFFIX
    from backend.billing import PLANS
    if not mailer.enabled():
        return 0
    now = now or datetime.now(timezone.utc)
    sent = 0
    for row in repo.plans_with_expiry():
        stage = _stage(row["expires_at"], now)
        if stage is None:
            continue
        key = f"{row['expires_at']}|{stage}"
        if row["reminded"] == key:
            continue
        owner = repo.user_find_by_tenant(row["tenant_id"])
        if not owner or owner["email"].endswith(DEMO_SUFFIX):
            continue
        if not repo.plan_claim_reminder(row["tenant_id"], key):
            continue                                  # another worker took it
        name = PLANS.get(row["plan"], {}).get("name", row["plan"])
        link = mailer.base_url()
        if stage == "0":
            ok = mailer.send_plan_expired(owner["email"], name, link, owner["lang"])
        else:
            left = datetime.fromisoformat(row["expires_at"]) - now
            days = max(1, -(-left.total_seconds() // 86400))
            ok = mailer.send_plan_expiring(owner["email"], name, int(days),
                                           row["expires_at"][:10], link, owner["lang"])
        sent += bool(ok)
    return sent


def run_once(now: Optional[datetime] = None) -> dict:
    """Run every job once. Each is isolated: one failing never blocks the rest."""
    from backend import auth, payments
    result = {}
    for name, job in (("reminders", lambda: send_renewal_reminders(now)),
                      ("orders_expired", payments._expire_stale),
                      ("demos_swept", auth._sweep_expired_demos)):
        try:
            result[name] = job()
        except Exception as e:                        # noqa: BLE001 — keep the loop alive
            log.exception("job %s failed", name)
            result[name] = f"error: {type(e).__name__}"
            _state["last_error"] = f"{name}: {type(e).__name__}"
    _state["last_run"] = datetime.now(timezone.utc).isoformat()
    _state["last_result"] = result
    return result


def _loop():
    if _stop.wait(_FIRST_RUN_DELAY):
        return
    while True:
        run_once()
        if _stop.wait(INTERVAL):
            return


def start():
    """Start the scheduler thread once per process (no-op when disabled)."""
    if not settings.JOBS_ENABLED or not _started.acquire(blocking=False):
        return
    _state["running"] = True
    threading.Thread(target=_loop, name="kol-jobs", daemon=True).start()
    log.info("background jobs started (every %ss)", INTERVAL)


def status() -> dict:
    return {"enabled": settings.JOBS_ENABLED, "running": _state["running"],
            "interval_seconds": INTERVAL, "last_run": _state["last_run"],
            "last_result": _state["last_result"], "last_error": _state["last_error"]}
