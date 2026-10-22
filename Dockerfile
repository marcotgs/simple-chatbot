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

# Install only the dependencies declared in pyproject.toml, not the package itself:
# chatbot/config.py derives ROOT from its own file location and reads web/ (and docs/)
# beside it, so the code must run from /app, not from site-packages.
# ponytail: no lockfile yet, versions float within pyproject ranges; add a hashed
# requirements lock (pip-compile / uv) and install with --require-hashes when it matters.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Non-root runtime user. /data is the Fly volume mount point (plain dir when run locally).
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/logs \
    && chown -R app:app /data

# Code stays root-owned (read-only to the app user). .dockerignore keeps .env, data/, logs/ out.
COPY . .

USER app

EXPOSE 8080
# Python maps SIGINT to KeyboardInterrupt, which server.main() handles (Fly's default kill_signal is SIGINT).
STOPSIGNAL SIGINT

# Liveness only: any HTTP response (including 401 when WEB_TOKEN is set) means the server is up.
# TODO(T6): switch to /healthz (2xx check) once server.py exposes it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request as r,urllib.error as e\ntry: r.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT', '8080'), timeout=4)\nexcept e.HTTPError: pass\nexcept Exception: sys.exit(1)"]

CMD ["python", "-m", "chatbot.server"]
