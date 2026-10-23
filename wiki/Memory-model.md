# Memory model

Code: [`chatbot/memory/`](https://github.com/marcotgs/simple-chatbot/tree/main/chatbot/memory).
Every memory side call carries its own `purpose` (`gate`, `extract`, `summary`, `doc_note`,
`forget_resolve`, `forget_find`) and is budget-checked and logged like a chat call. All of them go
through `llm.chat` except the gate, which makes its own HTTP call and mirrors the same checks
(`llm._record`).

## What the model sees

`memory.memory_text()` ([`memory/__init__.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/__init__.py))
builds one block, capped at `CONTEXT_MAX_CHARS = 3150` (about 900 tokens), fenced as
`<memory ...>...</memory>` and marked as user data, not instructions:

1. Global facts (up to 1400 chars), then this chat's session facts (up to 466 chars). When the cap
   bites, preference-like keys (`pref*`, `style`, `tone`, `spelling`, `language`, `format`, ...)
   win, then the oldest keys. Keys that did not fit are named, with a pointer to
   `get_fact_history`.
2. "Documents with saved notes": docs whose notes were written in **this** thread (up to 10,
   newest first). Docs noted in other threads are not listed here, even after a cached
   `summarize_doc` call in this thread (it writes no new rows), but `search_doc_notes`, `list_docs` and
   `fetch_doc` stay user-scoped, and uploads belong to the user, not the thread.
3. The tail of this chat's rolling summary, if room is left.

## Fact scopes

Table `facts`, code [`facts.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/facts.py).

| Scope | Stored as | Visible in |
| --- | --- | --- |
| `global` | `thread_id` NULL | every thread of the user |
| `session` | `thread_id` set | only that thread |
| `ephemeral` | never stored | the current answer only |

The extractor picks `session` only when the user says the instruction is for this chat; an
unknown scope from the model falls back to `session`, never `global`.

## Extraction after each turn

`core.ingest_turn(assistant, ...)` calls `memory.after_turn`, which runs
[`extract.maybe_extract`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/extract.py)
and then the summary roll. The live chat and the history loader share this path: one extraction
pass per user turn.

1. `memory_cursor` holds the last extracted `turn_no` per thread. The extractor reads the user
   messages after the cursor (normally just this turn). Re-runs are idempotent. When a call fails,
   the cursor stays and the next turn catches up (chunks of up to 9000 chars).
2. **Salience gate** ([`gate.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/gate.py)):
   one yes/no question to an OpenRouter decisions model (`GATE_MODEL`, default
   `typesafe/jev-1.14`) at `https://openrouter.ai/api/alpha/decisions`. A probability below
   `GATE_THRESHOLD` (default 0.3) skips the extractor. Any error, timeout (10 s) or odd reply runs
   the extractor anyway (fail-open). `GATE_MODEL=` (empty) turns the gate off.
3. **Extractor**: one JSON call on `JUDGE_MODEL` (defaults to `LLM_MODEL`). The prompt
   (`extract._PROMPT`) holds generic principles only: ignore quoted or forwarded text, store only
   durable explicit facts, decide whose fact it is, scope rules, sensitive data only on request.
   It gets `KNOWN FACTS` (related values plus every active key) so a rephrased change reuses the
   same key.
4. Each returned item is normalised (`snake_case` key, value up to 500 chars). `ephemeral` items
   are dropped, `retract` ends the active fact (`valid_to`), and values that match a forgotten value
   are skipped.

## Supersession and the correction guard

`facts.upsert_fact` (under `BEGIN IMMEDIATE`, so two threads cannot both supersede):

- Same key + scope (+ thread for session) with the same value (case-insensitive): no-op.
- New value **and** the extractor set `"correction": true`: a new row is inserted, and the old row
  keeps its value and gets `superseded_by` + `valid_to`. "What did I say before?" is answered from
  this chain by the `get_fact_history` tool (status `current` / `superseded` / `retracted`).
- New value **without** `correction: true`: the old fact is kept and the new one is rejected
  (logged as "rejected overwrite"). This stops a mention of another person in a similar role from
  overwriting the user's own fact.

## Rolling summaries

[`summary.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/summary.py):
messages that left the 20-message recent window are folded into one summary row per thread, in
batches of at least 10 turns (`MIN_NEW_TURNS`) and chunks of about 12000 chars. The prompt keeps
names, numbers and dates with their turn numbers. The summary is capped at 2800 chars, and
forgotten values are redacted before it is stored.

## Retrieval

The `search_history` tool ([`retrieval.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/retrieval.py))
runs an FTS5 query over `messages_fts`, ranked by `bm25`. Every term is a double-quoted word
(operators are inert), stopwords are dropped, up to 16 terms are OR-joined. Results come from the
current thread only, exclude the current turn, return 1-10 snippets with turn numbers, capped at
2000 chars.

## Document notes

`summarize_doc(doc_id)` ([`doc_notes.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/doc_notes.py))
makes one `doc_note` call per page and stores a page summary (at most 60 words) plus up to 15
`key_fact` lines in `doc_notes`. Pages already noted for that user are skipped. Uncached pages run
in parallel on up to `DOC_NOTE_WORKERS` threads (default 4; `1` = one page after another); each
`doc_note` call is capped at `DOC_NOTE_MAX_TOKENS` completion tokens (default 1000). If one page
fails, the other pages are still saved and the first failure (in page order) is raised. The overview it returns is the stored
notes (page summaries + key facts, capped at 6000 chars); there is no extra reduce call.
`search_doc_notes` scores notes by query-word overlap (key facts first on ties). Re-uploading a
file under the same name deletes its old notes.

## Forget

The `forget` tool ([`tools.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/tools.py))
runs only when the latest user message (cut before any Slack file note) contains one of
`forget|delete|erase|remove|wipe|purge` (a negation up to 3 words before the verb, such as "don't forget", "do not ever delete" or "I
do not want you to delete", is removed first; a comma or period ends it) **and** the content words
of the subject, in order and adjacent ("home page address" does not name "home address"). Otherwise it returns `error: not run`. Corrections never call it.

[`forget.forget`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/forget.py) then:

1. **Resolves** what to delete: a `forget_resolve` call over the user's latest 150 facts (fact ids,
   literal values, search terms), plus FTS search of user messages and a `forget_find` call for the
   exact strings. All superseded versions of a matched key go too. Values must occur in stored
   data and be specific: a year, a house number or a short single word deletes only its fact rows
   and is never tombstoned.
2. **Purges** each value and its variants (full value, specific comma parts, street without house
   number). Matching uses normalised token sequences (NFKC + casefold, punctuation ignored, street
   abbreviations, joined digit runs), never SQL `LIKE`. Fact rows are deleted. `messages`,
   `summaries`, `doc_notes` and `send_log` are rewritten in place (summaries are not re-rolled),
   and the FTS index is rebuilt.
3. **Tombstones**: an HMAC-SHA256 digest per variant goes into `forgotten`. Key: `FORGET_SECRET`,
   else a random 32-byte `forget.key` (mode 0600) next to the DB.
4. **Uploads**: the user's uploaded files are scrubbed. A file whose name holds the value is
   renamed to `upload-forgotten-<random hex>` and its doc notes follow.
5. **Compacts** (`VACUUM`, WAL checkpoint) and runs **`verify_clean`**: it sweeps every table (FTS
   shadow tables included), the FTS term vocabulary, the raw `.db` and `-wal` bytes, the call log
   and every upload file name and body. The status line says "verified clean" or lists the traces.

Guards after a forget: every reply and assistant tool-call text is redacted (`filter_reply`),
`send_message` refuses text that holds a forgotten value, and the extractor, summaries and doc notes
skip or redact it. Tombstones are global for all users of the instance (see
[[Known limits|Known-limits]]).
