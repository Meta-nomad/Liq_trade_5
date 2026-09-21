FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DB_PATH=/data/paper_v040.db

WORKDIR /app

COPY Liquidation_Lab_v0.4.7/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && addgroup --system paper \
    && adduser --system --ingroup paper --home /app paper \
    && mkdir -p /data \
    && chown -R paper:paper /app /data

COPY --chown=paper:paper Liquidation_Lab_v0.4.7/app ./app
COPY --chown=paper:paper Liquidation_Lab_v0.4.7/pyproject.toml Liquidation_Lab_v0.4.7/README_RU.md ./

# Railway mounts a persistent volume at runtime.  Its ownership is supplied
# by the platform and can be root:root, so dropping privileges here makes
# SQLite fail with "unable to open database file" before the app starts.
# This is a paper-only service with no exchange credentials or order client;
# keep the process as root so the mounted /data volume is writable.

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=3)"

CMD ["python", "-m", "app"]
