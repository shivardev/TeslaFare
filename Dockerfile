# TeslaFare: FastAPI app (+ optional headless Firefox for server-side Tesla lookups).
# On every start the container can update itself from git: see docker/entrypoint.sh.
FROM python:3.12-slim-bookworm

ARG TARGETARCH
ARG GECKODRIVER_VERSION=0.37.1

# Firefox ESR (Debian) + geckodriver, used by Selenium when Tesla blocks plain HTTP requests.
RUN apt-get update \
 && apt-get install -y --no-install-recommends firefox-esr ca-certificates curl tzdata git \
 && case "${TARGETARCH:-amd64}" in \
      amd64) GECKO_ARCH=linux64 ;; \
      arm64) GECKO_ARCH=linux-aarch64 ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-${GECKO_ARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin geckodriver \
 && apt-get purge -y curl && apt-get autoremove -y \
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

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/teslafare-entrypoint

# Unprivileged user; /data holds the SQLite cache and the learned Supercharger prices.
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
    TESLA_PLAYWRIGHT_HEADLESS=true \
    TESLA_BROWSER_BACKEND=selenium \
    TESLA_PLAYWRIGHT_BROWSER=firefox

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Updates from git (if enabled), then runs one uvicorn worker: trip progress and
# "why not this charger?" data live in process memory.
ENTRYPOINT ["/usr/local/bin/teslafare-entrypoint"]
