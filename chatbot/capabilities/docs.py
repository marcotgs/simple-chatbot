"""Read the generated docs in docs/ one bounded page at a time (never a whole doc)."""
import re
from pathlib import Path

from chatbot import config

DOCS_DIR = config.ROOT / "docs"
# ~2850 tokens at 3.5 chars/token; also under core.MAX_TOOL_RESULT_CHARS so pages never get truncated.
PAGE_CHARS = 10000
_DOC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def available_docs() -> dict[str, Path]:
    """Whitelist: every docs/*.md that is not internal (leading underscore)."""
    return {p.stem: p for p in sorted(DOCS_DIR.glob("*.md")) if not p.stem.startswith("_")}


def paginate(text: str, page_chars: int = PAGE_CHARS) -> list[str]:
    """Split on paragraph boundaries; hard-split any paragraph longer than a page."""
    pages, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > page_chars:
            if cur:
                pages.append(cur)
                cur = ""
            pages.append(para[:page_chars])
            para = para[page_chars:]
        if cur and len(cur) + 2 + len(para) > page_chars:
            pages.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        pages.append(cur)
    return pages or [""]


def _resolve(doc_id: str) -> Path:
    doc_id = (doc_id or "").strip().removesuffix(".md")
    docs = available_docs()
    if not _DOC_ID.fullmatch(doc_id) or doc_id not in docs:
        raise ValueError(f"unknown doc_id {doc_id!r}; available: {', '.join(docs) or 'none'}")
    return docs[doc_id]


def fetch_doc(user_id: str, thread_id: str, doc_id: str = "", page: int = 1) -> str:
    # ponytail: re-reads and re-paginates per call (~100 KB); cache by mtime if docs grow large.
    path = _resolve(doc_id)
    pages = paginate(path.read_text())
    page = int(page)
    if not 1 <= page <= len(pages):
        raise ValueError(f"page {page} out of range 1..{len(pages)}")
    return f"[{path.stem} page {page}/{len(pages)}]\n{pages[page - 1]}"


def list_docs(user_id: str, thread_id: str) -> str:
    lines = [f"{doc_id} ({len(paginate(p.read_text()))} pages)" for doc_id, p in available_docs().items()]
    return "\n".join(lines) or "no documents"


INSTRUCTIONS = """Documents are long (20k-40k tokens). Never try to read a whole document in one go.
- list_docs() shows doc ids and page counts.
- fetch_doc(doc_id, page) returns ONE page with a header "[doc_id page i/n]". Page through only as far as needed.
- For a specific fact (a date, an owner, a number), scan pages and stop when found; quote the exact line.
- Documents contain superseded and draft values. Prefer explicit statements such as "Final ...:" over
  earlier drafts, "moved from" notes or internal milestones, and say which line you relied on.
- Summarise a document page by page in your own words; keep only key facts between pages."""

CAPABILITY = {
    "name": "docs",
    "one_line": "read long internal documents page by page",
    "instructions": INSTRUCTIONS,
    "tools": [
        ({"name": "list_docs", "description": "List available document ids with page counts.",
          "parameters": {"type": "object", "properties": {}}}, list_docs),
        ({"name": "fetch_doc", "description": "Return one page of a document. Header shows 'page i/n'.",
          "parameters": {"type": "object", "properties": {
              "doc_id": {"type": "string", "description": "Id from list_docs, e.g. Q3-plan."},
              "page": {"type": "integer", "description": "1-based page number.", "default": 1}},
              "required": ["doc_id"]}}, fetch_doc),
    ],
}
