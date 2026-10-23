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
   newest first). Notes and `search_doc_notes` are per thread; `list_docs`, `fetch_doc` and
   uploads stay per user, not per thread.
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
   It gets `KNOWN FACTS` so a rephrased change reuses the same key: the newest 40 keys with their
   current value cut to 40 chars, older keys without value, the whole block capped at 3500 chars.
   Forgotten values are redacted.
4. Each returned item is normalised (`snake_case` key, value up to 500 chars). `ephemeral` items
   are dropped, `retract` ends the active fact (`valid_to`), and values that match a forgotten value
   are skipped.

## Supersession and the correction guard

`facts.upsert_fact` (under `BEGIN IMMEDIATE`, so two threads cannot both supersede):

- Same key + scope (+ thread for session) with the same value (case-insensitive): no-op.
- The user's own fact restated with a new value: a new row is inserted, and the old row keeps its
  value and gets `superseded_by` + `valid_to`. "What did I say before?" is answered from this chain
  by the `get_fact_history` tool (status `current` / `superseded` / `retracted`).
- A fact about another person never overwrites the user's fact; it is rejected (logged as
  "rejected overwrite").

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

**Auto-recall** ([`core.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/core.py)):
before each answer, the same search runs with the user's message over this thread's messages older
than the 20-message window. Up to 3 hits, capped at 300 tokens, forgotten text redacted, are fenced
as data in a `<recall>` block placed as a non-system message just before the newest user turn. The
`search_history` tool stays available for anything recall misses.

## Document notes

`summarize_doc(doc_id)` ([`doc_notes.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/doc_notes.py))
makes one `doc_note` call per page and stores a page summary (at most 60 words) plus up to 15
`key_fact` lines in `doc_notes`. Pages already noted in this thread are skipped. Uncached pages run
in parallel on up to `DOC_NOTE_WORKERS` threads (default 4; `1` = one page after another); each
`doc_note` call is capped at `DOC_NOTE_MAX_TOKENS` completion tokens (default 1000). If one page
fails, the other pages are still saved and the first failure (in page order) is raised. The overview it returns is the stored
notes (page summaries + key facts, capped at 6000 chars); there is no extra reduce call.
`search_doc_notes` scores notes by query-word overlap (key facts first on ties). A variant doc id
(case, extension, spaces or dashes) resolves to a doc noted in this thread; an unknown id searches
the whole thread. Re-uploading a
file under the same name deletes its old notes.

## Forget

The `forget` tool ([`tools.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/tools.py))
runs only when the user's own latest message (cut before any Slack file note) asks for it **and**
names the fact:

- A sentence of that message holds one of `forget|delete|erase|remove|wipe|purge`, and the words
  that name the fact are together in that same sentence.
- Any sentence that negates the request ("don't forget ...", "never delete ...", "you cannot
  erase ...") is refused as a whole. Punctuation and markup other than a sentence break do not
  end the negation, so "never forget, then delete my address" and "don't *forget* my address" are
  refused too; only a sentence break does.
- Words the model adds to the subject are allowed only if, together with the words the user typed,
  they all belong to ONE stored fact key ("Forget my address" names `home_address`).

Anything wider returns `error: not run`. Corrections never call it. A deletion that resolves to no
stored data deletes nothing. With the resolver model down, a subject that matches more than one
key deletes nothing.

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
