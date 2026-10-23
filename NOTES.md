# Notes

<!-- Skeleton. Replace every prompt below with your own words; delete the prompts and the
candidate lists when done. -->

## Time spent

<!-- Hours, by phase. Example rows only; edit or replace. -->

| Phase | Hours |
| --- | --- |
| Planning | |
| Core, memory, capabilities | |
| Slack | |
| Tests and scenarios | |
| Review and fix rounds | |
| Docs | |
| **Total** | |

## AI usage log

<!-- Which tools and models you used, for what, and roughly how much. Where transcripts and the
screen recording are. -->

- Tools:
- Models:
- How the work was split between them:

## Two mistakes the AI made that I caught

<!-- Pick two, or write your own. For each: what the AI did, how you noticed, what you changed. -->

1.
2.

Candidates (from agent session notes: verify, then rewrite in your own words). Each item is tagged
with its source and who caught it. "From session log, not in git" means the mistake itself is not
recoverable from git history; only the fix may be.

- The fact-extractor prompt was overfit: it contained a literal sentence from a real Slack
  conversation plus test fixture names. Rewritten as generic principles with placeholders
  (`chatbot/memory/extract.py`, `_PROMPT`). Caught by: user. Source: session log, not in git (the
  overfit prompt was never committed).
- The first default model, `openai/gpt-4o-mini`, returned 404 "0 endpoints" under the OpenRouter
  account's ZDR guardrail whenever tools were attached; the default moved to `openai/gpt-4.1-mini`
  (`chatbot/config.py`, `.env.example`). Caught by: live smoke run (provider error). Source: commit
  `5f08dc1` (old default), commit `8db7220` (new default).
- A tool wrapper passed `search_doc_notes` arguments in the wrong order
  (`chatbot/memory/tools.py`, `_search_doc_notes`). Caught by: mint self-correction. Source: session
  log, not in git (the first committed version, `97d9827`, already has the right order).
- `forget` left non-ASCII values (e.g. a German street name) and partial mentions in storage while
  the smoke test reported "verified clean"; matching moved to normalized tokens
  (`chatbot/memory/forget.py`). Caught by: peter review (round 1, general and security). Source:
  session log and review notes, not in git (the first committed `forget.py`, `97d9827`, already
  matches normalized tokens; more hardening in `8db7220`).
- CSRF / DNS rebinding on the loopback API; fixed with a Host allowlist, Origin check and JSON-only
  POST (`chatbot/server.py`). Caught by: automated security plugin. Source: commit `3d580fe` (fix).
- Jev was assumed usable as a JSON judge, but it is a decisions-only model (chat/completions returns
  400); it is used only as the salience gate (`chatbot/memory/gate.py`, `.env.example` JUDGE_MODEL
  note). Caught by: tamara live evaluation. Source: commit `8db7220` (gate).
- The gate was briefed with question type `box`, which is for image bounding boxes; corrected to
  `noul` (`chatbot/memory/gate.py`, `_QUESTION`). Caught by: mint self-correction while building
  the gate. Source: session log, not in git.
- The `forget` tool fired on correction turns ("it is X, not Y") and purged the old value, until a
  guard required an explicit forget request in the typed message (`chatbot/memory/tools.py`,
  `_forget`; `chatbot/core.py` SYSTEM_PROMPT). Caught by: tamara integration tests (8 of 10 live
  correction turns). Source: session log, not in git (the guard is in commit `8db7220`).

## One AI suggestion I rejected

<!-- What was suggested, why you said no, what you did instead. -->

Candidates:

- No separate LLM judge for "is this worth remembering": salience folded into the extractor prompt
  (later a separate salience gate was added after all, `chatbot/memory/gate.py`).
- Accepted-risk decisions, e.g. no upload size cap (`MAX_FILE_BYTES=0` default, `.env.example`).

## What I would do next

<!-- Short list, most important first. -->

-

## How to run

See [README.md](README.md#quick-start): `make install`, copy `.env.example` to `.env`, `make run`.
Tests: `make test`. Scenarios: `make scenarios-dry` (fake model, gate off: no network, no credit),
then `make scenarios`.
