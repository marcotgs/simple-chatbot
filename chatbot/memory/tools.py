"""Memory-facing tools in core.register_tool format: fn(user_id, thread_id, **args) -> str."""
from chatbot.memory import doc_notes, facts, forget, retrieval


def _search_history(user_id, thread_id, query, limit=5):
    return retrieval.search_history(user_id, thread_id, query, limit)


def _search_doc_notes(user_id, thread_id, query, doc_id=None):
    return doc_notes.search_doc_notes(user_id, query, doc_id)


def _summarize_doc(user_id, thread_id, doc_id):
    return doc_notes.summarize_doc(user_id, thread_id, doc_id)


def _get_fact_history(user_id, thread_id, key_or_query):
    rows = facts.get_fact_history(user_id, key_or_query, thread_id)
    if not rows:
        return "No stored facts match."
    return "\n".join(f"- {r['key']} = {r['value']} [{r['status']}, {r['scope']}, since {r['created_at'][:10]}"
                     + (f", until {r['valid_to'][:10]}" if r["valid_to"] else "") + "]" for r in rows)[:2000]


def _forget(user_id, thread_id, subject):
    return forget.forget(user_id, subject, thread_id)


def _schema(name, description, props, required):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}


_STR = {"type": "string"}
TOOLS = [
    (_schema("search_history", "Full-text search of earlier messages in this chat (older than what you can see). "
             "Use for exact details from long ago: names, numbers, codes, dates.",
             {"query": {**_STR, "description": "keywords"}, "limit": {"type": "integer"}}, ["query"]), _search_history),
    (_schema("search_doc_notes", "Search saved notes and key facts (dates, owners, numbers) of documents summarized earlier.",
             {"query": _STR, "doc_id": {**_STR, "description": "optional document id"}}, ["query"]), _search_doc_notes),
    (_schema("summarize_doc", "Read a long document page by page, save notes and key facts, and return an overview.",
             {"doc_id": _STR}, ["doc_id"]), _summarize_doc),
    (_schema("get_fact_history", "All versions of a remembered fact or preference, including superseded ones. "
             "Use when the user asks what they said before or what changed.",
             {"key_or_query": {**_STR, "description": "fact key or topic, e.g. 'spelling'"}}, ["key_or_query"]),
     _get_fact_history),
    (_schema("forget", "Permanently delete something the user asks you to forget (e.g. 'my address') from all memory.",
             {"subject": _STR}, ["subject"]), _forget),
]


def register_all() -> None:
    from chatbot import core  # lazy: core imports the memory package
    for schema, fn in TOOLS:
        core.register_tool(schema["name"], schema, fn)
