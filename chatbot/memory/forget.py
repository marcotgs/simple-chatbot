"""Forget: purge a value from every store, keep only a keyed tombstone, guard outputs.

Matching works on normalized token sequences: NFKC + casefold (so 'Hauptstraße' == 'hauptstrasse',
'Émile' == 'émile' in NFC or NFD), punctuation/whitespace ignored, street-type abbreviations
canonicalized. The same tokenizer drives scrubbing, verify_clean and the output guard, so the
three cannot disagree. Every value is expanded into _variants() (full value, specific comma parts,
street without house number) and each variant is scrubbed, tombstoned and verified.

Safety rails (forget is irreversible and model-triggered, tombstones are global): a value needs
>= 3 alphanumeric chars and must occur verbatim (normalized) in this user's stored facts or user
messages. Generic values (a year, a house number, a 1-token word under 4 chars) are never scrubbed
or tombstoned; their fact rows are still deleted. A model/literal value must be a whole stored fact
value (1 token), occur inside a stored fact value or contain a digit (2 tokens), or have >= 3
tokens.
Runs of digit tokens also match their joined form ('555-0142' == '(555) 0142' == '5550142').

Tombstones are HMAC-SHA256 digests of the token sequence. Key: env FORGET_SECRET, else a random
key created once in `forget.key` (mode 0600) next to the DB. Limits: with the generated key, whoever
holds the data directory (copied volume, snapshot) holds the key too and can brute-force short
values (phones, house numbers); set FORGET_SECRET outside the volume to prevent that. Losing or
changing the key silently disables the output guard for older tombstones. Tombstones are global
(not per user): one user's forgotten value is redacted for every user (single-owner app).
"""

import functools
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import unicodedata

from chatbot import config, db
from chatbot.capabilities import docs
from chatbot.memory._llm import ask_json, log

REDACTED = "[forgotten]"
# Word chars plus combining marks, so decomposed (NFD) 'É' stays inside its token.
_TOKEN = re.compile(r"(?:[^\W_]|[̀-ͯ])+")
_CANON = {
    "street": "st",
    "avenue": "ave",
    "road": "rd",
    "apartment": "apt",
    "boulevard": "blvd",
    "drive": "dr",
    "lane": "ln",
    "suite": "ste",
    "place": "pl",
    "court": "ct",
    "number": "no",
}
_ALTS: dict[str, set[str]] = {}
for _long, _short in _CANON.items():
    _ALTS.setdefault(_short, {_short}).add(_long)
_STOP = {
    "my",
    "the",
    "forget",
    "please",
    "about",
    "that",
    "this",
    "what",
    "where",
    "and",
    "all",
    "me",
    "i",
    "you",
    "it",
    "is",
    "of",
    "a",
    "an",
    "to",
    "info",
    "information",
    "everything",
    "details",
}
MIN_ALNUM = 3
MIN_DIGITS = 7  # an all-digit value shorter than a phone number (year, house number) is generic


def _canon(tok: str) -> str:
    t = unicodedata.normalize("NFKC", tok).casefold()
    return _CANON.get(t, t)


def _spans(text: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), _canon(m.group())) for m in _TOKEN.finditer(text or "")]


def _words(text: str) -> tuple[str, ...]:
    return tuple(t for _, _, t in _spans(text))


def content_words(text: str) -> list[str]:
    """Words of a forget subject that name something (no stopwords, > 2 chars)."""
    return [w for w in _words(text) if w not in _STOP and len(w) > 2]


@functools.cache
def _key() -> bytes:
    env = os.environ.get("FORGET_SECRET", "")
    if env:
        return env.encode()
    path = config.DB_PATH.parent / "forget.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        key = secrets.token_bytes(32)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        return key
    key = path.read_bytes()
    if len(key) < 32:  # never regenerate: older tombstones would silently stop matching
        raise RuntimeError(
            f"{path} is truncated ({len(key)} bytes); restore it or set FORGET_SECRET"
        )
    return key


def _digest(tokens) -> str:
    return hmac.new(_key(), " ".join(tokens).encode(), hashlib.sha256).hexdigest()


def _variants(value: str) -> list[tuple[str, ...]]:
    """Full value, comma/line parts specific enough to identify it alone, and street parts
    without the leading house number ('742 Evergreen Terrace' -> 'evergreen terrace')."""
    out: list[tuple[str, ...]] = []

    def add(toks, specific):
        if toks and specific and toks not in out:
            out.append(toks)

    full = _words(value)
    add(full, True)
    if len(full) > 1 and all(t.isdigit() for t in full):
        add(("".join(full),), True)  # '555-0142' also as '5550142'
    for part in [value] + re.split(r"[,;\n]", value):
        toks = _words(part)
        add(
            toks, len(toks) >= 3 or (len(toks) >= 2 and any(ch.isdigit() for t in toks for ch in t))
        )
        # ponytail: also redacts that street name elsewhere ('Main St'); privacy over precision.
        rest = toks[1:]
        if toks and toks[0].isdigit() and len(rest) >= 2 and any(t not in _ALTS for t in rest):
            add(rest, True)
    return out


def _by_len(seqs) -> dict[int, set]:
    out: dict[int, set] = {}
    for s in seqs:
        out.setdefault(len(s), set()).add(s)
    return out


def _windows(text: str, lengths):
    """(start, end, token tuple) for every token window of the given lengths."""
    toks = _spans(text)
    words = [t for _, _, t in toks]
    for n in lengths:
        for i in range(len(toks) - n + 1):
            yield toks[i][0], toks[i + n - 1][1], tuple(words[i : i + n])
    if 1 in lengths:  # digit-normalised form: '(555) 0142' / '555-0142' also match '5550142'
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                if not (words[i].isdigit() and words[j].isdigit()):
                    break
                yield toks[i][0], toks[j][1], ("".join(words[i : j + 1]),)


def _seq_spans(text: str, by_len: dict[int, set]) -> list[tuple[int, int]]:
    return [(s, e) for s, e, w in _windows(text, by_len) if w in by_len[len(w)]]


def _redact(text: str, spans) -> str:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + REDACTED + text[end:]
    return text


def _match_spans(text: str) -> list[tuple[int, int]]:
    rows = db.conn().execute("SELECT digest, ntokens FROM forgotten").fetchall()
    if not rows or not text:
        return []
    by_len: dict[int, set[str]] = {}
    for r in rows:
        by_len.setdefault(r["ntokens"], set()).add(r["digest"])
    return [(s, e) for s, e, w in _windows(text, by_len) if _digest(w) in by_len[len(w)]]


def contains_forgotten(text: str) -> bool:
    """True if text contains any forgotten value (case/Unicode/punctuation-insensitive)."""
    return bool(_match_spans(text))


def redact_forgotten(text: str) -> str:
    """Replace every forgotten value in text with a placeholder."""
    return _redact(text, _match_spans(text))


def _scrub(text: str, by_len: dict[int, set]) -> str:
    return _redact(text, _seq_spans(text, by_len)) if text else text


# --- resolution: what does "forget X" refer to? -------------------------------------------------

_RESOLVE = """The user asked the assistant to forget something. Decide exactly what stored data must be deleted.
Return ONLY JSON: {"fact_ids": [ids], "values": ["..."], "search_terms": ["..."]}
- fact_ids: every stored fact about that subject, including old versions.
- values: the literal sensitive data to scrub from history, copied verbatim (e.g. the address itself), not the subject name.
- search_terms: words likely to appear in old messages that mention it (e.g. "live", "address", "street").
Never include generic words in values."""  # noqa: E501

_FIND = """The user asked the assistant to forget something. Below are old messages that may mention it.
Return ONLY JSON: {"values": ["..."]} with the exact sensitive strings (copied verbatim from the messages)
that are the data to forget. Return {"values": []} if none of the messages contain it."""  # noqa: E501


def _valid(value: str) -> bool:
    """Real content only: '???' or '---' would match the empty token sequence everywhere."""
    words = _words(value)
    return sum(ch.isalnum() for t in words for ch in t) >= MIN_ALNUM and any(
        t not in _STOP for t in words
    )


def _specific(words: tuple[str, ...]) -> bool:
    """Specific enough to scrub from all history and tombstone for every user. Not: a year or house
    number ('2026', '742'), a short single word ('Bob')."""
    if all(t.isdigit() for t in words) and sum(map(len, words)) < MIN_DIGITS:
        return False
    return len(words) >= 2 or sum(ch.isalnum() for t in words for ch in t) >= MIN_ALNUM + 1


_FACT_SQL = "SELECT value AS t FROM facts WHERE user_id = ?"
_MSG_SQL = "SELECT content AS t FROM messages WHERE user_id = ? AND role = 'user'"


def _stored(user_id: str, candidates: set[str], sqls=(_FACT_SQL, _MSG_SQL)) -> set[str]:
    """Candidates whose full token sequence occurs in this user's fact values or user messages."""
    want = {v: _words(v) for v in candidates if _valid(v)}
    if not want:
        return set()
    by_len = _by_len(want.values())
    found: set[tuple] = set()
    c = db.conn()
    # ponytail: Python scan of every fact + user message of the user (bounded by one user's
    # history); add a normalized shadow column if histories reach ~1e6 rows.
    for sql in sqls:
        for r in c.execute(sql, (user_id,)):
            found.update(w for _, _, w in _windows(r["t"], by_len) if w in by_len[len(w)])
    return {v for v, w in want.items() if w in found}


def _resolve(user_id: str, subject: str, thread_id: str | None) -> tuple[set[int], set[str]]:
    c = db.conn()
    rows = c.execute(
        "SELECT id, subject, value FROM facts WHERE user_id = ? ORDER BY id DESC LIMIT 150",
        (user_id,),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    listing = "\n".join(f"{r['id']} | {r['subject']} | {r['value'][:120]}" for r in rows)[:5000]
    ids, values, terms = set(), set(), []
    try:
        obj = (
            ask_json(
                _RESOLVE,
                f"FORGET REQUEST: {subject}\n\nSTORED FACTS (id | key | "
                f"value):\n{listing or '(none)'}",
                "forget_resolve",
                thread_id,
                model=config.JUDGE_MODEL,
            )
            or {}
        )
        ids = {int(i) for i in obj.get("fact_ids") or [] if str(i).isdigit() and int(i) in by_id}
        values = {str(v).strip() for v in obj.get("values") or [] if isinstance(v, (str, int))}
        terms = [str(t) for t in obj.get("search_terms") or [] if isinstance(t, str)]
    except Exception as e:  # model down: fall back to key matching below
        log("forget_resolve", type(e).__name__)
    words = content_words(subject)
    if not ids and not values:
        # Deletion is irreversible: only ONE key holding every subject word, else nothing.
        hit = {r["subject"] for r in rows if words and set(words) <= set(_words(r["subject"]))}
        ids = {r["id"] for r in rows if r["subject"] in hit} if len(hit) == 1 else set()
    # Always also look in messages: the value may be phrased differently there than in the fact.
    values |= _find_in_messages(user_id, subject, terms + words, thread_id)
    literal = set()
    if any(
        ch.isdigit() for ch in subject
    ):  # "forget 42 Example Road": the subject may be the value
        literal.add(re.sub(r"^(please\s+)?forget\s+(that\s+)?", "", subject.strip(), flags=re.I))
    # Whole key chains go, so superseded versions of the same fact are deleted too.
    keys = {by_id[i]["subject"] for i in ids}
    fact_values = set()
    if keys:
        q = ",".join("?" * len(keys))
        for r in c.execute(
            f"SELECT id, value FROM facts WHERE user_id = ? AND subject IN ({q})", (user_id, *keys)
        ):
            ids.add(r["id"])
            fact_values.add(r["value"])
    # W-SEC1: tombstones are global, so model-supplied and literal values must be specific and
    # exist in stored data: 1 token only as a whole fact value ("Mochi"); 2 tokens only inside a
    # fact value or with a digit ("555-0142"), never a subject phrase ("launch date").
    whole = {_words(r["t"]) for r in c.execute(_FACT_SQL, (user_id,))}
    in_facts = _stored(user_id, values | literal, (_FACT_SQL,))

    def ok(v: str) -> bool:
        w = _words(v)
        if not _specific(w):
            return False
        if len(w) == 1:
            return w in whole
        if len(w) == 2:  # "Hauptstraße 5", "555-0142" yes; "launch date" no
            return v in in_facts or any(ch.isdigit() for t in w for ch in t)
        return True

    accepted = {v for v in _stored(user_id, values | literal) if ok(v)}
    return ids, {v for v in fact_values if _valid(v) and _specific(_words(v))} | accepted


def _find_in_messages(
    user_id: str, subject: str, terms: list[str], thread_id: str | None
) -> set[str]:
    from chatbot.memory.retrieval import fts_query

    q = fts_query(" ".join(terms) or subject)
    if not q:
        return set()
    rows = (
        db.conn()
        .execute(
            "SELECT m.content FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
            " WHERE messages_fts MATCH ? AND m.user_id = ? AND m.role = 'user' ORDER BY "
            "bm25(messages_fts) LIMIT 15",
            (q, user_id),
        )
        .fetchall()
    )
    texts = [r["content"][:400] for r in rows]
    if not texts:
        return set()
    try:
        obj = (
            ask_json(
                _FIND,
                f"FORGET REQUEST: {subject}\n\nMESSAGES:\n" + "\n".join(f"- {t}" for t in texts),
                "forget_find",
                thread_id,
                model=config.JUDGE_MODEL,
            )
            or {}
        )
    except Exception as e:
        log("forget_find", type(e).__name__)
        return set()
    # Occurrence in stored data is checked by _resolve (_stored), same as resolve values.
    return {str(v).strip() for v in obj.get("values") or [] if isinstance(v, (str, int))}


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
    labels = (
        sorted(
            {
                r["subject"]
                for r in db.conn().execute(
                    f"SELECT subject FROM facts WHERE id IN ({','.join('?' * len(ids))})",
                    tuple(ids),
                )
            }
        )
        if ids
        else []
    )
    counts = _purge(user_id, ids, values)
    notes = []
    try:
        compact()
    except sqlite3.Error as e:  # purge is already committed; report honestly instead of failing
        log("forget_compact", type(e).__name__)
        notes.append(
            "compaction failed, so freed pages/WAL may still hold old bytes until the next VACUUM"
        )
    if not values:
        status = "not scanned (no literal value to search for; only fact rows were deleted)"
    else:
        try:
            traces = verify_clean(*values)
            status = (
                "verified clean"
                if not traces
                else f"WARNING {len(traces)} trace(s) remain: {sorted(set(traces))}"
            )
        except (sqlite3.Error, OSError) as e:
            log("forget_verify", type(e).__name__)
            status = "NOT verified (verification sweep failed)"
    if notes:
        status += "; " + "; ".join(notes)
    what = ", ".join(labels) if labels else f"{len(values)} value(s) from history"
    return (
        f"Forgotten: {what}. Removed {counts['facts']} fact(s); redacted {counts['messages']} "
        "message(s), "
        f"{counts['summaries']} summary(ies), {counts['doc_notes']} doc note(s), "
        f"{counts['send_log']} sent message(s), {counts['uploads']} uploaded file(s). "
        f"Storage {status}. Do not repeat or guess the forgotten value."
    )


def _purge(user_id: str, ids: set[int], values: set[str]) -> dict:
    c = db.conn()
    variants = {t for v in values for t in _variants(v)}
    by_len = _by_len(variants)
    counts = dict.fromkeys(
        ("facts", "messages", "summaries", "doc_notes", "send_log", "uploads"), 0
    )
    c.execute("PRAGMA secure_delete = ON")

    def rewrite(table: str, cols: tuple[str, ...]):
        if not by_len:
            return
        updates = []
        for r in c.execute(
            f"SELECT id, {', '.join(cols)} FROM {table} WHERE user_id = ?", (user_id,)
        ).fetchall():
            new = tuple(_scrub(r[col], by_len) for col in cols)
            if new != tuple(r[col] for col in cols):
                updates.append((*new, r["id"]))
        if updates:  # messages UPDATE trigger keeps messages_fts in sync
            c.executemany(
                f"UPDATE {table} SET {', '.join(f'{col} = ?' for col in cols)} WHERE id = ?",
                updates,
            )
        counts[table] = len(updates)

    with c:
        if by_len:
            for r in c.execute(
                "SELECT id, value FROM facts WHERE user_id = ?", (user_id,)
            ).fetchall():
                if _seq_spans(r["value"], by_len):
                    ids.add(r["id"])
        if ids:
            q = ",".join("?" * len(ids))
            c.execute(
                f"UPDATE facts SET superseded_by = NULL WHERE superseded_by IN ({q})", tuple(ids)
            )
            counts["facts"] = c.execute(f"DELETE FROM facts WHERE id IN ({q})", tuple(ids)).rowcount
        # Tables are constants here, never user input. Summaries are scrubbed in place (no re-roll
        # calls).
        rewrite("messages", ("content",))
        rewrite("summaries", ("content",))
        rewrite("doc_notes", ("content",))
        rewrite("send_log", ("text", "recipient"))
        c.executemany(
            "INSERT OR IGNORE INTO forgotten (user_id, digest, ntokens) VALUES (?, ?, ?)",
            [(user_id, _digest(t), len(t)) for t in variants],
        )
        # Rebuild drops old index segments that still held the deleted terms.
        c.execute("INSERT INTO messages_fts (messages_fts) VALUES ('rebuild')")
    # Uploaded files are a store too: fetch_doc would serve the value again, and list_docs shows
    # the file name, so a name holding the value is renamed (its doc notes follow).
    # ponytail: rewritten in place; the filesystem may keep old blocks (no secure erase).
    for path in docs.uploaded_docs(user_id).values() if by_len else ():
        text = path.read_text(encoding="utf-8")
        new = _scrub(text, by_len)
        if new != text:
            path.write_text(new, encoding="utf-8")
        if _seq_spans(path.stem, by_len):
            # Random id: a hash of the stem would let a guess of the value be confirmed. A taken
            # id is skipped so another upload is never overwritten.
            target = path
            while target.exists():
                new_id = f"{docs.UPLOAD_PREFIX}forgotten-{secrets.token_hex(4)}"
                target = path.with_name(f"{new_id}.md")
            path.replace(target)
            with c:
                c.execute(
                    "UPDATE doc_notes SET doc_id = ? WHERE user_id = ? AND doc_id = ?",
                    (new_id, user_id, path.stem),
                )
        elif new == text:
            continue
        counts["uploads"] += 1
    return counts


def compact() -> None:
    """R-1: rewrite the file so freed pages, FTS segments and WAL frames lose the old bytes."""
    c = db.conn()
    if c.in_transaction:
        c.commit()
    c.execute("VACUUM")
    busy = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy and busy[0]:
        raise sqlite3.OperationalError("wal_checkpoint busy")


def _fold(tok: str) -> str:
    """FTS5 unicode61 view of a token: diacritics removed, lowercased."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", tok) if not unicodedata.combining(ch))


def verify_clean(*values: str) -> list[str]:
    """Sweep every table (incl. FTS shadow tables), the FTS term index, the db/WAL files, the
    call log and every uploaded file for every variant of the values. Returns where traces were
    found; [] means clean."""
    c = db.conn()
    variants = {t for v in values for t in _variants(v)}
    if not variants:
        raise ValueError("verify_clean needs at least one value with real tokens")
    by_len = _by_len(variants)
    hits = []
    tables = c.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE 'CREATE VIRTUAL%'"
        " AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (name,) in tables:
        ident = '"' + name.replace('"', '""') + '"'  # names come from sqlite_master, not user input
        for row in c.execute(f"SELECT * FROM {ident}").fetchall():
            for col, cell in zip(row.keys(), row, strict=True):
                if isinstance(cell, bytes):
                    cell = cell.decode("utf-8", "ignore")
                if isinstance(cell, str) and _seq_spans(cell, by_len):
                    hits.append(f"{name}.{col}")
    c.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.messages_fts_vocab USING fts5vocab(main, "
        "'messages_fts', 'row')"
    )
    vocab = {_fold(_canon(r[0])) for r in c.execute("SELECT term FROM temp.messages_fts_vocab")}
    for v in variants:
        # ponytail: all-terms-present heuristic; common words of the value elsewhere can
        # false-positive.
        words = {_fold(w) for w in v if len(w) > 2 and w not in _ALTS}
        if words and words <= vocab:
            hits.append("messages_fts index terms")
            break
    for path in (
        config.DB_PATH,
        config.DB_PATH.with_name(config.DB_PATH.name + "-wal"),
        config.CALL_LOG_PATH,
        *docs.UPLOADS_DIR.glob("*/*"),
    ):
        # ponytail: reads whole files; fine for a single-user SQLite file of a few MB.
        # The name is scanned too (an upload named after the value); the hit label is redacted.
        if path.is_file() and _seq_spans(
            path.name + "\n" + path.read_bytes().decode("utf-8", "ignore"), by_len
        ):
            hits.append(f"file:{_scrub(path.name, by_len)}")
    return hits
