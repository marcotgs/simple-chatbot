# Run `make` (or `make help`) for the list. The app reads .env itself; nothing is loaded here.
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
RUFF := $(if $(wildcard .venv/bin/ruff),.venv/bin/ruff,ruff)
PORT ?= 8765
S ?=

.DEFAULT_GOAL := help
.PHONY: help lock install hooks run slack test test-live fmt lint check scenario scenarios scenarios-dry gen-docs gen-history clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# Cooldown rule: every pin is >= 7 days old on PyPI. Security floors live in pyproject.toml
# (pypdf>=6.21.2) and lock-constraints.txt (certifi). If a floor or an exact pin (ruff in the dev
# extra) is newer than the cutoff, uv fails loudly; exempt that package with a reason via
#   make lock LOCK_FLAGS='--exclude-newer-package certifi=<date> --exclude-newer-package ruff=<date>'
# Both locks compile to *.tmp first and replace the real files only if both succeed. The tmp
# files start as copies of the current locks so uv keeps existing pins as preferences.
UV ?= uv
LOCK_FLAGS ?=
LOCK_COMPILE = $(UV) pip compile pyproject.toml -c lock-constraints.txt --generate-hashes \
	--universal --no-header --exclude-newer "$$cutoff" $(LOCK_FLAGS)
lock: ## Re-lock requirements[-dev].lock (uv, 7-day cooldown; exceptions via LOCK_FLAGS=...)
	trap 'rm -f requirements.lock.tmp requirements-dev.lock.tmp' EXIT; \
	cutoff="$$($(PY) -c 'import datetime as d; print((d.datetime.now(d.UTC) - d.timedelta(days=7)).date())')" \
	&& test -n "$$cutoff" \
	&& for f in requirements.lock requirements-dev.lock; do \
		if [ -f "$$f" ]; then cp "$$f" "$$f.tmp"; fi; done \
	&& $(LOCK_COMPILE) -o requirements.lock.tmp \
	&& $(LOCK_COMPILE) --extra dev -o requirements-dev.lock.tmp \
	&& mv requirements.lock.tmp requirements.lock \
	&& mv requirements-dev.lock.tmp requirements-dev.lock

install: ## Create .venv if missing; install the hashed dev lock and the package (editable)
	@test -x .venv/bin/python || python3 -m venv .venv
	.venv/bin/python -m pip install --require-hashes --no-deps -r requirements-dev.lock
	.venv/bin/python -m pip install --no-deps -e .

hooks: ## Use the repo's git hooks (.githooks: format/lint/tests/secret scan, Conventional Commits)
	git config core.hooksPath .githooks

run: ## Web server (PORT=8765 default); starts the Slack bot too if its tokens are set
	PORT=$(PORT) $(PY) -m chatbot.server

slack: ## Slack bot only (Socket Mode)
	$(PY) -m chatbot.slack_app

test: ## Offline unit tests (live tests skip)
	$(PY) -m unittest discover tests

test-live: ## Live extraction, gate and flow tests (spends API credit; needs OPENROUTER_API_KEY)
	RUN_LIVE_TESTS=1 $(PY) -m unittest tests.test_extraction_quality tests.test_gate_quality tests.test_integration_live tests.test_live_docs_upload -v

fmt: ## Format and auto-fix lint (ruff)
	$(RUFF) format .
	$(RUFF) check --fix .

lint: ## Check formatting and lint (ruff, no changes)
	$(RUFF) format --check .
	$(RUFF) check .

check: lint test ## lint + offline tests (what the hooks run)

scenario: scenarios ## Alias for scenarios (same S= subset)

scenarios: ## Live S1-S7 (spends API credit); subset with S=1,4
	$(PY) -m scenarios.run $(if $(S),--only $(S))

scenarios-dry: ## Scenario plumbing: fake model, gate off (no network)
	$(PY) -m scenarios.run --dry

gen-docs: ## Regenerate docs/*.md and docs/_manifest.json (seeded)
	$(PY) scripts/gen_docs.py

gen-history: ## Regenerate scenarios/data/history_500.json + expected.json (seeded)
	$(PY) scripts/gen_history.py

clean: ## Remove caches and build artifacts (keeps data/, logs/, .venv)
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
	rm -rf *.egg-info build dist
