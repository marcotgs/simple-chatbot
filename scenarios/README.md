# Scenarios S1-S7

```
python -m scenarios.run --dry       # fake model, gate off: no network, no credit; generates history, loads it, checks plumbing
python -m scenarios.run             # all 7, live model
python -m scenarios.run --only 1,4  # a subset
python -m scenarios.run --keep-db   # keep each temp dir (chatbot.db, calls.jsonl, transcript.json)
```

`--dry` fakes the chat model, and the runner sets `GATE_MODEL=""` in each dry child process so the
salience gate (its own HTTP client) stays off too. `make scenarios-dry` runs the same.

One line per scenario, `PASS|FAIL S<n> <name> - <reason> [calls=.. max_prompt_tokens=..]`, then a `SUMMARY`.
Exit code 1 if any scenario fails. Conversation progress goes to stderr.

## How it runs

- Each scenario runs in a child process with `DB_PATH` / `CALL_LOG_PATH` pointing at a fresh temp dir,
  set before `chatbot` is imported. The repo's `data/` and `logs/` are never touched.
- Live steps call `core.send(user, thread, text)` with one user `scenario-user`, thread `slack-dm`
  for the Slack DM and uuid threads for web sessions.
- History and the 100 filler turns of S3 go through `core.ingest_turn`, the same storage
  and memory path as live messages (one fact-extraction call per user turn); the canned assistant
  replies skip reply generation.
- The 500-turn history is loaded ONCE per run into a snapshot dir (`PASS Ssnapshot ...` line, with load calls by
  purpose); S2, S6, S7 and `--dry` each start from a copy of it (DB, WAL, call log, `forget.key`).
  Each scenario's budget line counts only its own calls, not the copied load calls.
- `scripts/gen_history.py` writes `scenarios/data/history_500.json` + `expected.json` (seeded, no network).
  The runner regenerates them on every run. Planted: address turn 8, partner turn 12, cat turn 20,
  restaurant turn 22. Manager and spelling facts only come from live steps.
- Assertions are deterministic, no LLM judge: substring checks, a date normaliser against
  `docs/_manifest.json`, British/American wordlist counts, a playful-signal proxy plus a `facts` query,
  leak scans (reply, `send_log`, every table column incl. FTS shadow tables, fts5vocab terms, raw
  `.db`/`-wal`/`-shm` bytes and the call log) and an independent LinkedIn checker
  (`scenarios/linkedin_rules.py`, authority over `chatbot.capabilities.linkedin`).
- Every scenario also fails if any `llm_calls` row has `prompt_tokens >= MAX_INPUT_TOKENS`, `over_limit`, or
  missing provider usage.

## Env and cost

Needs `OPENROUTER_API_KEY` (from `.env` or the environment); `LLM_MODEL` (default `openai/gpt-4.1-mini`)
and `MAX_INPUT_TOKENS` are honoured. Live runs spend real OpenRouter credit.
One history load per run: 500 salience-gate calls (one per user turn), an extract call for each turn
the gate passes, plus summary rollups. Measured on the 500-turn load with `GATE_THRESHOLD=0.3`: the
gate skipped 84% of turns (420 of 500), and the load cost about $0.07 with the gate vs about $0.24
without it. The rest of a full run (UNVERIFIED estimate): S3's 100 filler turns (gate plus extract
calls), 20 `send` calls of 1-6 model calls each plus their gate/extract calls, and the Q3-plan
map-reduce, all under 8k input tokens. The `calls=` column shows the real count.
