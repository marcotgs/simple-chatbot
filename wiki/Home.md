# simple-chatbot

A personal assistant for one owner. One function, `send(user_id, thread_id, text) -> reply` in
[`chatbot/core.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/core.py), sits
behind a bare web chat page, a curl-able JSON API, a Slack bot and the scenario runner.

What it does:

- **Remembers across chats.** Facts and preferences are extracted after every turn and stored in
  SQLite with a scope (global or this chat only). A changed fact supersedes the old one, which is
  kept as history.
- **Keeps every model call under a hard input-token budget** (`MAX_INPUT_TOKENS`, default 8000),
  checked against the provider-reported `usage.prompt_tokens` and logged per call.
- **Reads long documents** page by page and stores per-page notes, so later questions do not
  re-read the document.
- **Forgets on request.** It deletes a value from every table, the FTS index, uploaded files and
  the database file itself, then checks that nothing is left.
- **Loads capabilities on demand** (documents, LinkedIn posts, messaging). Only a one-line index
  sits in the prompt.

Stack: Python 3.11+, stdlib `sqlite3` (FTS5) and `http.server`, the official `openrouter` Python
SDK, `slack_bolt` (Socket Mode), `pypdf` for PDF uploads. See
[`pyproject.toml`](https://github.com/marcotgs/simple-chatbot/blob/main/pyproject.toml).

## Quick start (local)

1. `make install`: creates `.venv` and installs the hashed dev lock plus the package.
2. `cp .env.example .env`, then set `OPENROUTER_API_KEY` (see
   [`.env.example`](https://github.com/marcotgs/simple-chatbot/blob/main/.env.example); every
   setting: [[Architecture#configuration|Architecture]]).
3. `make run`: starts the web server on port 8765. It also starts the Slack bot when
   `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are both set.
4. Open `http://127.0.0.1:8765/`, or call the API:
   ```sh
   curl -s http://127.0.0.1:8765/send -H 'Content-Type: application/json' \
     -d '{"thread_id":"t1","text":"hi"}'
   ```

Other targets (`make help` lists them all): `make test` (offline unit tests),
`make test-live` (live tests, spends credit), `make scenarios` (alias `make scenario`) /
`make scenarios-dry`,
`make lint`, `make check`, `make hooks`.

## Pages

| Page | What it covers |
| --- | --- |
| [[Architecture]] | Components, HTTP API, configuration, the `core.send` request flow and tool loop, SQLite tables, file layout |
| [[Memory model|Memory-model]] | Fact scopes, extraction and the salience gate, supersession, summaries, retrieval, doc notes, forget |
| [[Token budget|Token-budget]] | Estimator, `TARGET`/`MAX`, trimming phases, the `llm_calls` log and how to read it |
| [[Capabilities guide|Capabilities-guide]] | How `load_capability` gating works, built-ins, adding a capability step by step |
| [[Scenarios]] | S1-S7, dry vs live runs, the history generator, cost, reading the output |
| [[Slack setup|Slack-setup]] | Scopes, DMs, `/bot`, @mentions, file uploads, owner-only access |
| [[Known limits|Known-limits]] | Accepted risks and open issues |

Repo docs these pages condense:
[`SLACK_SETUP.md`](https://github.com/marcotgs/simple-chatbot/blob/main/SLACK_SETUP.md),
[`scenarios/README.md`](https://github.com/marcotgs/simple-chatbot/blob/main/scenarios/README.md).
When a page and the code disagree, the code wins. Deployment is out of scope for this project:
it runs locally.
