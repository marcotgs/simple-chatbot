# Scenarios

Seven end-to-end scenarios check the behaviour the brief asks for, against the real model, with
deterministic assertions only (no LLM judge). Runner:
[`scenarios/run.py`](https://github.com/marcotgs/simple-chatbot/blob/main/scenarios/run.py),
checks: [`scenarios/checks.py`](https://github.com/marcotgs/simple-chatbot/blob/main/scenarios/checks.py),
details: [`scenarios/README.md`](https://github.com/marcotgs/simple-chatbot/blob/main/scenarios/README.md).

## S1-S7

| ID | Name | What it does | Passes when |
| --- | --- | --- | --- |
| S1 | I just told you | Opens session B, states the manager in session A (first rephrased, then "My manager is ..."), asks in B and in `slack-dm` | B and `slack-dm` name the latest manager |
| S2 | Long thread | Loads the 500-turn history, asks for the cat (turn 20) and the favourite restaurant (turn 22) | Both names are in the replies |
| S3 | Big document | "Summarize doc Q3-plan.", 100 filler turns, then "What was the launch date in that plan?" | The launch date from `docs/_manifest.json` is stated and no distractor date is |
| S4 | Changed mind | "Always use British spelling.", filler pushes it out of the window, "Switch to American spelling.", more filler, drafts in new and old threads, then "What did I say about spelling before?" | Drafts use only the current spelling; the history answer names British and American |
| S5 | Stays local | "For this draft only, make it playful." in session A, the same request in session B | B shows no playful signals and no global fact mentions playful |
| S6 | Forget | Loads history (address planted at turn 8), "Forget my address.", "Message my partner where I live." | The address and its variants appear nowhere: replies, `send_log`, every table column incl. FTS shadow tables, FTS vocabulary, raw `.db`/`-wal`/`-shm` bytes, call log |
| S7 | LinkedIn post | Loads history, asks for a LinkedIn post about the Q3 launch from doc Q3-plan | A real post of at least 200 chars with the launch date passes the independent rule checker [`scenarios/linkedin_rules.py`](https://github.com/marcotgs/simple-chatbot/blob/main/scenarios/linkedin_rules.py) |

Every scenario also fails if any of its `llm_calls` rows has `prompt_tokens >= MAX_INPUT_TOKENS`,
`over_limit`, or missing provider usage, or if it made no model calls at all.

## How a run works

- Each scenario runs in its own child process with `DB_PATH` and `CALL_LOG_PATH` pointing at a
  fresh temp dir, so the repo's `data/` and `logs/` are never touched.
- Live steps call `core.send` as user `scenario-user`: thread `slack-dm` for the Slack DM, a new
  UUID per web session.
- History and filler turns go through `core.ingest_turn`, the same storage and memory path as live
  messages, with canned assistant replies (no reply generation).
- The 500-turn history is loaded **once** per run into a snapshot dir (the `Ssnapshot` line). S2, S6,
  S7 and `--dry` start from a copy of it. Each scenario's budget line counts only its own calls.

## History generator

[`scripts/gen_history.py`](https://github.com/marcotgs/simple-chatbot/blob/main/scripts/gen_history.py)
writes `scenarios/data/history_500.json` and `expected.json`, seeded (seed 1337), with no network
calls. The runner regenerates both on every run. Planted, obviously fake details: address at turn
8, partner at turn 12, cat at turn 20, restaurant at turn 22. Manager and spelling facts come only
from live steps. `make gen-history` regenerates them by hand.

The seeded documents (Q3 plan with distractor dates, engineering handbook, customer research) come
from [`scripts/gen_docs.py`](https://github.com/marcotgs/simple-chatbot/blob/main/scripts/gen_docs.py)
(`make gen-docs`), with the asserted facts in `docs/_manifest.json`.

## Running

| Command | What it does | Cost |
| --- | --- | --- |
| `make scenarios-dry` (`python -m scenarios.run --dry`) | Generates the history, loads it with a fake model, checks plumbing: determinism, one extract call per user turn, FTS hit, leak-scan positive controls, call logging | Free, no network: the runner turns the salience gate off (`GATE_MODEL=""`) for `--dry` |
| `make scenarios` (alias `make scenario`) | All 7, live model | Real OpenRouter credit |
| `make scenarios S=1,4` | A subset (`--only 1,4`) | Less |
| `python -m scenarios.run --keep-db` | Keeps each temp dir (`chatbot.db`, `calls.jsonl`, `transcript.json`) for inspection | Same as live |

Needs `OPENROUTER_API_KEY` (from `.env` or the environment). `LLM_MODEL` and `MAX_INPUT_TOKENS` are
honoured.

The fake model in `--dry` replaces only the chat client; the salience gate has its own HTTP
client, so the runner sets `GATE_MODEL=""` in each dry child process.

Cost: [`scenarios/README.md`](https://github.com/marcotgs/simple-chatbot/blob/main/scenarios/README.md)
report a measured 500-turn history load: about $0.24 without the salience gate and $0.07 with it
(84% of turns skipped). The cost of a full run is not measured. The `calls=` column shows the real
count of each run.

## Reading the output

Conversation progress goes to stderr. Stdout has one line per scenario, then a summary. A dry
run (`make scenarios-dry`, about one second) prints:

```
PASS Ssnapshot History snapshot (500 turns, loaded once) - ok [calls=549 max_prompt_tokens=1427] | notes: load calls by purpose: extract=500, summary=49
PASS Sdry Dry plumbing (no LLM) - ok [calls=2 max_prompt_tokens=2007] | notes: registered tools: [...]
SUMMARY 2/2 passed; calls=551; max prompt_tokens=2007 (limit 8000)
```

A live run has the same shape (the counts below are placeholders):

```
PASS S1 I just told you - ok [calls=.. max_prompt_tokens=..]
FAIL S4 Changed mind - new-session draft: 1 non-american form(s) (...) [calls=.. max_prompt_tokens=..] | notes: ...
SUMMARY 6/8 passed; calls=..; max prompt_tokens=.. (limit 8000)
```

- `PASS|FAIL S<n> <name> - <reason>`: `ok`, or every failed assertion joined with `;`.
- `calls` / `max_prompt_tokens`: this scenario's model calls and its largest provider-reported
  prompt. `max_prompt_tokens` must stay below the limit.
- `| notes:` carries soft signals that do not fail the run, e.g. S5 "weak proxy" or S7 "checker
  disagreement" between the scenario checker and the app's own checker.
- The `Ssnapshot` line counts in the summary total; its notes list the load calls by purpose.
- With `--keep-db`, the notes end with `kept <temp dir>`.
- Exit code is 1 if any scenario fails.

To dig into a failure, rerun it alone with `--only <n> --keep-db` and query the kept
`chatbot.db` (see [[Token budget|Token-budget]] for `llm_calls` queries).
