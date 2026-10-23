# simple-chatbot

A personal assistant for one owner. Everything goes through one function,
`core.send(user_id, thread_id, text) -> reply` in `chatbot/core.py`. A web chat page, a JSON API, a
Slack bot and the scenario runner all call it. Memory lives in SQLite: facts with scope and
history, rolling summaries, full-text search over old messages and notes on long documents. Every
model call (OpenRouter, through the official `openrouter` Python SDK) stays under an input-token
limit, and its provider-reported usage is logged.

Depth lives in the [wiki](wiki/Home.md). This page covers running it and extending it.

## Quick start

Needs Python 3.11+ and an OpenRouter API key.

```sh
make install              # creates .venv, installs the hashed dev lock + the package (editable)
cp .env.example .env      # then set OPENROUTER_API_KEY (and FORGET_SECRET)
make run                  # web server on http://127.0.0.1:8765/ (+ Slack bot if its tokens are set)
```

`make` alone lists every target. `make run` sets `PORT=8765` (override with `make run PORT=9000`);
`python -m chatbot.server` uses `PORT` from `.env` (default 8000).

Open `http://127.0.0.1:8765/` in a browser. Each "New session" is its own thread.

## Core entry point

`send(user_id, thread_id, text)` stores the user message, answers it and stores the reply. Turns of
one thread run one at a time; different threads run in parallel.

Over HTTP:

```sh
curl -s http://127.0.0.1:8765/send \
  -H 'Content-Type: application/json' \
  -H "X-Web-Token: $WEB_TOKEN" \
  -d '{"thread_id": "demo", "text": "Hi, my manager is Alex Example."}'
# -> {"reply": "..."}
```

`user_id` is optional and defaults to `user-1`. The `X-Web-Token` header is needed only when
`WEB_TOKEN` is set. Other routes: `GET /threads`, `GET /history?thread_id=...`, `GET /healthz`.
Host, origin and token rules: [Architecture](wiki/Architecture.md).

From the command line (a live model call: needs `OPENROUTER_API_KEY` and spends credit):

```sh
.venv/bin/python -c 'from chatbot import core; print(core.send("user-1", "demo", "What is my manager called?"))'
```

## Slack app

The bot uses Socket Mode (outbound websocket, no public URL). Set `SLACK_BOT_TOKEN`,
`SLACK_APP_TOKEN` and `SLACK_USER_ID` in `.env`. `make run` then starts the bot inside the web
process; `make slack` runs the bot alone. It answers DMs, `/bot <text>`, `@<bot> <text>` and file
uploads in the DM, from the owner only. `/bot new` starts a fresh thread.

App creation, scopes and troubleshooting: [Slack setup](wiki/Slack-setup.md) and
[SLACK_SETUP.md](SLACK_SETUP.md).

## Scenarios

`scenarios/run.py` runs S1-S7. Each scenario runs in a child process against a fresh temp DB, with
deterministic assertions and no LLM judge. A scenario also fails if any call reached
`MAX_INPUT_TOKENS` or lacked provider usage.

```sh
make scenarios-dry        # fake model, gate off: no network, no credit (plumbing check)
make scenarios            # all 7, live model (spends OpenRouter credit)
make scenarios S=1,4      # a subset (`make scenario` is an alias)
```

A full live run takes about 20 minutes and prints progress to stderr while it works. Stdout gets
one PASS/FAIL line per scenario and a summary. The exit code is non-zero when any scenario fails.
What each scenario checks, cost and how to read the output: [Scenarios](wiki/Scenarios.md).

## Adding a capability

1. Add one module in `chatbot/capabilities/` that exports a `CAPABILITY` dict. Modules are found
   with `pkgutil` (names starting with `_` are skipped), so no other file changes.
2. Write each tool as `fn(user_id, thread_id, **args) -> str`. Exceptions go back to the model as
   `error: ...`. Results over 12,000 chars are truncated, so keep output bounded.
3. Restart the process, then add an offline test next to `tests/test_capabilities.py` and run
   `make check`.

```python
# chatbot/capabilities/clock.py
from datetime import UTC, datetime


def current_time(user_id: str, thread_id: str) -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


CAPABILITY = {
    "name": "clock",  # unique; the model calls load_capability("clock")
    "one_line": "tell the current date and time",  # in every system prompt: keep it short
    "instructions": "Call current_time when asked for the date or time. Answer in one line.",
    "tools": [
        (
            {
                "name": "current_time",
                "description": "Current UTC date and time (ISO 8601).",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            current_time,
        ),
    ],
}
```

Only the `one_line` index sits in the prompt. A capability's tools are exposed after the model
calls `load_capability(name)` in the same `send`. Details: [Capabilities guide](wiki/Capabilities-guide.md).

## How it works in brief

- **Facts are global by default.** Extraction runs after each turn and stores durable facts for all
  threads. A new value for the same key supersedes the old one, which stays as history.
  [Memory model](wiki/Memory-model.md)
- **Session and ephemeral scopes.** "For this chat only" stays in one thread; "for this draft only"
  is never stored. [Memory model](wiki/Memory-model.md)
- **Doc notes are per thread.** `summarize_doc` reads a document page by page and stores notes for
  later questions in that thread. [Memory model](wiki/Memory-model.md)
- **Auto-recall.** Before each answer, older messages of the same thread (outside the last 20) are
  searched with the user's message, and the top hits go into the context.
  [Memory model](wiki/Memory-model.md)
- **Forget.** It runs only when the user's own message asks for it and names the fact. A sentence
  that negates the request ("don't forget ...") is refused as a whole; punctuation and markup other than a
  sentence break do not end the negation. Anything wider than one stored fact
  is refused. A forgotten value is purged from every store and checked afterwards.
  [Memory model](wiki/Memory-model.md)
- **Token budget.** `MAX_INPUT_TOKENS` (default 8000) is the hard limit per model call, checked
  against provider usage. Context is trimmed to `TARGET_INPUT_TOKENS` (default 6500) first. Every
  call is logged in table `llm_calls` and `logs/calls.jsonl`. [Token budget](wiki/Token-budget.md)
- **Components and request flow.** [Architecture](wiki/Architecture.md)

## Tests and checks

```sh
make test                 # offline unit + integration tests (fake model, no network)
make lint                 # ruff format --check + ruff check
make check                # lint + offline tests (what the hooks run)
make test-live            # live suites; spends credit, needs OPENROUTER_API_KEY
```

Offline test modules set `GATE_MODEL=""` before importing `chatbot`, so they never call the
network. A new offline test module must do the same.

## Configuration

Copy `.env.example` to `.env`; real environment variables win. The main settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | (required) | OpenRouter key for all model calls |
| `LLM_MODEL` | `openai/gpt-4.1-mini` | Chat model; must support tool calling and return usage |
| `MAX_INPUT_TOKENS` | `8000` | Hard input limit per call |
| `TARGET_INPUT_TOKENS` | `6500` | Trim target; must be below MAX |
| `FORGET_SECRET` | (generated `forget.key` next to the DB) | HMAC key for forget tombstones; keep it stable |
| `DB_PATH` | `./data/chatbot.db` | SQLite file |
| `WEB_TOKEN` | (empty = no auth) | Shared secret for the web page and API |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_USER_ID` | (empty) | Slack bot; starts only with both tokens |

Every other setting (`JUDGE_MODEL`, `GATE_MODEL`, `HOST`, `ALLOWED_HOSTS`, `MAX_FILE_BYTES`,
`DOC_NOTE_WORKERS`, ...) is documented in `.env.example` and in the
[configuration table](wiki/Architecture.md#configuration).

## Known limits

- No upload size cap by default (`MAX_FILE_BYTES=0`).
- The forget cue is English only; a request in another language is refused.
- Forget tombstones are global, not per user. Fine for one owner.
- `summarize_doc` makes one model call per page, so long files are slow.
- The real Slack Socket Mode round-trip is checked by hand only.

Full list: [Known limits](wiki/Known-limits.md).

## Documentation

- [Home](wiki/Home.md): overview and quick start.
- [Architecture](wiki/Architecture.md): components, `core.send` flow, SQLite tables, file layout.
- [Memory model](wiki/Memory-model.md): scopes, extraction, summaries, retrieval, doc notes, forget.
- [Token budget](wiki/Token-budget.md): estimator, guard, trimming phases, call log.
- [Capabilities guide](wiki/Capabilities-guide.md): gating, tools, adding a capability.
- [Scenarios](wiki/Scenarios.md): S1-S7, how a run works, reading the output.
- [Slack setup](wiki/Slack-setup.md): create the app, run it, what it answers.
- [Known limits](wiki/Known-limits.md): accepted risks and open issues.
- [SLACK_SETUP.md](SLACK_SETUP.md), [scenarios/README.md](scenarios/README.md),
  [NOTES.md](NOTES.md), [PLAN.md](PLAN.md): setup steps, scenario details, design notes, plan.
