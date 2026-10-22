# Scenarios S1-S7

```
python -m scenarios.run --dry          # no LLM, no credit: generates history, loads it with a fake model, checks plumbing
python -m scenarios.run                # all 7, live model
python -m scenarios.run --only 1,4     # a subset
python -m scenarios.run --keep-db      # keep each temp dir (chatbot.db, calls.jsonl, transcript.json)
```

One line per scenario, `PASS|FAIL S<n> <name> - <reason> [calls=.. max_prompt_tokens=..]`, then a `SUMMARY`.
Exit code 1 if any scenario fails. Conversation progress goes to stderr.

## How it runs

- Each scenario runs in a child process with `DB_PATH` / `CALL_LOG_PATH` pointing at a fresh temp dir,
  set before `chatbot` is imported. The repo's `data/` and `logs/` are never touched.
- Live steps call `core.send(user, thread, text)` with one user `scenario-user`, thread `slack-dm`
  for the Slack DM and uuid threads for web sessions.
- History (S2, S6, S7) and the 100 filler turns of S3 go through `core.ingest_turn`, the same storage
  and memory path as live messages; the canned assistant replies skip reply generation.
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

Needs `OPENROUTER_API_KEY` (from `.env` or the environment); `LLM_MODEL`, `MAX_INPUT_TOKENS`,
`FACT_EXTRACT_BATCH` are honoured. Live runs spend real OpenRouter credit.
Rough estimate (UNVERIFIED until the first full run): 20 `send` calls, each 1-6 model calls plus memory
extraction; 3 history loads of 500 turns (about 25 extraction calls each at `FACT_EXTRACT_BATCH=20`,
plus summary rollups); the Q3-plan map-reduce. Around 150-300 calls, all under 8k input tokens:
below 1 USD on `openai/gpt-4o-mini`. The per-scenario `calls=` column shows the real count.
