# syntax=docker/dockerfile:1
# Single stage on purpose: pure-Python deps, no compiler, so a builder stage buys nothing.
# python:3.12-slim-bookworm multi-arch index digest, pinned 2026-10-22. Bump tag + digest together.
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/chatbot.db \
    CALL_LOG_PATH=/data/logs/calls.jsonl

WORKDIR /app

# Install only the locked runtime dependencies, not the package itself:
# chatbot/config.py derives ROOT from its own file location and reads web/ (and docs/)
# beside it, so the code must run from /app, not from site-packages.
# requirements.lock is generated from pyproject.toml (see DEPLOY.md "Dependencies"); every
# package, transitive ones included, is pinned and sha256-checked.
COPY requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements.lock

# Non-root runtime user. /data is the Fly volume mount point (plain dir when run locally).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/logs \
    && chown -R app:app /data

# Code stays root-owned (read-only to the app user). .dockerignore keeps .env, data/, logs/ out.
COPY . .

# Numeric uid (the `app` user above), so runtimes can check non-root without reading /etc/passwd.
USER 10001

EXPOSE 8080
# Python maps SIGINT to KeyboardInterrupt, which server.main() handles (Fly's default kill_signal is SIGINT).
STOPSIGNAL SIGINT

# Liveness: /healthz answers 200 without auth; urlopen raises on any non-2xx, so exit 1.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request as r\ntry: r.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4)\nexcept Exception: sys.exit(1)"]

CMD ["python", "-m", "chatbot.server"]
