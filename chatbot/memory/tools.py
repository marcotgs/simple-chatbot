"""Memory-facing tools in core.register_tool format: fn(user_id, thread_id, **args) -> str."""

import re

from chatbot import db, uploads
from chatbot.memory import doc_notes, facts, forget, retrieval

# forget is irreversible and model-triggered, so it runs only when the text the user typed in the
# current message asks for it in words AND names the subject in the same sentence (see _names);
# a correction ("it is X, not Y") goes through extraction, keeping the old value.
# ponytail: English keyword check; extend the verb list if other languages are needed.
_FORGET_ASK = re.compile(r"\b(forget|delete|erase|remove|wipe|purge)\b", re.I)
# A negation up to 4 words before the verb keeps the value ("I do not want you to delete X"),
# and so does every verb chained to it ("don't forget or delete X"); only whitespace may separate
# them, so a comma or period ends the negated clause.
_VERB = r"(?:forget|delete|erase|remove|wipe|purge)"
_NOT_FORGET = re.compile(
    r"(?:\b(?:not|never|no|dont|cant|wont)\b|n['’ʼ`]t\b)(?:\s+\w+){0,4}?\s+"
    + _VERB
    + r"\b(?:\s+(?:or|and|nor)\s+"
    + _VERB
    + r"\b)*",
    re.I,
)
# A forget ask counts only inside its own sentence ("Forget my address. My partner is Bob.").
_CLAUSE = re.compile(r"[.?!;]+(?:\s+|$)|\n")
_FILLER = {"my", "the", "a", "an", "please"}


def _names(clause: str, subject: str, keys: set[tuple[str, ...]]) -> bool:
    """The subject's words the user typed form one run of the clause ("home page address" does
    not name "home address", R5-SEC-S1), and words the model added fit, with the typed ones,
    inside ONE stored fact key ("my address" names "home address" when home_address is stored;
    "address phone partner" or "address and everything else" name nothing)."""
    said = [w for w in forget._words(clause) if w not in _FILLER]
    want = [w for w in forget._words(subject) if w not in _FILLER]
    typed = [w for w in want if w in said]
    run = any(said[i : i + len(typed)] == typed for i in range(len(said) - len(typed) + 1))
    if not forget.content_words(" ".join(typed)) or not run:
        return False
    return set(typed) == set(want) or any(set(want) <= set(k) for k in keys)


def _search_history(user_id, thread_id, query, limit=5):
    return retrieval.search_history(user_id, thread_id, query, limit)


def _search_doc_notes(user_id, thread_id, query, doc_id=None):
    return doc_notes.search_doc_notes(user_id, thread_id, query, doc_id)


def _summarize_doc(user_id, thread_id, doc_id):
    return doc_notes.summarize_doc(user_id, thread_id, doc_id)


def _get_fact_history(user_id, thread_id, key_or_query):
    rows = facts.get_fact_history(user_id, key_or_query, thread_id)
    if not rows:
        return "No stored facts match."
    return "\n".join(
        f"- {r['key']} = {r['value']} [{r['status']}, {r['scope']}, since {r['created_at'][:10]}"
        + (f", until {r['valid_to'][:10]}" if r["valid_to"] else "")
        + "]"
        for r in rows
    )[:2000]


def _forget(user_id, thread_id, subject):
    row = (
        db.conn()
        .execute(
            "SELECT content FROM messages WHERE user_id = ? AND thread_id = ? AND role = 'user'"
            " ORDER BY id DESC LIMIT 1",
            (user_id, thread_id),
        )
        .fetchone()
    )
    typed = row["content"].split(uploads.NOTE_PREFIX, 1)[0] if row else ""
    keys = {forget._words(f["key"]) for f in facts.get_active_facts(user_id, thread_id)}
    asks = [s for s in _CLAUSE.split(typed) if _FORGET_ASK.search(_NOT_FORGET.sub("", s))]
    if not any(_names(s, subject, keys) for s in asks):
        return (
            "error: not run. forget only runs when the user's current message explicitly asks to "
            "forget, delete or erase something and names it; use the user's own words as the "
            "subject. A correction or update is stored automatically "
            "and keeps the old version as history: just acknowledge it."
        )
    return forget.forget(user_id, subject, thread_id)


def _schema(name, description, props, required):
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


_STR = {"type": "string"}
TOOLS = [
    (
        _schema(
            "search_history",
            "Full-text search of earlier messages in this chat (older than what you can see). "
            "Use for exact details from long ago: names, numbers, codes, dates.",
            {"query": {**_STR, "description": "keywords"}, "limit": {"type": "integer"}},
            ["query"],
        ),
        _search_history,
    ),
    (
        _schema(
            "search_doc_notes",
            "Search saved notes and key facts (dates, owners, numbers) of documents summarized "
            "earlier in this chat.",
            {"query": _STR, "doc_id": {**_STR, "description": "optional document id"}},
            ["query"],
        ),
        _search_doc_notes,
    ),
    (
        _schema(
            "summarize_doc",
            "Read a long document page by page, save notes and key facts, and return an overview.",
            {"doc_id": _STR},
            ["doc_id"],
        ),
        _summarize_doc,
    ),
    (
        _schema(
            "get_fact_history",
            "All versions of a remembered fact or preference, including superseded ones. "
            "Use when the user asks what they said before or what changed.",
            {"key_or_query": {**_STR, "description": "fact key or topic, e.g. 'spelling'"}},
            ["key_or_query"],
        ),
        _get_fact_history,
    ),
    (
        _schema(
            "forget",
            "Permanently delete something from all memory. Only when the user explicitly asks to "
            "forget, delete or erase it (e.g. 'forget my address'); never for a correction or "
            "update of a value.",
            {"subject": _STR},
            ["subject"],
        ),
        _forget,
    ),
]


def register_all() -> None:
    from chatbot import core  # lazy: core imports the memory package

    for schema, fn in TOOLS:
        core.register_tool(schema["name"], schema, fn)
