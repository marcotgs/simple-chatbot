"""Memory subsystem: facts (scope + supersession), rolling summaries, FTS history search,
document notes and forget.

Integration API for core (T6):

    from chatbot import memory
    memory.tools.register_all()                         # once at import/startup
    system = SYSTEM_PROMPT + "\\n\\n" + memory.memory_text(user_id, thread_id)
    #   or memory.memory_context(user_id, thread_id) -> [] | [{"role": "system", ...}];
    #   merge it INTO the first system message: trim_to_budget only protects messages[0].
    memory.after_turn(user_id, thread_id, turn_no)      # from core.after_turn; never raises
    reply = memory.filter_reply(reply)                  # before storing and returning a reply
    memory.forget.contains_forgotten(text) -> bool      # send_message guard (T2)

Every side call goes through llm.chat with its own purpose (extract, summary, doc_note,
forget_resolve, forget_find), so it is budget-checked and logged like chat calls.
"""
from chatbot.memory import doc_notes, extract, facts, forget, retrieval, summary, tools  # noqa: F401
from chatbot.memory._llm import log

CONTEXT_MAX_CHARS = 3150  # ~900 tokens
FACTS_MAX_CHARS = 1400
_FILL = ("Remembered facts and standing preferences of the user. These are CURRENT and override anything "
         "said earlier; for earlier versions call get_fact_history:")
_SESSION = "Instructions for THIS chat only (never apply them in other chats):"


def _lines(items, budget):
    out, used = [], 0
    for f in reversed(items):  # newest first survive the cap
        line = f"- {f['key']}: {f['value']}"
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line) + 1
    return list(reversed(out))


def memory_text(user_id: str, thread_id: str) -> str:
    """Compact memory block (facts, this chat's summary, noted docs), capped ~900 tokens. '' if empty."""
    active = facts.get_active_facts(user_id, thread_id)
    glob = _lines([f for f in active if f["scope"] == "global"], FACTS_MAX_CHARS)
    sess = _lines([f for f in active if f["scope"] == "session"], FACTS_MAX_CHARS // 3)
    parts = []
    if glob:
        parts.append(_FILL + "\n" + "\n".join(glob))
    if sess:
        parts.append(_SESSION + "\n" + "\n".join(sess))
    docs = doc_notes.noted_docs(user_id)
    if docs:
        parts.append("Documents with saved notes (use search_doc_notes, not re-reading): " + ", ".join(docs))
    s = summary.get_summary(user_id, thread_id)
    if s and s["content"]:
        head = f"Summary of this chat up to turn {s['upto_turn']} (use search_history for exact details):\n"
        room = CONTEXT_MAX_CHARS - sum(len(p) + 2 for p in parts) - len(head)
        if room > 200:
            parts.append(head + s["content"][:room])
    return "\n\n".join(parts)[:CONTEXT_MAX_CHARS]


def memory_context(user_id: str, thread_id: str) -> list[dict]:
    text = memory_text(user_id, thread_id)
    return [{"role": "system", "content": text}] if text else []


def after_turn(user_id: str, thread_id: str, turn_no: int) -> None:
    """Extraction + summary roll. A memory failure must never break a reply."""
    for name, step in (("extract", lambda: extract.maybe_extract(user_id, thread_id, turn_no)),
                       ("summary", lambda: summary.maybe_roll_summary(user_id, thread_id))):
        try:
            step()
        except Exception as e:
            log(name, f"{type(e).__name__} (turn {turn_no})")


def filter_reply(text: str) -> str:
    """Redact forgotten values from model output."""
    return forget.redact_forgotten(text or "")
