# Production image for the KOL Matrix Optimizer (FastAPI + static SPA).
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    DATABASE_PATH=/var/data/app.db \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend ./backend
COPY engine ./engine
COPY frontend ./frontend
COPY data ./data

# Run as a non-root user; /var/data is where the persistent disk is mounted.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /var/data && chown -R app:app /var/data /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)"

# One worker: the SQLite store and the auth rate limiter are single-process by design.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
