# TeslaFare: FastAPI app. Prices come from visitors' browsers (the TeslaFare helper), so the image
# has no browser. On every start the container can update itself from git: see docker/entrypoint.sh.
FROM python:3.12-slim

# git: the container's self-update; tzdata: local times in logs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# Same uv version that wrote uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.9.4 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

# Dependencies first so code changes don't reinstall them. The optional "browser" extra
# (Playwright/Selenium, for server-side Tesla lookups) is deliberately not installed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/teslafare-entrypoint

# Unprivileged user; /data holds the SQLite cache and the saved Supercharger prices, /src the git clone.
RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data /src \
 && chown app:app /data /src \
 && chown -R app:app /opt/venv \
 && sed -i 's/\r$//' /usr/local/bin/teslafare-entrypoint \
 && chmod +x /usr/local/bin/teslafare-entrypoint
USER app

ENV HOME=/home/app \
    CACHE_DB_PATH=/data/cache.sqlite3 \
    CHARGER_KNOWLEDGE_PATH=/data/superchargers.json \
    SERVER_PRICE_LOOKUPS=false

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Updates from git (if enabled), then runs one uvicorn worker: trip progress and
# "why not this charger?" data live in process memory.
ENTRYPOINT ["/usr/local/bin/teslafare-entrypoint"]
