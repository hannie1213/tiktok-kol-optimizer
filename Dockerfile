# TikTok Shop KOL Matrix Optimizer — production image.
#
#   docker build -t kol-optimizer .
#   docker run -p 8000:8000 -e JWT_SECRET=... kol-optimizer          # SQLite (single node)
#   docker compose up                                                # PostgreSQL (recommended)
#
# WEB_CONCURRENCY controls the number of uvicorn worker processes; since the
# optimization engine is CPU-bound pure Python, workers ≈ CPU cores is the
# right starting point.
FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY engine/ engine/
COPY frontend/ frontend/
COPY data/generator.py data/seed_kols.json data/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-4}"]
