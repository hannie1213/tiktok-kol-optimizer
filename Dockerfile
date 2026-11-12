# TikTok Shop KOL Matrix Optimizer — production image.
#
#   docker build -t kol-optimizer .
#   docker compose up -d                 # app + PostgreSQL (recommended; see docs/DEPLOYMENT.md)
#
# Single container on SQLite (small single-node installs) — mount /app/state
# so the database survives redeploys:
#   docker run -p 8000:8000 -v kol_state:/app/state \
#       -e APP_ENV=production -e JWT_SECRET=... -e WEB_CONCURRENCY=1 kol-optimizer
#
# WEB_CONCURRENCY controls the number of uvicorn worker processes; since the
# optimization engine is CPU-bound pure Python, workers ≈ CPU cores is the
# right starting point.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY engine/ engine/
COPY frontend/ frontend/
# Read-only reference data baked into the image: the seed library every new
# tenant starts from, the merchant guide, and the synthetic-pool generator.
COPY data/generator.py data/seed_kols.json data/merchant_guide.json data/

# Run unprivileged. /app/state is the only writable path (SQLite file when
# DATABASE_URL is unset); with PostgreSQL the container is fully stateless.
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /app/state \
    && chown app:app /app/state
ENV SQLITE_DIR=/app/state
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# --proxy-headers + FORWARDED_ALLOW_IPS let the app see the real client IP
# (for rate limiting) when it sits behind a reverse proxy.
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-4} --proxy-headers"]
