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
  already noted are cached, so a second call is fast.
- Forget rewrites upload files in place. The filesystem may keep old blocks (no secure erase).

## Forget

- **English-only cue.** The `forget` tool runs only when the user's message contains
  `forget|delete|erase|remove|wipe|purge` and every content word of the subject
  ([`memory/tools.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/tools.py)).
  A request in another language is refused.
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
- Subject binding is a bag of words: "remove the home page address from the doc" with subject
  `home address` passes the guard and deletes `home_address` (open review finding R5-SEC-S1).
- Subjects whose words are all 2 characters or shorter ("forget my IP") are always refused, because
  `content_words` drops them. Fail-closed (R5-SEC-S4).
- Copies outside the app are out of reach: backups of the `data/` folder you made yourself and
  OpenRouter retention.

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
- The overwrite guard keeps the old fact when the extractor does not flag a change as
  `"correction": true`. A real correction phrased vaguely can be dropped. The user can restate it
  explicitly.
- **Jev gate on an alpha endpoint.** The salience gate calls OpenRouter's
  `/api/alpha/decisions` with `typesafe/jev-1.14`. An alpha API can change or disappear, and a
  silent model update can shift scores. The gate is fail-open, so errors cost money, not facts. A
  score shift could skip worthy turns: rerun `tests/test_gate_quality.py` after any change.

## Slack

- **Live Slack is not tested automatically.** Socket Mode, `/bot`, mentions and file share are tested
  offline with fakes ([`tests/test_slack_app.py`](https://github.com/marcotgs/simple-chatbot/blob/main/tests/test_slack_app.py),
  integration tests). A real workspace is a manual check (see [[Slack setup|Slack-setup]]).
- Event dedupe is in memory (last 1000 keys). A Slack retry that arrives across a restart can be
  answered twice.

## Other

- One process by design: one SQLite file in `data/` (`DB_PATH`), in-memory locks and Slack dedupe.
  Do not run two instances on the same database.
- Documents in `docs/` and uploads are summarized into `doc_notes` and replayed later, so a
  hostile document is a stored prompt-injection channel. Keep `docs/` to trusted files.
- Per-thread locks in `core.py` are never evicted (fine for one user).
