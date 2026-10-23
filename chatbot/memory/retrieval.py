"""FTS5 search over a thread's messages with a safe query builder and a hard result cap."""

import re

from chatbot import db

MAX_RESULT_CHARS = 2000  # ~570 tokens: the tool result must stay small inside an 8k context
MAX_TERMS = 16
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "his",
    "how",
    "i",
    "in",
    "is",
    "it",
    "its",
    "me",
    "my",
    "near",
    "not",
    "of",
    "on",
    "or",
    "our",
    "she",
    "so",
    "that",
    "the",
    "their",
    "them",
    "there",
    "they",
    "this",
    "to",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "you",
    "your",
    "about",
    "tell",
}


def query_terms(text: str) -> list[str]:
    """The searchable words of text: lowercase \\w+ tokens, no stopwords, deduped, capped."""
    terms: list[str] = []
    for t in re.findall(r"\w+", (text or "").lower()):
        if len(t) > 1 and t not in STOPWORDS and t not in terms:
            terms.append(t)
    return terms[:MAX_TERMS]


def fts_query(text: str) -> str:
    """User text -> FTS5 MATCH string. Every term is a double-quoted \\w+ token, so operators
    (AND/OR/NOT/NEAR), column filters (:), prefixes (*), ^ and stray quotes are inert. OR-joined."""
    return " OR ".join(f'"{t}"' for t in query_terms(text))


def search_rows(
    user_id: str, thread_id: str, query: str, limit: int = 5, before_id: int | None = None
) -> list:
    """Best-matching (bm25) messages of this thread with an id below before_id (default: older
    than the current turn), as rows of turn_no, role, content, snip. Other threads are never
    searched (session privacy)."""
    q = fts_query(query)
    if not q:
        return []
    if before_id is None:
        older, args = (
            "m.turn_no < (SELECT COALESCE(MAX(turn_no), 0) FROM messages"
            " WHERE user_id = ? AND thread_id = ?)",
            (user_id, thread_id),
        )
    else:
        older, args = "m.id < ?", (before_id,)
    return (
        db.conn()
        .execute(
            "SELECT m.turn_no, m.role, m.content,"
            " snippet(messages_fts, 0, '', '', ' ... ', 32) AS snip"
            " FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
            " WHERE messages_fts MATCH ? AND m.user_id = ? AND m.thread_id = ?"
            f" AND {older} ORDER BY bm25(messages_fts) LIMIT ?",
            (q, user_id, thread_id, *args, limit),
        )
        .fetchall()
    )


def search_history(user_id: str, thread_id: str, query: str, limit: int = 5) -> str:
    """Best-matching earlier messages of this thread (bm25), as short snippets with turn numbers.
    The current turn is excluded; other threads are never searched (session privacy)."""
    if not fts_query(query):
        return "No searchable words in the query."
    rows = search_rows(user_id, thread_id, query, max(1, min(int(limit or 5), 10)))
    if not rows:
        return "No earlier messages match."
    out = "\n".join(f"[turn {r['turn_no']} {r['role']}] {r['snip']}" for r in rows)
    if len(out) > MAX_RESULT_CHARS:
        out = out[: MAX_RESULT_CHARS - 12] + " [truncated]"
    return out
