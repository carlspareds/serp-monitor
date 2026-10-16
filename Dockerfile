# SERP Monitor in a container (README: Run with Docker). One process, one
# worker, SQLite under /data; sign-in required like everywhere else.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first (pinned in requirements.txt) so code edits don't reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the app reads at runtime. `--app-dir src` puts src/ on the module
# path and dashboard.main adds the repo root, so config/ is importable; the
# assistant reads docs/assistant-knowledge.md; scripts/ keeps seed_demo_data.py
# and check_config.py usable with `docker compose run`.
COPY config/ config/
COPY src/ src/
COPY static/ static/
COPY templates/ templates/
COPY docs/ docs/
COPY scripts/ scripts/

# Non-root. Everything the app writes -- database, evidence/ and exports/
# (siblings of the database file), the .env that Settings saves keys to --
# lives under /data. /app/data points there too, so the few paths resolved
# against the repo root ("data/...") land in the volume as well.
ARG APP_UID=1000
RUN useradd --uid "${APP_UID}" --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data \
    && ln -s /data /app/data
USER app
VOLUME /data

# HOST is what the startup config check looks at: with 0.0.0.0 it refuses
# ALLOW_NO_AUTH=1 (local demos only), so the container always asks to sign in.
ENV HOST=0.0.0.0 \
    DB_PATH=/data/seo_dashboard.db \
    ENV_FILE=/data/.env

EXPOSE 8000

# /health answers 200 (ok/degraded) or 503 (database down, scheduler not
# running); urlopen raises on 503, so the check fails. Not redirected by FORCE_HTTPS.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# Single worker on purpose: sessions and rate limits live in the process.
CMD ["uvicorn", "dashboard.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000"]
