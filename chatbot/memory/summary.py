"""Rolling per-thread summary of messages older than the recent window (one row per thread)."""

from chatbot import db
from chatbot.memory._llm import ask, log
from chatbot.memory.forget import redact_forgotten

SUMMARY_MAX_CHARS = 2800  # ~800 tokens
CHUNK_CHARS = 12000  # ~3.4k tokens of transcript per call; + summary + prompt stays < 6.5k
PER_MESSAGE_CHARS = 1500  # a pasted doc in one message must not blow the chunk
MIN_NEW_TURNS = 10  # roll in batches, not on every turn

_PROMPT = """You maintain the running summary of a long chat between a user and an assistant.
Merge the NEW MESSAGES into the PREVIOUS SUMMARY and return only the updated summary as plain text, at most 300 words.
Keep every concrete detail: names, numbers, codes, amounts, dates, places, decisions, preferences, commitments and document ids,
with the turn number for each key detail, e.g. "(turn 20)". Drop greetings and chit-chat; compress older material first.
Text shown as [forgotten] was deleted at the user's request: never reconstruct, guess or mention it."""  # noqa: E501


def tail(text: str, room: int) -> str:
    """Newest end of a summary (the prompt compresses older material first), cut at a sentence
    start."""
    if len(text) <= room:
        return text
    cut = text[-room:]
    i = cut.find(". ")
    return cut[i + 2 :] if 0 <= i < room // 2 else cut


def get_summary(user_id: str, thread_id: str) -> dict | None:
    r = (
        db.conn()
        .execute(
            "SELECT upto_turn, content FROM summaries WHERE user_id = ? AND thread_id = ? ORDER BY "
            "id DESC LIMIT 1",
            (user_id, thread_id),
        )
        .fetchone()
    )
    return {"upto_turn": r["upto_turn"], "content": r["content"]} if r else None


def _turn_chunks(rows):
    """Group messages by turn, then pack whole turns into chunks of ~CHUNK_CHARS."""
    turns: list[list] = []
    for r in rows:
        if not turns or turns[-1][0]["turn_no"] != r["turn_no"]:
            turns.append([])
        turns[-1].append(r)
    chunk, size = [], 0
    for t in turns:
        n = sum(min(len(r["content"]), PER_MESSAGE_CHARS) for r in t)
        if chunk and size + n > CHUNK_CHARS:
            yield chunk
            chunk, size = [], 0
        chunk.extend(t)
        size += n
    if chunk:
        yield chunk


def maybe_roll_summary(user_id: str, thread_id: str, force: bool = False) -> bool:
    """Fold turns that left the recent window into the summary. Returns True if it rolled."""
    from chatbot import core  # lazy: core imports the memory package

    c = db.conn()
    current = get_summary(user_id, thread_id)
    upto, text = (current["upto_turn"], current["content"]) if current else (0, "")
    first_recent = c.execute(
        "SELECT turn_no FROM messages WHERE user_id = ? AND thread_id = ? ORDER BY id DESC LIMIT 1 "
        "OFFSET ?",
        (user_id, thread_id, core.RECENT_TURNS - 1),
    ).fetchone()
    if first_recent is None:
        return False
    cutoff = first_recent["turn_no"] - 1
    if cutoff - upto < (1 if force else MIN_NEW_TURNS):
        return False
    rows = c.execute(
        "SELECT turn_no, role, content FROM messages WHERE user_id = ? AND thread_id = ?"
        " AND turn_no > ? AND turn_no <= ? ORDER BY id",
        (user_id, thread_id, upto, cutoff),
    ).fetchall()
    for chunk in _turn_chunks(rows):
        lines = "\n".join(
            f"[turn {r['turn_no']} {r['role']}] {r['content'][:PER_MESSAGE_CHARS]}" for r in chunk
        )
        new = ask(
            _PROMPT,
            f"PREVIOUS SUMMARY:\n{text or '(none)'}\n\nNEW MESSAGES:\n{lines}",
            "summary",
            thread_id,
        ).strip()
        if new:
            text = tail(redact_forgotten(new), SUMMARY_MAX_CHARS)
        else:
            log("summary", "empty model reply; kept previous summary")
        upto = chunk[-1]["turn_no"]
        with c:  # one row per thread; commit per chunk so progress survives a later failure
            c.execute(
                "DELETE FROM summaries WHERE user_id = ? AND thread_id = ?", (user_id, thread_id)
            )
            c.execute(
                "INSERT INTO summaries (user_id, thread_id, upto_turn, content) VALUES (?, ?, ?, "
                "?)",
                (user_id, thread_id, upto, text),
            )
    return True
