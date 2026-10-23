# Plan: simple chatbot (take-home) — status: APPROVED, executing W1

Repo: /Users/marcotgs/projects/simple-chatbot (empty, clean). Agents never commit; user commits (first commit = this plan, per brief).

## Stack decisions (ponytail ladder)
- Python 3.11+, stdlib `sqlite3` (+FTS5), stdlib `http.server` serving a bare web page (`web/index.html`, vanilla JS, no build) + JSON API (`POST /send`, curl-able). No CLI.
- LLM: OpenRouter via its official `openrouter` Python SDK (`server_url=https://openrouter.ai/api/v1`, one dep). Key in `.env` `OPENROUTER_API_KEY`, never committed. Model in env `LLM_MODEL` (must support tool calling). Budget read from `usage.prompt_tokens`. `slack_bolt` Socket Mode (no public URL needed). Nothing else.
- Token budget: `MAX_INPUT_TOKENS` env (default 8000). Preflight estimate (chars/3.5 + margin) trims context; post-call check of `usage.input_tokens`, log to `logs/calls.jsonl` + table `llm_calls`; over budget = raise + retry once with tighter trim.

## Design core
- `send(user_id, thread_id, text) -> reply` in `chatbot/core.py`. Single entry for CLI, HTTP, Slack, scenarios.
- SQLite tables: `messages` (+FTS5), `facts` (scope=global|session|ephemeral, `superseded_by`, `valid_to`), `summaries` (rolling per thread), `doc_notes` (chunk summaries + key-facts per doc/thread), `llm_calls`, `send_log`.
- Context per call = system prompt + capability INDEX (name + 1 line) + global facts + session facts + rolling summary + last N turns. Full capability instructions loaded on demand (tool `load_capability`) -> scales to ~20 more capabilities without blowing budget.
- Tools: `fetch_doc(doc_id, page)` returns bounded page (<=~4k tok); `search_history(query)` FTS5; `send_message(to,text)` logs; `linkedin_post` = capability with instructions + deterministic validator (<=1300 chars, <=3 hashtags all at end, no URLs) + regenerate loop.
- Big doc: map-reduce in-process: each page summarized (own call <8k), key facts (dates, owners) stored in `doc_notes`; later questions answered via `search_doc`/`search_history` over notes, not raw doc.
- Memory write path: after each user turn, small extractor call -> facts with scope + supersession. "Always British" then "Switch to American" => new fact supersedes, old kept as history (answers "what did I say before").  "This draft only" => ephemeral/session scope, never global.
- Forget: `forget(subject)` purges value across `facts`, `messages`(+FTS), `summaries` (re-summarize affected), `doc_notes`, `send_log`; then verification sweep (LIKE scan of all tables + files). Reply must not echo value; `send_message` guard blocks forgotten values (tombstone of hash only, not plaintext).
- Long history load: `ingest_turn()` shared by live path and loader (same storage, FTS, summary rollup, extraction); loader supplies canned assistant replies, skips reply generation.

## Acceptance criteria
- AC-1 `send()` callable over HTTP (curl) and from the bare web chat page; used by Slack app and scenarios.
- AC-2 `fetch_doc`, `send_message`(logs only), LinkedIn rules enforced; 3+ generated docs of 20-40k tokens incl. `Q3-plan` with launch date (+ distractor dates).
- AC-3 Every model call input tokens < limit (configurable), logged per call from provider `usage`.
- AC-4 State only in SQLite/files in repo dir.
- AC-5 Scenario script runs S1-S7, prints pass/fail.
- AC-6..12 = scenarios 1..7 as written in brief.
- AC-13 Adding capability = one file + registry entry; documented in README.
- AC-14 README.md, NOTES.md deliverables (see Q-3).

## Tasks
| ID | Agent | Files | Dep | DONE WHEN | AC | Status |
|---|---|---|---|---|---|---|
| T1 | mint | pyproject, `chatbot/{config,db,llm,core,server}.py`, `web/index.html` | — | `send()` works end-to-end with plain chat; budget guard + call log work; HTTP API + web page (new/parallel sessions) call it; unit check on budget trim | 1,3,4 | in progress |
| T2 | mint | `chatbot/capabilities/*`, `docs/`, `scripts/gen_docs.py` | T1 | 3+ docs 20-40k tok incl Q3-plan; tools registered; LinkedIn validator+retry; capability index/on-demand load | 2,13 | todo |
| T3 (incl. env `FACT_EXTRACT_BATCH`, default 20) | mint | `chatbot/memory/*` (facts, summary, retrieval, doc_notes, forget, ingest_turn) | T1 | scope/supersede/forget unit checks pass; rolling summary keeps calls < budget at 500 turns | 6,7,8,9,10 | todo |
| T4 | mint | `chatbot/slack_app.py`, `.env.example` | T1 | DM event -> `send(user, "slack-dm", text)` -> reply; hard-coded user map | 1 | todo |
| T5 | tamara | `scenarios/*`, `scripts/gen_history.py` | T1 | synthetic 500-turn history (turn-20 detail, early address) + S1-S7 runner printing pass/fail w/ assertions (no LLM judge for post rules/address/budget) | 5,6-12 | todo |
| T6 | mint | wiring in `core.py`, prompts | T2,T3 | agent loop uses tools+memory; S1-S7 runnable | all | todo |
| T7 | peter x2 (general + `FOCUS: security`) | diff | T4,T6 | APPROVE; security focus: secrets/tokens, forget purge, SQL injection in FTS query, tool guard | — | todo |
| T8 | tamara | run S1-S7 + variations (rephrasings, other order, restart process) | T6 | B-n bugs triaged; token log shows max < limit | 3,5-12 | todo |
| T9 | mint | fixes for C/W/B ids; README.md; NOTES.md skeleton | T7,T8 | all findings answered; README covers core/Slack/scenarios/new capability | 13,14 | todo |

| T10 | — | removed: deployment out of scope | — | — | — | removed |
| T11 | — | removed: deployment out of scope | — | — | — | removed |

Waves: W1 T1 | W2 T2,T3,T4,T5 (parallel, disjoint files) | W3 T6 | W4 T7,T8 | W5 T9 -> peter re-review.

## Phase 3 — removed: deployment out of scope (user decision 2026-10-23; D-1..D-4, Q-6, Q-7 removed)
Open-ended cost: S1-S7 live runs cost real API tokens; 500-turn load with extraction on every turn is ~500 small calls.

## Questions / risks
- Q-1 RESOLVED: OpenRouter. Model choice open: default a cheap tool-calling model, user can override via `LLM_MODEL`.
- Q-2 RESOLVED: batches of 20, env `FACT_EXTRACT_BATCH` (default 20; 1 = every turn). Applies in `ingest_turn`/loader; documented in `.env.example` + README. Original note: History loader: extraction on all 500 synthetic user turns (cost, faithful) vs only on turns heuristically fact-bearing (cheap). Default: batched extraction every 20 turns through same function.
- Q-3 NOTES.md needs YOUR content: hours spent, 2 AI mistakes caught, 1 AI suggestion rejected. Only you can write truthfully; I keep a log of such moments during execution and hand it over.
- Q-4 Brief asks user to use both Claude Code and Codex, keep transcripts, record screen, create Slack app, first commit = plan. Those are yours; not delegable.
- Q-5 Token estimate heuristic may under-count; mitigated by margin + post-call retry. OpenRouter has no count endpoint and tokenizers differ per model: keep safety margin (target ~6.5k of 8k), rely on post-call `usage.prompt_tokens`; verify `usage` is actually returned for the chosen model.
- R-1 Forget leaves traces in FTS shadow tables/WAL; mint must `VACUUM` + check `-wal`.
- R-2 Variations by evaluators: harden against different phrasing (forget by other subject, other doc ids) — extractor/forget must be LLM-driven, not regex on exact phrases.

## Phase 4 — GitHub wiki documentation
GitHub wiki = separate git repo (`<repo>.wiki.git`). Agents cannot push (no git writes). Mint writes markdown pages to `wiki/` in the working tree; USER enables Wiki on GitHub, creates first page via UI, then clones `<repo>.wiki.git` and copies `wiki/*.md` in + pushes.
| ID | Agent | Files | Dep | DONE WHEN | Status |
|---|---|---|---|---|---|
| T12 | mint | `wiki/*.md` (Home, Architecture, Memory-model, Token-budget, Capabilities-guide, Scenarios, Slack-setup, Known-limits) | T9 | every page matches the code (no invented behaviour); facts/paths verified against repo; sidebar `_Sidebar.md`; README links wiki | todo |
| T13 | peter | `wiki/` | T12 | APPROVE: accurate vs code, no secrets/tokens/real IDs | todo |
Waves: W8 T12 | W9 T13.  NOTES.md stays in repo (yours to write), wiki does not replace it.
