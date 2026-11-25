"""
Runtime configuration read from environment variables.

  APP_ENV         "production" turns on strict checks (default "development")
  JWT_SECRET      required in production, at least 32 characters
  PUBLIC_BASE_URL e.g. https://kolmatrix.com, used to build links in emails
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM
                  outgoing mail for password reset; if unset, links are logged
  CORS_ORIGINS    comma-separated allowed origins (default "*" in development)
"""
import os

DEV_SECRET = "dev-insecure-secret-change-me"

APP_ENV = os.environ.get("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV == "production"
JWT_SECRET = os.environ.get("JWT_SECRET") or DEV_SECRET
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "" if IS_PRODUCTION else "*").split(",") if o.strip()]


def validate():
    """Refuse to boot a production server with unsafe settings."""
    if not IS_PRODUCTION:
        return
    problems = []
    if JWT_SECRET == DEV_SECRET or len(JWT_SECRET) < 32:
        problems.append("JWT_SECRET must be set to a random string of at least 32 characters")
    if os.environ.get("DEFAULT_TENANT"):
        problems.append("DEFAULT_TENANT must not be set in production (it disables login)")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
