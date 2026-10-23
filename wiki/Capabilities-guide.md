# Capabilities guide

A capability is a module that bundles instructions and tools for one job. Only a one-line index
entry per capability sits in every prompt. The full instructions and the tool schemas reach the
model only when it asks for them, so new capabilities cost almost nothing until used.

Registry: [`chatbot/capabilities/__init__.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/capabilities/__init__.py).

## How gating works

1. `capability_index()` adds one line per capability to the system block:
   `- <name>: <one_line> [tools: a, b]`, under a header telling the model to call
   `load_capability(name)` first.
2. On each model call, `core._exposed` sends the schemas of tools that belong to no capability
   (always on), plus the tools of capabilities loaded in **this** `send`. The loaded set starts
   empty on every `send`.
3. `load_capability(name)` returns `# <name> instructions` + the full `instructions` text. When the
   result does not start with `error`, core marks the capability loaded, and its tool schemas are
   sent from the next step on.
4. If the model calls a capability tool that is not loaded, the tool does not run. The result is
   an error telling the model to call `load_capability` first and retry.

## Tools

Always on (registered by [`memory/tools.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/memory/tools.py)
and the registry): `search_history`, `search_doc_notes`, `summarize_doc`, `get_fact_history`,
`forget`, `load_capability`.

Built-in capabilities:

| Capability | File | Tools | Notes |
| --- | --- | --- | --- |
| `docs` | [`docs.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/capabilities/docs.py) | `list_docs`, `fetch_doc` | One bounded page per call (`PAGE_TOKENS = 2800`). Whitelist: `docs/*.md` (not `_`-prefixed) plus the user's own Slack uploads (`upload-*`) |
| `linkedin` | [`linkedin.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/capabilities/linkedin.py) | `write_linkedin_post`, `validate_linkedin_post` | Deterministic checker: at most 1300 chars, at most 3 hashtags all at the end, no links. The drafter regenerates up to 3 times with the violations. With `source_doc_id` it also gets the doc's key facts (relevant ones first, every page covered; an un-noted doc is noted first; no facts = error). Nothing is posted; a passing post gets a draft link (below) |
| `messaging` | [`messaging.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/capabilities/messaging.py) | `send_message` | Writes to `send_log`, delivers nothing. Refuses text holding a forgotten value |

### LinkedIn draft link

When a turn produced a post that passed the checker, `core.send` appends one line to the reply:
`Open in LinkedIn (draft): https://www.linkedin.com/feed/?shareActive=true&text=<percent-encoded post>`.

- Built in code (`linkedin.draft_line`) from the validated post, never by the model. A forgotten
  value in the post is redacted before encoding. Draft lines the model writes itself are removed,
  and no link is added unless the reply shows the validated post.
- Added after the reply is stored: it is not in `messages`, `/history` or search, so a reloaded
  web page does not show it.
- Opening it shows LinkedIn's composer with the post as a draft (you must be logged in). Nothing is
  posted.
- Web page: rendered as a link (only the exact composer URL on the last line of the reply) that opens a LinkedIn tab and
  copies the post to the clipboard. Slack: a clickable `Open in LinkedIn (draft)` link.
- If the URL would exceed 2000 chars, the line says to copy the text above instead.
- LinkedIn's `text` parameter is not documented and may be ignored. Then paste the post from the
  clipboard (web) or copy it from the reply.

## Add a capability, step by step

1. Create one module in `chatbot/capabilities/`, e.g. `chatbot/capabilities/units.py`. Files whose
   name starts with `_` are skipped. Modules are found with `pkgutil`, so there is no list to edit.
2. Write each tool as `fn(user_id, thread_id, **args) -> str`. Return a string. Exceptions are
   caught and sent to the model as `error: <Type>: <message>`. Results over 12000 chars are
   truncated, so keep them small.
3. Export `CAPABILITY` with `name` (unique; a duplicate raises `ValueError`), `one_line` (short: it
   is in every prompt), `instructions` (full text, sent on demand) and `tools`, a list of
   `(schema, fn)` pairs. `schema` is the OpenAI `function` object: `{"name", "description",
   "parameters"}`. Tool names share one registry with the memory tools, so pick unused names.
4. Restart the process. `core` registers every capability tool at import
   (`capabilities.register_all`).
5. Optional: if the model should reach for it on certain requests, add one line to
   `SYSTEM_PROMPT` in [`core.py`](https://github.com/marcotgs/simple-chatbot/blob/main/chatbot/core.py),
   the way the `linkedin` and `messaging` lines do.
6. Add an offline test next to
   [`tests/test_capabilities.py`](https://github.com/marcotgs/simple-chatbot/blob/main/tests/test_capabilities.py)
   and run `make check`.

Minimal example (illustrative, not in the repo):

```python
"""Convert temperatures. Deterministic, no model call."""


def convert_temperature(user_id: str, thread_id: str, value: float = 0, to: str = "C") -> str:
    to = (to or "").upper()
    if to == "C":
        return f"{(float(value) - 32) * 5 / 9:.1f} C"
    if to == "F":
        return f"{float(value) * 9 / 5 + 32:.1f} F"
    return "error: 'to' must be C or F"


CAPABILITY = {
    "name": "units",
    "one_line": "convert temperatures between Celsius and Fahrenheit",
    "instructions": "Use convert_temperature(value, to) and quote its result verbatim.",
    "tools": [
        (
            {
                "name": "convert_temperature",
                "description": "Convert a temperature to C or F.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "number"},
                        "to": {"type": "string", "enum": ["C", "F"]},
                    },
                    "required": ["value", "to"],
                },
            },
            convert_temperature,
        ),
    ],
}
```

The index line becomes `- units: convert temperatures between Celsius and Fahrenheit [tools:
convert_temperature]`.

## Rules of thumb

- A tool that calls the model goes through `llm.chat(..., purpose="<name>")`, so the budget check
  and the call log apply (see `linkedin.generate_linkedin_post`).
- Treat document or upload text as data. Uploaded content only ever comes back as tool results,
  never in the system prompt.
- A tool that sends data out must check `memory.forget.contains_forgotten` the way
  `send_message` does.
