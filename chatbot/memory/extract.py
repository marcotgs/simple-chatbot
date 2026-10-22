"""Fact extraction from user messages: batched every FACT_EXTRACT_BATCH turns, or immediately
when the latest message looks like an explicit statement (cheap regex gate; the LLM decides)."""
import re

from chatbot import config, db
from chatbot.memory import facts
from chatbot.memory._llm import ask_json, log
from chatbot.memory.forget import contains_forgotten

CHUNK_CHARS = 9000         # ~2.6k tokens of messages per call
PER_MESSAGE_CHARS = 2000
KNOWN_FACTS_CHARS = 1500

# ponytail: English-only gate; a miss only delays extraction to the next batch, never loses it.
TRIGGER = re.compile(
    r"\b(my \w+(?: \w+)? (?:is|are|was|lives?|works?)|i live|i'm|i am|i work|call me|remember|always|never"
    r"|from now on|switch(?:ed)? to|prefer|instead|no longer|anymore|don't|do not|stop|for this|in this chat"
    r"|forget)\b", re.I)

_PROMPT = """You extract durable memory from a user's chat messages. Return ONLY JSON:
{"facts": [{"key": "...", "value": "...", "scope": "global|session|ephemeral", "action": "set|retract", "turn": 0}]}
Rules:
- Only facts, preferences and standing instructions the USER states (about themselves, people in their life, or how the assistant should behave). Ignore questions, chit-chat, assistant text, and requests to forget something (handled elsewhere).
- key: short snake_case, stable across rephrasings: "manager", "home_address", "partner_name", "spelling_preference", "tone". If KNOWN FACTS already has a key for the same thing, reuse that exact key so the new value replaces it.
- value: short and self-contained, keep exact names, numbers and dates ("Priya Raman", "American spelling").
- scope: "global" = applies in every chat (facts about the user; "always ...", "from now on ..."). "session" = only this chat ("in this chat", "for this conversation"). "ephemeral" = a one-off for the current task ("for this draft only", "just this once").
- A change ("switch to American spelling", "my new manager is X") is action "set" with the SAME key as the old fact. A plain negation with no new value ("I don't have a car anymore") is action "retract".
- turn: the turn number the statement came from.
- If nothing qualifies, return {"facts": []}."""


def _cursor(user_id: str, thread_id: str) -> int:
    r = db.conn().execute("SELECT extracted_upto FROM memory_cursor WHERE user_id = ? AND thread_id = ?",
                          (user_id, thread_id)).fetchone()
    return r[0] if r else 0


def _set_cursor(user_id: str, thread_id: str, turn_no: int) -> None:
    c = db.conn()
    with c:
        c.execute("INSERT INTO memory_cursor (user_id, thread_id, extracted_upto) VALUES (?, ?, ?)"
                  " ON CONFLICT (user_id, thread_id) DO UPDATE SET extracted_upto = MAX(extracted_upto, excluded.extracted_upto)",
                  (user_id, thread_id, turn_no))


def _chunks(rows):
    chunk, size = [], 0
    for r in rows:
        n = min(len(r["content"]), PER_MESSAGE_CHARS)
        if chunk and size + n > CHUNK_CHARS:
            yield chunk
            chunk, size = [], 0
        chunk.append(r)
        size += n
    if chunk:
        yield chunk


def maybe_extract(user_id: str, thread_id: str, turn_no: int) -> int:
    """Extract facts from user turns after the per-thread cursor if due. Returns facts stored.
    Model/network errors propagate (cursor not advanced, retried next time); bad JSON is skipped."""
    cursor = _cursor(user_id, thread_id)
    if turn_no <= cursor:
        return 0
    c = db.conn()
    latest = c.execute("SELECT content FROM messages WHERE user_id = ? AND thread_id = ? AND turn_no = ?"
                       " AND role = 'user' ORDER BY id DESC LIMIT 1", (user_id, thread_id, turn_no)).fetchone()
    due = turn_no - cursor >= max(1, config.FACT_EXTRACT_BATCH) or (latest and TRIGGER.search(latest["content"]))
    if not due:
        return 0
    rows = c.execute("SELECT turn_no, content FROM messages WHERE user_id = ? AND thread_id = ? AND role = 'user'"
                     " AND turn_no > ? AND turn_no <= ? ORDER BY id", (user_id, thread_id, cursor, turn_no)).fetchall()
    stored = 0
    for chunk in _chunks(rows):
        stored += _extract_chunk(user_id, thread_id, chunk)
        _set_cursor(user_id, thread_id, chunk[-1]["turn_no"])
    _set_cursor(user_id, thread_id, turn_no)
    return stored


def _extract_chunk(user_id: str, thread_id: str, chunk) -> int:
    known = "\n".join(f"- {f['key']}: {f['value']}" for f in reversed(facts.get_active_facts(user_id, thread_id)))
    msgs = "\n".join(f"[turn {r['turn_no']}] {r['content'][:PER_MESSAGE_CHARS]}" for r in chunk)
    obj = ask_json(_PROMPT, f"KNOWN FACTS:\n{known[:KNOWN_FACTS_CHARS] or '(none)'}\n\nMESSAGES:\n{msgs}",
                   "extract", thread_id)
    items = obj.get("facts") if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return 0
    turns = {r["turn_no"] for r in chunk}
    stored = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        key, value = facts.normalize_key(it.get("key")), str(it.get("value") or "").strip()[:500]
        scope = str(it.get("scope") or "").lower()
        scope = scope if scope in facts.SCOPES else "session"  # unknown scope: never leak globally
        try:
            turn = int(it.get("turn"))
        except (TypeError, ValueError):
            turn = None
        turn = turn if turn in turns else chunk[-1]["turn_no"]
        if not key or scope == "ephemeral":  # ephemeral = never stored
            continue
        if str(it.get("action") or "set").lower() == "retract":
            facts.retract_fact(user_id, thread_id, key)
        elif not value:
            continue
        elif contains_forgotten(value):
            log("extract", "skipped a fact matching a forgotten value")
        else:
            facts.upsert_fact(user_id, thread_id, scope, key, value, turn)
            stored += 1
    return stored
