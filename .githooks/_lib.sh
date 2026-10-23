# Shared helpers for the hooks in this directory (sourced, not a hook itself). POSIX sh.
# Enable the hooks once per clone with: make hooks

ROOT=$(git rev-parse --show-toplevel) || exit 1
cd "$ROOT" || exit 1

if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
if [ -x .venv/bin/ruff ]; then RUFF=.venv/bin/ruff; else RUFF=ruff; fi

fail() {
    printf '\n[%s] FAILED: %s\n' "$HOOK" "$1" >&2
    [ -n "$2" ] && printf '  fix: %s\n' "$2" >&2
    exit 1
}

need_ruff() {
    command -v "$RUFF" >/dev/null 2>&1 \
        || fail "ruff not found" "run 'make install' (installs the dev extra with the pinned ruff)"
}

# Offline unit tests: a fake key wins over any real one in the shell or .env (config uses
# setdefault), and live tests stay skipped, so no hook ever calls the model.
run_offline_tests() {
    printf '[%s] offline unit tests\n' "$HOOK"
    OPENROUTER_API_KEY=test-not-a-key RUN_LIVE_TESTS= \
        "$PY" -m unittest discover -q tests >/dev/null 2>"${TMPDIR:-/tmp}/githook-tests.$$" || {
        tail -n 40 "${TMPDIR:-/tmp}/githook-tests.$$" >&2
        rm -f "${TMPDIR:-/tmp}/githook-tests.$$"
        fail "unit tests are red" "run 'make test' and fix the failures above"
    }
    rm -f "${TMPDIR:-/tmp}/githook-tests.$$"
}
