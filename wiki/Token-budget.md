# Token budget

Every model call must stay under `MAX_INPUT_TOKENS` input tokens, measured by the provider's
`usage.prompt_tokens`. OpenRouter has no token-count endpoint and tokenizers differ per model, so
the app estimates before the call, trims to a lower target, and checks the real number after.

| Env var | Default | Meaning |
| --- | --- | --- |
| `MAX_INPUT_TOKENS` | 8000 | Hard limit. A call with `prompt_tokens >= MAX` is a violation |
| `TARGET_INPUT_TOKENS` | 6500 | Context is trimmed to this estimate before each call. Must be below `MAX` (config raises at import otherwise) |

## The estimator

`llm.estimate_tokens` ([`chatbot/llm.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/llm.py))
counts two character classes over message contents, tool calls and the tool schemas:

| Class | Chars per token |
| --- | --- |
| Letters and whitespace (`CHARS_PER_TOKEN`) | 3.5 |
| Digits and punctuation (`DENSE_CHARS_PER_TOKEN`) | 1.4 |

Plus 4 tokens per message (`PER_MESSAGE_OVERHEAD`). Prose runs about 4 chars per token, so the
estimate over-counts on purpose. Numeric CSV measured 1.77 chars per token on gpt-4.1-mini, and
the estimate puts it at about 1.67. Document pages are sized by the same estimator
(`docs.PAGE_TOKENS = 2800`, hard cap `PAGE_CHARS = 10000`). CJK text is under-counted (see
[[Known limits|Known-limits]]).

## Guard in `llm.chat`

1. **Before sending**: estimate > `TARGET_INPUT_TOKENS` raises `BudgetExceeded`, and nothing is
   sent.
2. **After the call**: `prompt_tokens >= MAX_INPUT_TOKENS` logs the row with `over_limit = 1`,
   prints `!!! BUDGET VIOLATION` to stderr and raises `BudgetExceeded`.
3. **Missing usage**: when the provider returns no `prompt_tokens`, or 0 (an error path such as a
   content filter), the estimate is logged in its place with `usage_missing = 1`.

`core._chat_trimmed` trims to `TARGET` and calls. On `BudgetExceeded` it retries once with a 30%
tighter target (`target * 0.7`). If trimming produced the same payload as before, it does not
resend it and raises. `core.send` then deletes the user message and re-raises (HTTP 413, or a short
Slack reply).

## `trim_to_budget` phases

[`core.trim_to_budget`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/core.py)
always keeps `messages[0]` (the system block with prompt, capability index and memory). That is why
every system text is merged into that one message.

1. **Fits**: estimate <= target, so nothing changes.
2. **Note**: when anything is cut, `TRIM_NOTE` is added once to the system block. It tells the model
   that earlier messages were shortened or left out and to call `search_history`.
3. **Shorten, oldest first**: messages older than the current user message, and tool results, are
   cut to 300 chars (`SHORTENED_CHARS`). Old chat messages get
   `[...shortened; search_history has the full text]`. Tool results get
   `[...shortened; call the tool again for the full result]`, because tool results are never stored.
   The current user text and the newest message are not touched in this phase. The summary only
   covers turns older than the 20-message window, so shortening comes before dropping.
4. **Drop, oldest first**: whole messages are dropped. The current user message is never dropped,
   and a tool result is never left first without its tool call.
5. **Shorten the newest tool result**: only when still over target after dropping, so a fetched
   document page normally arrives whole.
6. **Cut the current message**: when the user's own message is over budget (a pasted table), a
   binary search keeps the longest prefix that fits and appends `CUT_MARK`. That mark tells the
   model the rest was not read and to ask for smaller parts. If even an empty prefix cannot fit,
   the message stays whole and the call fails cleanly.

Other fixed caps keep tool output small: a tool result is truncated at 12000 chars
(`MAX_TOOL_RESULT_CHARS`), `search_history` at 2000, `search_doc_notes` at 2500, a document
overview at 6000, and the memory block at 3150 chars. Completions are capped only for
`doc_note` calls: `DOC_NOTE_MAX_TOKENS` (default 500) output tokens per page.

## The call log

Every completed call, including the salience gate's HTTP call, writes one row to table `llm_calls` and one
JSON line to `logs/calls.jsonl` (`CALL_LOG_PATH`). The log holds metadata only, never message
bodies or keys. A call that fails (an exception, a gate HTTP error or timeout) or is refused before
sending (`BudgetExceeded` on the estimate) writes no row.

| Field | Meaning |
| --- | --- |
| `ts`, `purpose`, `model`, `thread_id` | When, why, which model, which chat |
| `estimated_tokens` | Pre-call estimate |
| `prompt_tokens` | Provider-reported input tokens (the estimate when `usage_missing`) |
| `completion_tokens` | Output tokens, if reported |
| `usage_missing` | 1 = the provider sent no usage; `prompt_tokens` is the estimate |
| `over_limit` | 1 = `prompt_tokens >= MAX_INPUT_TOKENS` (a violation) |

Purposes: `chat`, `extract`, `gate`, `summary`, `doc_note`, `forget_resolve`, `forget_find`,
`linkedin`.

### Reading it

```sh
# Largest real prompts and any violations
sqlite3 -readonly data/chatbot.db \
  "SELECT purpose, COUNT(*), MAX(prompt_tokens), SUM(over_limit), SUM(usage_missing)
   FROM llm_calls GROUP BY purpose ORDER BY 3 DESC;"

# How far the estimate is from the provider's number (ratio > 1 = estimate over-counts)
sqlite3 -readonly data/chatbot.db \
  "SELECT purpose, ROUND(AVG(1.0 * estimated_tokens / prompt_tokens), 2)
   FROM llm_calls WHERE usage_missing = 0 GROUP BY purpose;"

# Last calls from the JSON log
tail -n 20 logs/calls.jsonl
```

What to look for: `MAX(prompt_tokens)` stays below `MAX_INPUT_TOKENS`, `over_limit` is always 0,
and `usage_missing` is 0 (otherwise that model does not report usage and the check falls back to
the estimate). The scenario runner fails a scenario on any of these (see [[Scenarios]]).
