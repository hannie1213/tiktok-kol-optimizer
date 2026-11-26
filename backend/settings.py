"""
Runtime configuration — one place for every environment switch, plus the
production safety checks that stop the app from booting misconfigured.

  APP_ENV              "development" (default) | "production".
                       Production refuses to start with an unsafe config:
                       missing/weak JWT_SECRET, DEFAULT_TENANT set, CORS "*".
  JWT_SECRET           Token signing key (required in production, ≥ 32 chars).
  ADMIN_KEY            Operator key for /admin/* and /billing/grant
                       (BILLING_ADMIN_KEY is accepted as a legacy alias).
  CORS_ORIGINS         Comma-separated browser origins allowed cross-origin.
                       Unset → same-origin only in production, "*" in dev.
  BILLING_SELF_SERVE   "1" lets a merchant switch to a PAID plan through
                       POST /billing/checkout without paying (demo/dev only).
                       Off by default: paid plans are activated by the
                       operator (or a payment webhook) after payment.
  RATE_LIMIT_ENABLED   "0" disables request throttling (tests).
  MAX_UPLOAD_MB        Largest accepted request body (default 10).
"""
import hmac
import os

_DEV_JWT_SECRET = "dev-insecure-secret-change-me"


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


APP_ENV = (os.environ.get("APP_ENV") or "development").strip().lower()
IS_PRODUCTION = APP_ENV in ("production", "prod")

JWT_SECRET = os.environ.get("JWT_SECRET") or _DEV_JWT_SECRET
ADMIN_KEY = os.environ.get("ADMIN_KEY") or os.environ.get("BILLING_ADMIN_KEY") or ""

_cors_raw = os.environ.get("CORS_ORIGINS")
if _cors_raw is None or not _cors_raw.strip():
    CORS_ORIGINS = [] if IS_PRODUCTION else ["*"]
else:
    CORS_ORIGINS = [o.strip().rstrip("/") for o in _cors_raw.split(",") if o.strip()]

BILLING_SELF_SERVE = _flag("BILLING_SELF_SERVE", False)
RATE_LIMIT_ENABLED = _flag("RATE_LIMIT_ENABLED", True)
# Hourly background jobs (renewal reminders, stale-order and demo cleanup).
JOBS_ENABLED = _flag("JOBS_ENABLED", True)
MAX_UPLOAD_BYTES = int(float(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024)


# ── QR payment (see backend/payments.py) ──────────────────────────
# Path of the receiving QR image inside frontend/. The feature is on when the
# file exists; delete the file (or point this at nothing) to switch it off.
PAY_QR_IMAGE = (os.environ.get("PAY_QR_IMAGE") or "pay/duitnow-qr.png").strip().lstrip("/")
PAY_PAYEE_NAME = (os.environ.get("PAY_PAYEE_NAME") or "").strip()
# Plan prices in backend/billing.py are set in ringgit, so orders are always MYR.
PAY_CURRENCY = "MYR"
# Where "a merchant says they paid" notices go (needs SMTP).
OPERATOR_EMAIL = (os.environ.get("OPERATOR_EMAIL") or "").strip()


def admin_key_ok(candidate) -> bool:
    """Constant-time operator-key check; always False when no key is configured."""
    if not ADMIN_KEY or not candidate:
        return False
    return hmac.compare_digest(str(candidate).encode(), ADMIN_KEY.encode())


def production_problems() -> list:
    """Human-readable reasons this configuration is unsafe to serve publicly."""
    problems = []
    if JWT_SECRET == _DEV_JWT_SECRET:
        problems.append("JWT_SECRET is not set (sign-in tokens could be forged)")
    elif len(JWT_SECRET) < 32:
        problems.append("JWT_SECRET is too short (use at least 32 random characters)")
    if os.environ.get("DEFAULT_TENANT"):
        problems.append("DEFAULT_TENANT is set (it disables authentication)")
    if "*" in CORS_ORIGINS:
        problems.append('CORS_ORIGINS contains "*" (list your real origins, or leave it unset)')
    if BILLING_SELF_SERVE:
        problems.append("BILLING_SELF_SERVE is on (anyone could take a paid plan for free)")
    if os.environ.get("SMTP_HOST") and not os.environ.get("APP_BASE_URL"):
        problems.append("SMTP_HOST is set without APP_BASE_URL (links in emails need your public origin)")
    if ADMIN_KEY and len(ADMIN_KEY) < 16:
        problems.append("ADMIN_KEY is too short (use at least 16 random characters)")
    return problems


def validate():
    """Fail fast on an unsafe production config; called at import by main.py."""
    if not IS_PRODUCTION:
        return
    problems = production_problems()
    if problems:
        raise RuntimeError(
            "Refusing to start with APP_ENV=production:\n  - " + "\n  - ".join(problems)
        )
