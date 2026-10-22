"""Forget: purge a value from every store, keep only a hashed tombstone, guard outputs.

Tombstones are SHA-256 digests of the normalized token sequence (casefold, punctuation and
whitespace collapsed, street-type abbreviations canonicalized). contains_forgotten() slides a
window of the same length over any text and compares digests, so no plaintext is ever stored.
"""
import hashlib
import re

from chatbot import config, db
from chatbot.memory import facts
from chatbot.memory._llm import ask_json, log

REDACTED = "[forgotten]"
_TOKEN = re.compile(r"[^\W_]+")
_CANON = {"street": "st", "avenue": "ave", "road": "rd", "apartment": "apt", "boulevard": "blvd",
          "drive": "dr", "lane": "ln", "suite": "ste", "place": "pl", "court": "ct", "number": "no"}
_ALTS: dict[str, set[str]] = {}
for _long, _short in _CANON.items():
    _ALTS.setdefault(_short, {_short}).add(_long)
_STOP = {"my", "the", "forget", "please", "about", "that", "this", "what", "where", "and", "all", "me",
         "i", "you", "it", "is", "of", "a", "an", "to", "info", "information", "everything", "details"}


def _canon(tok: str) -> str:
    t = tok.casefold()
    return _CANON.get(t, t)


def _spans(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), _canon(m.group())) for m in _TOKEN.finditer(text or "")]


def _digest(tokens: list[str]) -> str:
    return hashlib.sha256(" ".join(tokens).encode()).hexdigest()


def _variants(value: str) -> list[list[str]]:
    """The full value plus comma/line parts that are specific enough to identify it alone."""
    full = [t for _, _, t in _spans(value)]
    out = [full] if full else []
    for part in re.split(r"[,;\n]", value):
        toks = [t for _, _, t in _spans(part)]
        specific = len(toks) >= 3 or (len(toks) >= 2 and any(ch.isdigit() for t in toks for ch in t))
        if toks and toks != full and specific and toks not in out:
            out.append(toks)
    return out


def _match_spans(text: str) -> list[tuple[int, int]]:
    rows = db.conn().execute("SELECT digest, ntokens FROM forgotten").fetchall()
    if not rows or not text:
        return []
    by_len: dict[int, set[str]] = {}
    for r in rows:
        by_len.setdefault(r["ntokens"], set()).add(r["digest"])
    toks = _spans(text)
    words = [t for _, _, t in toks]
    hits = []
    for n, digests in by_len.items():
        for i in range(len(toks) - n + 1):
            if _digest(words[i:i + n]) in digests:
                hits.append((toks[i][0], toks[i + n - 1][1]))
    return hits


def contains_forgotten(text: str) -> bool:
    """True if text contains any forgotten value (case/whitespace/punctuation-insensitive)."""
    return bool(_match_spans(text))


def redact_forgotten(text: str) -> str:
    """Replace every forgotten value in text with a placeholder."""
    merged: list[list[int]] = []
    for start, end in sorted(_match_spans(text)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + REDACTED + text[end:]
    return text


def _pattern(value: str) -> re.Pattern:
    """Regex for the plaintext value tolerant to case, spacing, punctuation, abbreviations."""
    pieces = []
    for _, _, tok in _spans(value):
        alts = sorted(_ALTS.get(tok, {tok}), key=len, reverse=True)
        pieces.append("(?:" + "|".join(re.escape(a) for a in alts) + ")")
    return re.compile(r"(?<![^\W_])" + r"[\W_]+".join(pieces) + r"(?![^\W_])", re.I)


def _scrub(text: str, patterns: list[re.Pattern]) -> str:
    for p in patterns:
        text = p.sub(REDACTED, text)
    return text


# --- resolution: what does "forget X" refer to? -------------------------------------------------

_RESOLVE = """The user asked the assistant to forget something. Decide exactly what stored data must be deleted.
Return ONLY JSON: {"fact_ids": [ids], "values": ["..."], "search_terms": ["..."]}
- fact_ids: every stored fact about that subject, including old versions.
- values: the literal sensitive data to scrub from history, copied verbatim (e.g. the address itself), not the subject name.
- search_terms: words likely to appear in old messages that mention it (e.g. "live", "address", "street").
Never include generic words in values."""

_FIND = """The user asked the assistant to forget something. Below are old messages that may mention it.
Return ONLY JSON: {"values": ["..."]} with the exact sensitive strings (copied verbatim from the messages)
that are the data to forget. Return {"values": []} if none of the messages contain it."""


def _resolve(user_id: str, subject: str, thread_id: str | None) -> tuple[set[int], set[str]]:
    rows = db.conn().execute(
        "SELECT id, subject, value FROM facts WHERE user_id = ? ORDER BY id DESC LIMIT 150", (user_id,)
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    listing = "\n".join(f"{r['id']} | {r['subject']} | {r['value'][:120]}" for r in rows)[:5000]
    ids, values, terms = set(), set(), []
    try:
        obj = ask_json(_RESOLVE, f"FORGET REQUEST: {subject}\n\nSTORED FACTS (id | key | value):\n{listing or '(none)'}",
                       "forget_resolve", thread_id) or {}
        ids = {int(i) for i in obj.get("fact_ids") or [] if str(i).isdigit() and int(i) in by_id}
        values = {str(v).strip() for v in obj.get("values") or [] if isinstance(v, (str, int))}
        terms = [str(t) for t in obj.get("search_terms") or [] if isinstance(t, str)]
    except Exception as e:  # model down: fall back to key matching below
        log("forget_resolve", type(e).__name__)
    words = [w for w in (t for _, _, t in _spans(subject)) if w not in _STOP and len(w) > 2]
    if not ids and not values:
        ids = {r["id"] for r in rows if any(w in r["subject"] for w in words)}
    if not ids and not values:
        values |= _find_in_messages(user_id, subject, terms + words, thread_id)
    if any(ch.isdigit() for ch in subject):  # "forget 42 Example Road": the subject is the value
        values.add(re.sub(r"^(please\s+)?forget\s+(that\s+)?", "", subject.strip(), flags=re.I))
    # Whole key chains go, so superseded versions of the same fact are deleted too.
    keys = {by_id[i]["subject"] for i in ids}
    if keys:
        q = ",".join("?" * len(keys))
        for r in db.conn().execute(f"SELECT id, value FROM facts WHERE user_id = ? AND subject IN ({q})",
                                   (user_id, *keys)):
            ids.add(r["id"])
            values.add(r["value"])
    return ids, {v for v in values if len(v) >= 3 and v.casefold() not in _STOP}


def _find_in_messages(user_id: str, subject: str, terms: list[str], thread_id: str | None) -> set[str]:
    from chatbot.memory.retrieval import fts_query
    q = fts_query(" ".join(terms) or subject)
    if not q:
        return set()
    rows = db.conn().execute(
        "SELECT m.content FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
        " WHERE messages_fts MATCH ? AND m.user_id = ? AND m.role = 'user' ORDER BY bm25(messages_fts) LIMIT 15",
        (q, user_id),
    ).fetchall()
    texts = [r["content"][:400] for r in rows]
    if not texts:
        return set()
    try:
        obj = ask_json(_FIND, f"FORGET REQUEST: {subject}\n\nMESSAGES:\n" + "\n".join(f"- {t}" for t in texts),
                       "forget_find", thread_id) or {}
    except Exception as e:
        log("forget_find", type(e).__name__)
        return set()
    blob = "\n".join(texts).casefold()
    # Only accept strings that really occur in the messages: no hallucinated tombstones.
    return {str(v).strip() for v in obj.get("values") or [] if str(v).strip().casefold() in blob}


# --- purge ---------------------------------------------------------------------------------------

def forget(user_id: str, subject: str, thread_id: str | None = None) -> str:
    """Forget a subject ("my address") or literal value. Returns a status line that never
    repeats the value."""
    subject = (subject or "").strip()
    if not subject:
        return "error: say what to forget"
    ids, values = _resolve(user_id, subject, thread_id)
    if not ids and not values:
        return "Nothing stored matches that request; nothing was deleted."
    patterns = [_pattern(v) for v in values]
    labels = sorted({r["subject"] for r in db.conn().execute(
        f"SELECT subject FROM facts WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))}) if ids else []
    counts, rethread = _purge(user_id, ids, values, patterns)
    from chatbot.memory import summary  # lazy: summary imports this module
    for tid in rethread:
        try:
            summary.maybe_roll_summary(user_id, tid, force=True)
        except Exception as e:
            log("forget_reroll", type(e).__name__)
    compact()
    traces = [h for v in values for h in verify_clean(v)]
    what = ", ".join(labels) if labels else f"{len(values)} value(s) from history"
    status = "verified clean" if not traces else f"WARNING {len(traces)} trace(s) remain: {sorted(set(traces))}"
    return (f"Forgotten: {what}. Removed {counts['facts']} fact(s); redacted {counts['messages']} message(s), "
            f"{counts['summaries']} summary(ies), {counts['doc_notes']} doc note(s), {counts['send_log']} sent "
            f"message(s). Storage {status}. Do not repeat or guess the forgotten value.")


def _purge(user_id, ids, values, patterns) -> tuple[dict, set[str]]:
    c = db.conn()
    counts = dict.fromkeys(("facts", "messages", "summaries", "doc_notes", "send_log"), 0)
    rethread = set()
    c.execute("PRAGMA secure_delete = ON")
    with c:
        for r in c.execute("SELECT id, value FROM facts WHERE user_id = ?", (user_id,)).fetchall():
            if any(p.search(r["value"]) for p in patterns):
                ids.add(r["id"])
        if ids:
            q = ",".join("?" * len(ids))
            c.execute(f"UPDATE facts SET superseded_by = NULL WHERE superseded_by IN ({q})", tuple(ids))
            counts["facts"] = c.execute(f"DELETE FROM facts WHERE id IN ({q})", tuple(ids)).rowcount
        for v in values:
            # LIKE prefilter on the longest token; abbreviable tokens ("st") scan all, regex decides.
            anchor = max((t for _, _, t in _spans(v)), key=len, default="")
            anchor = "" if anchor in _ALTS else anchor
            for r in c.execute("SELECT id, content FROM messages WHERE user_id = ? AND content LIKE ?",
                               (user_id, f"%{anchor}%")).fetchall():
                new = _scrub(r["content"], patterns)
                if new != r["content"]:  # UPDATE trigger keeps messages_fts in sync
                    c.execute("UPDATE messages SET content = ? WHERE id = ?", (new, r["id"]))
                    counts["messages"] += 1
        for r in c.execute("SELECT id, thread_id, content FROM summaries WHERE user_id = ?", (user_id,)).fetchall():
            if _scrub(r["content"], patterns) != r["content"]:
                c.execute("DELETE FROM summaries WHERE id = ?", (r["id"],))
                rethread.add(r["thread_id"])
                counts["summaries"] += 1
        for r in c.execute("SELECT id, content FROM doc_notes WHERE user_id = ?", (user_id,)).fetchall():
            new = _scrub(r["content"], patterns)
            if new != r["content"]:
                c.execute("UPDATE doc_notes SET content = ? WHERE id = ?", (new, r["id"]))
                counts["doc_notes"] += 1
        for r in c.execute("SELECT id, recipient, text FROM send_log WHERE user_id = ?", (user_id,)).fetchall():
            text, rcpt = _scrub(r["text"], patterns), _scrub(r["recipient"], patterns)
            if (text, rcpt) != (r["text"], r["recipient"]):
                c.execute("UPDATE send_log SET text = ?, recipient = ? WHERE id = ?", (text, rcpt, r["id"]))
                counts["send_log"] += 1
        c.executemany("INSERT OR IGNORE INTO forgotten (user_id, digest, ntokens) VALUES (?, ?, ?)",
                      [(user_id, _digest(t), len(t)) for v in values for t in _variants(v)])
        # Rebuild drops old index segments that still held the deleted terms.
        c.execute("INSERT INTO messages_fts (messages_fts) VALUES ('rebuild')")
    return counts, rethread


def compact() -> None:
    """R-1: rewrite the file so freed pages, FTS segments and WAL frames lose the old bytes."""
    c = db.conn()
    if c.in_transaction:
        c.commit()
    c.execute("VACUUM")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()


def verify_clean(value: str) -> list[str]:
    """Sweep every table (incl. FTS shadow tables), the FTS term index, the db/WAL files and the
    call log for the value. Returns where traces were found; [] means clean."""
    c = db.conn()
    pat = _pattern(value)
    hits = []
    tables = c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
                       " AND name NOT LIKE 'sqlite_%'").fetchall()
    for (name,) in tables:
        ident = '"' + name.replace('"', '""') + '"'  # names come from sqlite_master, not user input
        for row in c.execute(f"SELECT * FROM {ident}").fetchall():
            for col, cell in zip(row.keys(), row):
                if isinstance(cell, bytes):
                    cell = cell.decode("utf-8", "ignore")
                if isinstance(cell, str) and pat.search(cell):
                    hits.append(f"{name}.{col}")
    words = [w.casefold() for _, _, w in _spans(value) if len(w) > 2 and w not in _CANON.values()]
    if words:
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.messages_fts_vocab USING fts5vocab(main, 'messages_fts', 'row')")
        q = ",".join("?" * len(words))
        present = c.execute(f"SELECT COUNT(DISTINCT term) FROM temp.messages_fts_vocab WHERE term IN ({q})",
                            tuple(words)).fetchone()[0]
        # ponytail: all-terms-present heuristic; common words of the value elsewhere can false-positive.
        if present == len(set(words)):
            hits.append("messages_fts index terms")
    raw = re.compile(pat.pattern.encode(), re.I)
    for path in (config.DB_PATH, config.DB_PATH.with_name(config.DB_PATH.name + "-wal"), config.CALL_LOG_PATH):
        # ponytail: reads whole files; fine for a single-user SQLite file of a few MB.
        if path.is_file() and raw.search(path.read_bytes()):
            hits.append(f"file:{path.name}")
    return hits
