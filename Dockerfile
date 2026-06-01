# ── Wattwise ──────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

LABEL version="0.1.0-beta" description="Wattwise"

# Install dependencies first (layer cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY config.py etl.py db.py main.py scheduler.py ./
COPY static/ ./static/

# Mount points
RUN mkdir -p /app/data /app/backups

EXPOSE 9521

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9521"]
