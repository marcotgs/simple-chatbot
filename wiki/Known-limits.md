# Known limits

Accepted risks and open issues as of the current `main`. Each item names where it lives.

## Uploads

- **No upload size cap by default.** `MAX_FILE_BYTES` defaults to `0` (unlimited), and PDFs have no
  page cap. A huge or hostile PDF can burn CPU in `pypdf`
  ([`uploads.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/uploads.py)). Set
  `MAX_FILE_BYTES` if other people can send the bot files.
- **`summarize_doc` is slow on long files.** One model call per page
  ([`doc_notes.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/doc_notes.py)).
  Run sequentially, an 18-page CSV took about 2-3 minutes in a live test. Pages now run on up to
  `DOC_NOTE_WORKERS` threads (default 4); the live time with threads is not re-measured. Pages
  already noted in this thread are cached, so a second call in the same thread is fast.
- **Re-noting a doc per thread.** Notes and `search_doc_notes` are per thread; `list_docs`,
  `fetch_doc` and uploads stay per user. A doc summarized in thread A and then in thread B is noted
  again in B: one `doc_note` call per page
  ([`doc_notes.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/doc_notes.py)).
- Forget rewrites upload files in place. The filesystem may keep old blocks (no secure erase).

## Forget

- **English-only cue.** The `forget` tool runs only when the user's own message asks for it
  (`forget|delete|erase|remove|wipe|purge`) and names the fact in the same sentence as the verb.
  Words the model adds must, with the typed ones, all belong to one stored fact key, and a sentence
  that negates the request is refused as a whole; punctuation and markup other than a sentence
  break do not end the negation
  ([`memory/tools.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/tools.py)).
  A request in another language, or one wider than a single fact, is refused.
- **Negation reach.** A negation more than 4 words before the verb is not detected, so that
  sentence can still delete.
- **Fail-closed refusals.** The negation check errs toward refusing: "No, forget my address" and
  "Don't keep it, forget my address" are refused. Rephrase without the negation.
- **Single owner, global tombstones.** Tombstones in `forgotten` are not per user: a value one user
  forgot is redacted for every user of the instance. Fine for the intended single-owner use, wrong
  for multiple users.
- Generic values (a year, a house number, a short single word) delete their fact rows but are never
  scrubbed from history or tombstoned. A two-token value with a digit (for example "Q3 2026") can be
  tombstoned and is then redacted everywhere.
- Street variants are redacted broadly: forgetting "742 Example Street" also redacts "Example
  Street" in unrelated text.
- `verify_clean` can report a false WARNING when the words of a value (dates, common words) still
  appear separately in the FTS vocabulary.
- Subjects whose words are all 2 characters or shorter ("forget my IP") are always refused, because
  `content_words` drops them. Fail-closed (R5-SEC-S4).
- Copies outside the app are out of reach: backups of the `data/` folder you made yourself and
  OpenRouter retention.

## Auto-recall

Accepted residuals of the `<recall>` block
([`core.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/core.py), see
[[Memory model|Memory-model]]):

- **Weak hits.** The bm25 search OR-joins the query words, so common words can match unrelated old
  messages and inject weak hits. The block is capped at 300 tokens.
- **Trimmed window messages are not recalled.** Recall searches only messages older than the
  20-message window. A window message that `trim_to_budget` drops is reachable only through the
  `search_history` tool.
- **Stored text is model-visible.** Recalled text reaches the model as user-role data. It is fenced
  and neutralised, but the model still reads it, so an old hostile message can still try to steer it.
- **Two user-role messages in a row.** The recall block and the current message are consecutive
  user-role messages. OpenRouter with `openai/gpt-4.1-mini` accepts this; other providers are
  untested.

## Token estimate

- **CJK under-count.** The estimator counts letters at 3.5 chars per token
  ([`llm.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/llm.py)). Chinese,
  Japanese and Korean characters are word characters but cost about one token each, so CJK-heavy
  text is under-estimated. The post-call check on `usage.prompt_tokens` still catches a real
  overrun: that call fails with `BudgetExceeded` and is retried once with a tighter trim.

## Memory extraction

- **Extraction flakes on gpt-4.1-mini.** Measured over 3 live runs of
  [`tests/test_extraction_quality.py`](https://github.com/marcotgs/simple-chatbot/blob/main/tests/test_extraction_quality.py):
  held-out precision 0.86, recall 1.00. The one failing held-out row is a temporary arrangement
  phrased in Portuguese ("while <name> is on vacation, I answer purchase requests"), which gets stored
  instead of skipped. Prompt wording did not fix it without overfitting. Severity: low (a
  short-lived fact is stored, nothing is lost).
- Other observed flakes: a fact about a named child stored without the name in the value ("seven
  years old" instead of "<name>, seven years old"), and "in this chat, reply in bullet points"
  sometimes not stored.
- **Over-eager overwrite.** A user's own fact restated with a new value replaces the old one, so
  the live risk is a wrong replace, not a dropped correction. The old value stays in fact history
  (`get_fact_history`). Accepted residuals:
  - A statement about a past or related thing (for example where you grew up) can replace a related
    key about 1 time in 3.
  - A third-party fact under a known key can still replace the user's value if the model misjudges
    whose fact it is (SEC-S1). `get_fact_history` recovers it.
  - Past 40 known facts, older keys are shown to the extractor without their value, so a change to
    one of them is judged on the key alone.
- **Jev gate on an alpha endpoint.** The salience gate calls OpenRouter's
  `/api/alpha/decisions` with `typesafe/jev-1.14`. An alpha API can change or disappear, and a
  silent model update can shift scores. The gate is fail-open, so errors cost money, not facts. A
  score shift could skip worthy turns: rerun `tests/test_gate_quality.py` after any change.

## Tests

- **Port collisions under WSL.** Local test servers bind port 0. With WSL mirrored or consomme
  networking, that port can rarely collide with a Windows listener, and the test gets a 404 from a
  foreign process. This is the environment, not the product: rerun the test.


## Other

- **LinkedIn nudge is a heuristic.** When the user mentions LinkedIn, `linkedin` is not loaded and
  the reply ends in a hashtag line, `core.run_tool_loop` asks the model once to redo the reply
  through `write_linkedin_post`
  ([`core.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/core.py)). An answer
  that ends in a line of example hashtags costs one extra call; the nudge tells the model to resend
  a non-post reply unchanged.

- One process by design: one SQLite file in `data/` (`DB_PATH`), in-memory locks and Slack dedupe.
  Do not run two instances on the same database.
- Documents in `docs/` and uploads are summarized into `doc_notes` and replayed later, so a
  hostile document is a stored prompt-injection channel. Keep `docs/` to trusted files.
- Per-thread locks in `core.py` are never evicted (fine for one user).
- Model calls ([`llm.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/llm.py))
  time out after 60 s per attempt. 408, 429, 5XX and connection errors are retried with backoff
  for up to 15 s (0.5 to 4 s apart), so a timed-out attempt is final. `Retry-After` is ignored:
  an hour-long value would park a thread for that hour.
- Model calls trust the pinned `certifi` CA bundle, not the OS store, unless `SSL_CERT_FILE` or
  `SSL_CERT_DIR` is set. Behind a TLS-inspecting proxy, chat fails closed with a TLS error while
  the salience gate (stdlib `urllib`, OS store) still works. Point `SSL_CERT_FILE` at the proxy CA.
