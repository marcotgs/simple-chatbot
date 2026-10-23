"""Durable facts with scope and supersession. Storage: table `facts` (column `subject` = key).

scope: global (every thread, thread_id NULL) | session (only its thread) | ephemeral (never stored).
A new value for the same key and scope supersedes the old row: old row keeps its value and gets
superseded_by + valid_to, so history stays answerable ("what did I say before?").
"""

import re

from chatbot import db

SCOPES = ("global", "session", "ephemeral")
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_STOP = {
    "the",
    "and",
    "for",
    "what",
    "did",
    "say",
    "about",
    "before",
    "my",
    "was",
    "said",
    "that",
    "this",
    "with",
    "you",
    "your",
    "are",
    "how",
    "which",
    "when",
    "tell",
    "me",
}


def normalize_key(key) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key or "").lower()).strip("_")[:64]


def _as_dict(r) -> dict:
    if r["superseded_by"] is not None:
        status = "superseded"
    elif r["valid_to"] is not None:
        status = "retracted"
    else:
        status = "current"
    return {
        "id": r["id"],
        "key": r["subject"],
        "value": r["value"],
        "scope": r["scope"],
        "thread_id": r["thread_id"],
        "source_turn": r["source_turn"],
        "created_at": r["created_at"],
        "valid_to": r["valid_to"],
        "superseded_by": r["superseded_by"],
        "status": status,
    }


def upsert_fact(
    user_id: str,
    thread_id: str | None,
    scope: str,
    key: str,
    value: str,
    source_turn: int | None = None,
    replace: bool = True,
) -> int | None:
    """Store a fact, superseding the active one with the same key+scope(+thread). Returns the id
    (existing id if the value is unchanged), or None for ephemeral/empty facts. With
    replace=False an active different value is kept and None is returned (checked under the
    write lock)."""
    if scope not in SCOPES:
        raise ValueError(f"bad scope {scope!r}")
    key, value = normalize_key(key), str(value or "").strip()
    if scope == "ephemeral" or not key or not value:
        return None
    if scope == "session" and not thread_id:
        raise ValueError("session fact needs a thread_id")
    tid = thread_id if scope == "session" else None
    c = db.conn()
    with c:
        if (
            not c.in_transaction
        ):  # W8: take the write lock before reading, so two threads can't both supersede
            c.execute("BEGIN IMMEDIATE")
        active = c.execute(
            "SELECT id, value FROM facts WHERE user_id = ? AND subject = ? AND scope = ? AND "
            "thread_id IS ?"
            " AND superseded_by IS NULL AND valid_to IS NULL ORDER BY id DESC",
            (user_id, key, scope, tid),
        ).fetchall()
        if active and active[0]["value"].casefold() == value.casefold():
            return active[0]["id"]
        if active and not replace:
            return None
        new_id = c.execute(
            "INSERT INTO facts (user_id, thread_id, scope, subject, value, source_turn) VALUES (?, "
            "?, ?, ?, ?, ?)",
            (user_id, tid, scope, key, value, source_turn),
        ).lastrowid
        c.executemany(
            f"UPDATE facts SET superseded_by = ?, valid_to = {_NOW} WHERE id = ?",
            [(new_id, r["id"]) for r in active],
        )
    return new_id


def retract_fact(user_id: str, thread_id: str | None, key: str) -> int:
    """Negation without replacement ("I no longer have a manager"). Returns rows retracted."""
    c = db.conn()
    with c:
        return c.execute(
            f"UPDATE facts SET valid_to = {_NOW} WHERE user_id = ? AND subject = ?"
            " AND superseded_by IS NULL AND valid_to IS NULL"
            " AND (scope = 'global' OR (scope = 'session' AND thread_id = ?))",
            (user_id, normalize_key(key), thread_id),
        ).rowcount


def get_active_facts(user_id: str, thread_id: str | None) -> list[dict]:
    """Global facts plus this thread's session facts, current versions only, oldest first."""
    rows = (
        db.conn()
        .execute(
            "SELECT * FROM facts WHERE user_id = ? AND superseded_by IS NULL AND valid_to IS NULL"
            " AND (scope = 'global' OR (scope = 'session' AND thread_id = ?)) ORDER BY id",
            (user_id, thread_id),
        )
        .fetchall()
    )
    return [_as_dict(r) for r in rows]


def get_fact_history(user_id: str, key_or_query: str, thread_id: str | None = None) -> list[dict]:
    """All versions (current, superseded, retracted) matching a key or free text, grouped by key,
    oldest first. With thread_id, other threads' session facts are excluded."""
    key = normalize_key(key_or_query)
    words = [
        w
        for w in re.findall(r"[a-z0-9]+", str(key_or_query).lower())
        if len(w) > 2 and w not in _STOP
    ]
    conds, params = ["subject = ?"], [key]
    for w in words[:8]:  # w is [a-z0-9] only, so no LIKE wildcards can sneak in
        conds.append("subject LIKE ? OR value LIKE ?")
        params += [f"%{w}%", f"%{w}%"]
    sql = f"SELECT * FROM facts WHERE user_id = ? AND ({' OR '.join(conds)})"
    if thread_id is not None:
        sql += " AND (scope = 'global' OR thread_id = ?)"
        params.append(thread_id)
    rows = db.conn().execute(sql + " ORDER BY subject, id LIMIT 50", (user_id, *params)).fetchall()
    return [_as_dict(r) for r in rows]
