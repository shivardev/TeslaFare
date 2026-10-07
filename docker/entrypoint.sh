#!/bin/sh
# On every container start: update the app from git (when AUTO_UPDATE=true), then run it.
# The clone lives in its own volume (/src). If git or the network fails, the last good copy
# (or the code baked into the image, /app) is used, so a bad pull never stops the app from starting.
set -u

APP_DIR=/app
if [ "${AUTO_UPDATE:-true}" = "true" ] && [ -n "${REPO_URL:-}" ]; then
  BRANCH="${REPO_BRANCH:-main}"
  if [ -d /src/.git ]; then
    echo "[update] pulling ${REPO_URL} (${BRANCH})"
    # The clone only ever follows the remote, so any local state is discarded.
    if git -C /src fetch --depth 1 origin "$BRANCH" && git -C /src reset --hard "origin/${BRANCH}"; then
      echo "[update] now at $(git -C /src log -1 --format='%h %s')"
    else
      echo "[update] pull failed; starting the copy already in /src"
    fi
  else
    echo "[update] first start: cloning ${REPO_URL} (${BRANCH})"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" /src || echo "[update] clone failed; using the code built into the image"
  fi
  if [ -f /src/app/main.py ]; then
    APP_DIR=/src
    # Install any dependency changes from the new uv.lock (fast no-op when nothing changed).
    (cd /src && uv sync --frozen --no-dev --no-install-project) || echo "[update] dependency sync failed; continuing"
  fi
fi

echo "[start] running code from ${APP_DIR}"
cd "$APP_DIR"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips "*"
