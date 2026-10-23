# simple-chatbot

A personal assistant for one user with a single entry point, `core.send(user_id, thread_id, text)`.
A web chat page, a curl-able JSON API, a Slack bot (DM, `/bot`, @mention, file upload) and the
scenario runner all call it. It keeps long-term memory in SQLite: facts with scope and history,
rolling chat summaries, full-text search over old messages, notes on long documents, and a `forget`
that purges a value from every store. Every model call (OpenRouter, via its official `openrouter` Python SDK) stays under
a configurable input-token limit, and each call's provider-reported usage is logged.

More detail lives in the wiki: <https://github.com/marcotgs/simple-chatbot/wiki>
([Home](https://github.com/marcotgs/simple-chatbot/wiki/Home),
[Architecture](https://github.com/marcotgs/simple-chatbot/wiki/Architecture),
[Memory-model](https://github.com/marcotgs/simple-chatbot/wiki/Memory-model),
[Token-budget](https://github.com/marcotgs/simple-chatbot/wiki/Token-budget),
[Capabilities-guide](https://github.com/marcotgs/simple-chatbot/wiki/Capabilities-guide),
[Scenarios](https://github.com/marcotgs/simple-chatbot/wiki/Scenarios),
[Slack-setup](https://github.com/marcotgs/simple-chatbot/wiki/Slack-setup),
[Known-limits](https://github.com/marcotgs/simple-chatbot/wiki/Known-limits)).

## Quick start

Needs Python 3.11+ and an OpenRouter API key.

```sh
make install              # creates .venv, installs the hashed dev lock + the package (editable)
cp .env.example .env      # then set OPENROUTER_API_KEY (and FORGET_SECRET, see the env table)
make run                  # web server on http://127.0.0.1:8765/ (+ Slack bot if its tokens are set)
```

`make` alone lists every target. `make run` sets `PORT=8765` itself (override with
`make run PORT=9000`); `python -m chatbot.server` uses `PORT` from `.env` (default 8000).

Open the page at `http://127.0.0.1:8765/`. Each "New session" is its own thread. While a reply is
pending the page shows "working…" and disables Send for that session. Or call the API:

```sh
curl -s http://127.0.0.1:8765/send \
  -H 'Content-Type: application/json' \
  -H "X-Web-Token: $WEB_TOKEN" \
  -d '{"thread_id": "demo", "text": "Hi, my manager is Alex Example."}'
# -> {"reply": "..."}
```

API rules (`chatbot/server.py`):

- `POST /send` takes JSON `{thread_id, text, user_id?}` (user defaults to `user-1`) and requires
  `Content-Type: application/json` (otherwise 415). Body max 64 KB.
- `WEB_TOKEN`: when set, every request except `GET /` and `GET /healthz` must send it as header
  `X-Web-Token` or query `?token=` (401 otherwise). Open the page once as `/?token=...`; it moves the
  token to localStorage and sends it as the header. When `WEB_TOKEN` is empty there is no auth, and
  the server refuses to start on a non-loopback `HOST`.
- `Host` must be `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>` or listed in `ALLOWED_HOSTS`;
  a present `Origin` must match it (403 otherwise). This blocks CSRF and DNS rebinding.
- Other routes: `GET /threads`, `GET /history?thread_id=...`, `GET /healthz` (no auth).
- Token budget exceeded -> 413 with the reason; other failures -> 500 (details only in the server log).

## Architecture

```
web page / curl ─┐
Slack (Socket)  ─┼─> core.send(user_id, thread_id, text)
scenarios       ─┘      │ per-thread lock (turns of one thread run one at a time)
                        ├─ ingest_turn(user)           -> messages (+ FTS5)
                        ├─ build_context               -> system prompt + capability index
                        │                                 + <memory> block + last 20 messages,
                        │                                 trim_to_budget
                        ├─ run_tool_loop (max 6 steps) -> llm.chat, tools
                        ├─ filter_reply                -> redact forgotten values
                        └─ ingest_turn(assistant)      -> after_turn: gate -> fact extraction,
                                                          summary roll
```

- `chatbot/core.py`: `send`, `ingest_turn` (shared by live chat and the history loader),
  `build_context`, `trim_to_budget`, the tool loop and `register_tool`.
- `chatbot/llm.py`: the only module that calls the chat model. Budget check and call log.
- `chatbot/db.py`: SQLite schema and migrations (`PRAGMA user_version`).
- State is only SQLite (`DB_PATH`, default `data/chatbot.db`) plus files next to it
  (`data/uploads/`, `data/forget.key`) and the JSONL call log (`logs/calls.jsonl`). Tables:
  `messages` + `messages_fts`, `facts`, `summaries`, `doc_notes`, `llm_calls`, `send_log`,
  `memory_cursor`, `forgotten`, `settings`.

### Token budget

- `MAX_INPUT_TOKENS` (default 8000) is the hard limit per model call; `TARGET_INPUT_TOKENS`
  (default 6500) is what context is trimmed to before a call. Config refuses to start if
  TARGET >= MAX.
- Estimator (`llm.estimate_tokens`): letters and spaces at 3.5 chars/token, digits and punctuation
  at 1.4 chars/token, plus 4 tokens per message, plus the tool schemas. It deliberately over-counts
  numeric text. OpenRouter has no token-count endpoint, so the provider's `usage.prompt_tokens` is
  the truth.
- `trim_to_budget`: keeps the system message; first shortens old messages to 300 chars (marked,
  pointing to `search_history`), then drops the oldest, then shortens the newest tool result, and
  as a last resort cuts the current user message with a visible note. When anything is cut, a
  context note tells the model to use `search_history`.
- `llm.chat` refuses to send above TARGET; after the call, `prompt_tokens >= MAX_INPUT_TOKENS`
  raises `BudgetExceeded` and `core` retries once with a 30% tighter target (never resending an
  identical payload). A zero or missing `prompt_tokens` counts as missing and the estimate is logged.
- Every call (chat, extract, gate, summary, doc_note, forget_resolve, forget_find, LinkedIn) writes
  one row to table `llm_calls` and one line to `logs/calls.jsonl`: purpose, model, thread, estimate,
  `prompt_tokens`, `completion_tokens`, `usage_missing`, `over_limit`. Metadata only, never message
  text or the key.
- Only always-on tools plus the tools of capabilities loaded in the current `send` go into a call,
  so tool schemas do not grow with every new capability.

## Capabilities

The system prompt carries a short index, one line per capability. The model calls
`load_capability(name)` to get the full instructions; only after that, within the same `send`, are
that capability's tools exposed. A call to a capability tool before loading returns an error that
tells the model to load it first.

| Capability | Tools | Notes |
| --- | --- | --- |
| `docs` | `list_docs`, `fetch_doc(doc_id, page)` | Seeded `docs/*.md` plus the user's own Slack uploads. One bounded page per call (~2800 estimated tokens, max 10,000 chars). |
| `messaging` | `send_message(to, text)` | Logs to table `send_log` only; nothing is delivered. Refuses text or recipient containing a forgotten value. |
| `linkedin` | `write_linkedin_post`, `validate_linkedin_post` | Deterministic checker: max 1300 chars, max 3 hashtags all at the end, no links (http(s), www., markdown links, bare domains). The drafting loop regenerates up to 3 times after the first draft and returns only a passing post, or an error. With `source_doc_id` the drafter also gets the doc's key facts (relevant ones first, every page covered); a doc with no notes is noted first, and no facts means an error instead of a post. Nothing is posted: see the draft link below. |

**LinkedIn draft link.** When a turn produced a post that passed the checker, `core.send` appends
one line to the reply: `Open in LinkedIn (draft): <url>`. The code builds the URL from the validated
post (`https://www.linkedin.com/feed/?shareActive=true&text=<percent-encoded post>`), never the
model; a forgotten value in the post is redacted first. The line is added after the reply is
stored, so it is not in `messages` or `/history` (a reloaded page does not show it). Opening it
shows LinkedIn's composer with the post as a draft, if you are logged in; nothing is posted. The
web page shows it as a link that opens a LinkedIn tab and copies the post to the clipboard; Slack
shows a clickable link. Only that last line counts: draft lines the model writes itself are
removed, and no link is added unless the reply shows the validated post. If the URL would exceed 2000 chars the line says to copy the text above
instead. LinkedIn's `text` parameter is not documented and may be ignored: then paste the post
from the clipboard (web) or copy it from the reply.

Memory tools are always on, not part of a capability: `search_history`, `search_doc_notes`,
`summarize_doc`, `get_fact_history`, `forget` (`chatbot/memory/tools.py`).

### How to add a capability

Add one file in `chatbot/capabilities/` that exports a `CAPABILITY` dict. That dict is the
registry entry: `chatbot/capabilities/__init__.py` discovers every module in the package with
`pkgutil` (files starting with `_` are skipped), so no other file changes. Names must be unique.

```python
# chatbot/capabilities/clock.py
from datetime import UTC, datetime


def current_time(user_id: str, thread_id: str, tz: str = "UTC") -> str:
    # fn(user_id, thread_id, **args) -> str; exceptions are returned to the model as "error: ..."
    if tz != "UTC":
        return "error: only UTC is supported"
    return datetime.now(UTC).isoformat(timespec="seconds")


CAPABILITY = {
    "name": "clock",  # load_capability("clock")
    "one_line": "tell the current date and time",  # always in the system prompt: keep it short
    "instructions": "Call current_time when asked for the date or time. Answer in one line.",
    "tools": [
        (
            {
                "name": "current_time",
                "description": "Current date and time (ISO 8601).",
                "parameters": {
                    "type": "object",
                    "properties": {"tz": {"type": "string", "description": "Only 'UTC'."}},
                    "required": [],
                },
            },
            current_time,
        ),
    ],
}
```

Check it: `.venv/bin/python -c "from chatbot import capabilities; print(capabilities.capability_index())"`
shows the new line, and `make test` still passes. Tool results longer than 12,000 chars are
truncated, so return bounded output (see `docs.fetch_doc` for paging).

## Memory model

Code: `chatbot/memory/`. After each assistant reply, `after_turn` runs fact extraction and the
summary roll; a memory failure is logged and never breaks the reply.

- **Facts** (`facts.py`, table `facts`): key/value with scope `global` (all threads), `session`
  (this thread only) or `ephemeral` (one draft or task; never stored). A new value for the same key
  supersedes the old row (`superseded_by`, `valid_to`), so `get_fact_history` can answer "what did
  I say before". Unknown scopes fall back to `session`, never global.
- **Extraction** (`extract.py`): one `JUDGE_MODEL` call per user turn, on live chat and on the
  history loader alike. The prompt holds generic principles: skip quoted or forwarded text, store
  only durable and explicit statements, store a relation only when the user claims it, sensitive
  data only on request. **Correction guard:** replacing a different active value needs the
  extractor's `"correction": true`; otherwise the old fact is kept and the overwrite is logged as
  rejected.
- **Salience gate** (`gate.py`), on by default: before extraction, one question to an OpenRouter
  decisions model (`GATE_MODEL`, default `typesafe/jev-1.14`, endpoint `/api/alpha/decisions`)
  scores "is anything here worth long-term memory?" (0..1). Below `GATE_THRESHOLD` (default 0.3)
  the extractor call is skipped. Fail-open: any error, timeout or redirect runs the extractor.
  `GATE_MODEL=` (empty) turns it off. Gate calls are budget-checked and logged as purpose `gate`.
- **JUDGE_MODEL**: model for the JSON decision calls only (fact extraction, forget resolver).
  Unset = `LLM_MODEL`. Must support chat/completions with JSON output, so decisions-only models such
  as `typesafe/jev-*` do not work here.
- **Summaries** (`summary.py`, table `summaries`): turns that leave the 20-message window are folded
  into a rolling per-thread summary, in batches of at least 10 turns.
- **History search** (`retrieval.py`): `search_history` runs FTS5 (bm25) over this thread's earlier
  messages only, never other threads.
- **Doc notes** (`doc_notes.py`, table `doc_notes`): `summarize_doc` reads a document page by page
  (one bounded call per page, cached per page), stores a summary and key facts (dates, owners) per
  page and returns an overview. Uncached pages are noted in parallel by up to `DOC_NOTE_WORKERS`
  threads (default 4; `1` = one after another), each call capped at `DOC_NOTE_MAX_TOKENS`
  completion tokens (default 1000). If a page fails, the other pages are still saved and the error
  is raised. `search_doc_notes` answers later questions from the notes.
- **The `<memory>` block**: current global facts, this thread's session facts, docs whose notes
  were written in this thread (doc tools and `search_doc_notes` still see all of the user's docs and uploads) and the
  summary tail go into the system message, capped at ~900 tokens and fenced as data, not
  instructions.
- **Forget** (`forget.py`, tool `forget`): runs only when the user's typed message asks to forget,
  delete, erase, remove, wipe or purge AND names the subject's content words in order (not on "don't
  forget" or "do not ever delete", not on corrections). It deletes matching fact rows, redacts the value in messages
  (+ FTS), summaries, doc notes, `send_log` and uploaded files (renaming uploads whose name holds it),
  runs `VACUUM` + WAL checkpoint, then `verify_clean` scans all tables, FTS vocabulary and upload
  files and reports "verified clean" or the traces left. It stores an HMAC tombstone (key
  `FORGET_SECRET`, else a generated `forget.key` next to the DB), never plaintext; every later reply
  and `send_message` call is checked against tombstones. Matching is on normalized tokens (NFKC,
  casefold, street abbreviations, joined digit runs). Generic values (a year, a house number, a
  short single word) are not tombstoned; only their fact rows are deleted.

## Slack

Socket Mode (outbound websocket, no public URL). Setup steps, scopes and troubleshooting:
[SLACK_SETUP.md](SLACK_SETUP.md).

- DMs from `SLACK_USER_ID` go to `core.send` on the owner's current Slack thread (`slack-dm` at
  first), as the same user as the web page (`user-1`). Anyone else gets a refusal.
- `/bot new` starts a fresh session: later DMs, `/bot` and @mentions go to the next thread
  (`slack-dm-2`, `slack-dm-3`, ...). Global facts carry over; the old thread keeps its history,
  summary, session facts and doc listing. The counter lives in table `settings`, so a restart keeps
  it. No model call.
- `/bot <text>` and `@<bot> <text>` in a channel: same thread, replies are ephemeral (only the owner
  sees them).
- Files in the DM: `.txt`, `.md`, `.csv`, `.json` and `.pdf` (text via `pypdf`) are stored as
  extracted text under `data/uploads/<user>/` and become docs for `fetch_doc` / `summarize_doc`.
  A CSV (or other delimited table) split over several pages repeats its header row on each page.
  Downloads only from Slack hosts with the bot token. Needs the `files:read` scope.
- Acks: once an event passes dedupe and the owner check, the bot posts
  `:hourglass_flowing_sand: Working on it…` (or `Reading your file…` for a file share) before the
  answer, so a slow turn does not look dead. No model call, not stored in `messages`; for `/bot`
  and @mentions the ack is ephemeral too. Refused users and Slack retries get no ack.
- `make run` starts the bot inside the web process when `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are
  set; `make slack` runs it alone.

## Scenarios

`scenarios/run.py` runs S1-S7, each in a child process against a fresh temp DB (the repo's `data/`
and `logs/` are never touched), with deterministic assertions and no LLM judge. Every scenario also
fails if any call reached `MAX_INPUT_TOKENS` or lacked provider usage.

```sh
make scenarios-dry               # fake model, gate off: no network, no credit (plumbing check)
make scenarios                   # all 7, live model (spends OpenRouter credit)
make scenarios S=1,4             # a subset (`make scenario` is an alias)
```

S1 manager fact across sessions (and supersede), S2 recall from a 500-turn history, S3 summarize
the Q3-plan doc and give the launch date among distractor dates, S4 British then American spelling,
S5 "for this draft only" stays out of other sessions, S6 forget the address everywhere, S7 LinkedIn
post from Q3-plan that passes the rules.

**Cost:** a live run loads the 500-turn history once (one extract call per user turn, minus turns
the gate skips, plus summary rolls), plus each scenario's own calls. Measured on the 500-turn load:
the gate skips 84% of turns, and the load costs about $0.07 with the gate vs about $0.24 without it
(`openai/gpt-4.1-mini`). The cost of a full run is not measured; the `calls=` column shows the real
count. History and docs are synthetic and seeded: `make gen-history`
(`scripts/gen_history.py` -> `scenarios/data/history_500.json` + `expected.json`) and `make gen-docs`
(`scripts/gen_docs.py` -> `docs/*.md` + `docs/_manifest.json`). Details:
[scenarios/README.md](scenarios/README.md).

## Tests and checks

```sh
make test                 # offline unit + integration tests (fake model, no network)
make test-live            # RUN_LIVE_TESTS=1: extraction quality, gate quality, live flows,
                          # docs/upload (spends credit; needs OPENROUTER_API_KEY)
make lint                 # ruff format --check + ruff check
make fmt                  # format and auto-fix
make check                # lint + offline tests (what the hooks run)
make hooks                # use .githooks: secret scan, ruff, tests, Conventional Commits
```

Offline test modules set `GATE_MODEL=""` before importing `chatbot` so they never call the network;
a new offline test module must do the same.

## Configuration

Copy `.env.example` to `.env`. Real environment variables win over `.env`.

| Variable | Default | Secret | Purpose |
| --- | --- | --- | --- |
| `OPENROUTER_API_KEY` | (none, required) | yes | OpenRouter key for chat, judge and gate calls |
| `LLM_MODEL` | `openai/gpt-4.1-mini` | no | Chat model; must support tool calling and return usage |
| `JUDGE_MODEL` | `LLM_MODEL` | no | JSON decision calls (extraction, forget resolver) |
| `GATE_MODEL` | `typesafe/jev-1.14` | no | Salience gate model; empty = gate off |
| `GATE_THRESHOLD` | `0.3` | no | Gate probability below which extraction is skipped |
| `MAX_INPUT_TOKENS` | `8000` | no | Hard input limit per call (provider usage) |
| `TARGET_INPUT_TOKENS` | `6500` | no | Trim target; must be below MAX |
| `FORGET_SECRET` | (generated `forget.key` next to the DB) | yes | HMAC key for forget tombstones; keep it stable |
| `DB_PATH` | `./data/chatbot.db` | no | SQLite file; uploads and `forget.key` live next to it |
| `CALL_LOG_PATH` | `./logs/calls.jsonl` | no | Per-call usage log |
| `HOST` | `127.0.0.1` | no | Bind address; non-loopback requires `WEB_TOKEN` |
| `PORT` | `8000` (`make run`: 8765) | no | HTTP port |
| `WEB_TOKEN` | (empty = no auth) | yes | Shared secret for the web page and API |
| `ALLOWED_HOSTS` | (empty) | no | Extra accepted `Host` values, comma separated |
| `SLACK_BOT_TOKEN` | (empty) | yes | `xoxb-` bot token; Slack starts only with both tokens |
| `SLACK_APP_TOKEN` | (empty) | yes | `xapp-` Socket Mode token |
| `SLACK_USER_ID` | (empty) | no | Owner's Slack member ID; everyone else is refused |
| `MAX_FILE_BYTES` | `0` (unlimited) | no | Largest Slack file the bot downloads |
| `DOC_NOTE_WORKERS` | `4` | no | Parallel page-note calls in `summarize_doc`; `1` = sequential |
| `DOC_NOTE_MAX_TOKENS` | `1000` | no | Completion token cap per page-note call |
| `RUN_LIVE_TESTS` | (unset) | no | `1` enables the live test suites (not in `.env.example`) |

## Deploy

Deployment is out of scope for this project: it runs locally (`make run`). `/healthz`,
`WEB_TOKEN`, the `Host`/`Origin` checks and `X-Frame-Options: DENY` are app hardening for the
local server, not a deployment setup.

## Known limits

- **No upload size cap by default.** `MAX_FILE_BYTES=0` means unlimited; a big file means memory
  use and many `summarize_doc` calls. Slack's own limit is the only ceiling unless you set it.
- **English-only forget cue.** The forget guard looks for English verbs (forget, delete, erase,
  remove, wipe, purge); a request in another language is refused. A negation split off by a comma
  ("Do NOT, under any circumstances, delete ...") is not seen by the guard.
- **Global tombstones.** Tombstones are not per user: a value forgotten by one user is redacted for
  every user. Fine for the single-owner use this is built for.
- **Estimator undercounts CJK.** The two-class estimator treats CJK characters as letters
  (3.5 chars/token), which undercounts them. The provider-usage check and retry still enforce MAX.
- **`summarize_doc` is slow on long files.** One call per page. Sequentially an 18-page numeric CSV
  took about 130-165 s in live tests; pages now run 4 at a time (`DOC_NOTE_WORKERS`), live timing
  not re-measured.
- **Extraction flakes.** On `openai/gpt-4.1-mini` the held-out extraction table scores precision
  0.86 (recall 1.00): a temporary arrangement ("while X is on holiday, I ...") is sometimes stored
  as a session fact. A few other rows flake between runs (a fact value without the person's name,
  a fact not stored).
- **Doc listing gap on a cache hit.** The memory block lists docs whose notes were written in this
  thread. A doc already summarized in another thread is fully cached, so summarizing it again
  writes no rows here and it is not listed in this thread; `search_doc_notes` still finds it.
- **Slack live path not tested automatically.** Slack handlers are tested offline with fakes; the
  real Socket Mode round-trip (DM, `/bot`, @mention, files) is only checked by hand.

Full list: [Known limits](https://github.com/marcotgs/simple-chatbot/wiki/Known-limits).
