"""Per-page notes + key facts for long documents, so later questions never re-read the doc."""
import re

from chatbot import db
from chatbot.memory._llm import ask_json, log
from chatbot.memory.forget import redact_forgotten
from chatbot.memory.retrieval import STOPWORDS

PAGE_MAX_CHARS = 12000      # ~3.4k tokens of page text per call
SEARCH_MAX_CHARS = 2500     # ~700 tokens
OVERVIEW_MAX_CHARS = 6000   # ~1.7k tokens

_PROMPT = """You take notes on ONE page of a long document so later questions can be answered without re-reading it.
Return ONLY JSON: {"summary": "...", "key_facts": ["..."]}
- summary: at most 120 words on what this page covers.
- key_facts: every date, deadline, owner/person, number, amount, metric and decision on the page, one self-contained line each
  that names what it is, e.g. "Launch date (public launch): 2026-11-03", "Owner of billing migration: Dana Kim".
  Keep each date with its exact context and distinguish similar ones (launch vs beta vs review vs freeze dates). At most 25."""


def note_page(user_id: str, thread_id: str | None, doc_id: str, page: int, text: str) -> dict:
    """Summarise one page (one model call) and replace its stored notes. Returns the notes."""
    obj = ask_json(_PROMPT, f"DOCUMENT {doc_id}, PAGE {page}:\n{(text or '')[:PAGE_MAX_CHARS]}",
                   "doc_note", thread_id) or {}
    summary = str(obj.get("summary") or "").strip()[:1500]
    raw_facts = obj.get("key_facts") if isinstance(obj.get("key_facts"), list) else []
    key_facts = [redact_forgotten(str(f).strip())[:300] for f in raw_facts[:25] if str(f).strip()]
    if not summary and not key_facts:
        log("doc_note", f"no notes for page {page}")
        summary = "(notes unavailable for this page; re-read it with fetch_doc)"
    summary = redact_forgotten(summary)
    c = db.conn()
    with c:
        c.execute("DELETE FROM doc_notes WHERE user_id = ? AND doc_id = ? AND page = ?", (user_id, doc_id, page))
        c.executemany(
            "INSERT INTO doc_notes (user_id, thread_id, doc_id, page, kind, content) VALUES (?, ?, ?, ?, ?, ?)",
            [(user_id, thread_id, doc_id, page, "summary", summary)]
            + [(user_id, thread_id, doc_id, page, "key_fact", f) for f in key_facts],
        )
    return {"summary": summary, "key_facts": key_facts}


def noted_pages(user_id: str, doc_id: str) -> set[int]:
    rows = db.conn().execute("SELECT DISTINCT page FROM doc_notes WHERE user_id = ? AND doc_id = ?",
                             (user_id, doc_id)).fetchall()
    return {r["page"] for r in rows}


def noted_docs(user_id: str, limit: int = 10) -> list[str]:
    rows = db.conn().execute("SELECT doc_id FROM doc_notes WHERE user_id = ? GROUP BY doc_id"
                             " ORDER BY MAX(id) DESC LIMIT ?", (user_id, limit)).fetchall()
    return [r["doc_id"] for r in rows]


def search_doc_notes(user_id: str, query: str, doc_id: str | None = None, limit: int = 12) -> str:
    """Notes ranked by query-word overlap (key facts first on ties), capped."""
    words = {w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 1 and w not in STOPWORDS}
    sql, params = "SELECT doc_id, page, kind, content FROM doc_notes WHERE user_id = ?", [user_id]
    if doc_id:
        sql += " AND doc_id = ? COLLATE NOCASE"
        params.append(doc_id)
    # ponytail: Python scoring over all notes of the user; add FTS5 on doc_notes past ~10k notes.
    rows = db.conn().execute(sql + " ORDER BY doc_id, page, id LIMIT 5000", params).fetchall()
    scored = []
    for r in rows:
        text = r["content"].lower()
        score = sum(1 for w in words if w in text) + (0.5 if r["kind"] == "key_fact" else 0)
        if score >= 1:
            scored.append((score, r))
    if not scored:
        return "No matching document notes." + (f" Known documents: {', '.join(noted_docs(user_id))}."
                                                if noted_docs(user_id) else "")
    scored.sort(key=lambda s: -s[0])
    out = "\n".join(f"[{r['doc_id']} p{r['page']} {r['kind']}] {r['content']}" for _, r in scored[:limit])
    return out if len(out) <= SEARCH_MAX_CHARS else out[:SEARCH_MAX_CHARS - 12] + " [truncated]"


def doc_overview(user_id: str, doc_id: str) -> str:
    """Page summaries and key facts, each half of the cap so neither crowds out the other."""
    rows = db.conn().execute("SELECT page, kind, content FROM doc_notes WHERE user_id = ? AND doc_id = ? COLLATE NOCASE"
                             " ORDER BY page, id", (user_id, doc_id)).fetchall()
    if not rows:
        return f"No notes for document {doc_id!r}."
    half = OVERVIEW_MAX_CHARS // 2
    parts = []
    for kind, title in (("summary", "Page summaries"), ("key_fact", "Key facts")):
        body = "\n".join(f"[p{r['page']}] {r['content']}" for r in rows if r["kind"] == kind)
        parts.append(f"{title}:\n" + (body if len(body) <= half else body[:half - 12] + " [truncated]"))
    return "\n\n".join(parts)


def summarize_doc(user_id: str, thread_id: str | None, doc_id: str, read_page=None, page_count=None) -> str:
    """Map step over every page (one bounded call each, cached per page), then the overview.
    read_page(doc_id, page)->str and page_count(doc_id)->int default to chatbot.capabilities.docs."""
    if read_page is None or page_count is None:
        try:
            from chatbot.capabilities.docs import page_count as _count, read_page as _read
        except ImportError as e:
            return f"error: document reader unavailable (chatbot.capabilities.docs: {e})"
        read_page, page_count = read_page or _read, page_count or _count
    n = int(page_count(doc_id))
    done = noted_pages(user_id, doc_id)
    for page in range(1, n + 1):
        if page not in done:
            note_page(user_id, thread_id, doc_id, page, read_page(doc_id, page))
    return f"Notes for {doc_id} ({n} pages):\n" + doc_overview(user_id, doc_id)
