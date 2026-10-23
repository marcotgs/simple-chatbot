"""LinkedIn posts: deterministic rule checker + a drafting loop that only returns passing posts."""

import re
from urllib.parse import quote

from chatbot import db, llm

MAX_CHARS = 1300
MAX_HASHTAGS = 3
MAX_REGENERATIONS = 3  # after the first draft
MAX_FACTS_CHARS = 2000
DRAFT_LABEL = "Open in LinkedIn (draft)"
DRAFT_URL = "https://www.linkedin.com/feed/?shareActive=true&text="
MAX_URL_CHARS = 2000  # longer URLs get cut by browsers/proxies; fall back to copy-paste

# Last post that passed the rules in the running turn, per (user_id, thread_id). core.send clears
# it when a turn starts and pops it when the turn ends; the per-thread lock in send serialises it.
_drafts: dict[tuple[str, str], str] = {}


def draft_line(post: str) -> str:
    """Reply line that opens LinkedIn's composer prefilled with post (nothing is posted). Built
    here, never by the model, so the URL cannot be mangled."""
    url = DRAFT_URL + quote(post, safe="")
    if len(url) > MAX_URL_CHARS:
        return "The post is too long for a LinkedIn draft link: copy the text above."
    return f"{DRAFT_LABEL}: {url}"


def strip_draft_lines(reply: str) -> str:
    """Drop draft lines the model wrote itself (e.g. copied from an injected doc): only
    core.send appends the real one (R6-SEC-W1)."""
    lines = reply.splitlines()
    kept = [line for line in lines if not line.lstrip().startswith(DRAFT_LABEL)]
    return reply if len(kept) == len(lines) else "\n".join(kept).strip()


def shows_post(reply: str, post: str) -> bool:
    """True when the reply contains the post (whitespace-insensitive)."""
    return bool(post.split()) and " ".join(post.split()) in " ".join(reply.split())


def pop_draft(user_id: str, thread_id: str) -> str | None:
    return _drafts.pop((user_id, thread_id), None)


RULES = f"""- At most {MAX_CHARS} characters in total.
- At most {MAX_HASHTAGS} hashtags, and all hashtags go at the very end (nothing but hashtags after the first one).
- No links of any kind: no http:// or https://, no www., no bare domains like example.com."""  # noqa: E501

HASHTAG = re.compile(r"(?<![\w#/])#\w+")
# ponytail: bare domains matched against a fixed TLD list; extend the list if other TLDs slip
# through.
_LINKS = [
    ("contains an http(s):// link", re.compile(r"https?://", re.I)),
    ("contains a www. link", re.compile(r"\bwww\.", re.I)),
    ("contains a markdown link", re.compile(r"\[[^\]]*\]\([^)]*\)")),
    (
        "contains a bare domain",
        re.compile(
            r"\b[a-z0-9-]+\.(?:com|io|net|org|co|ai|dev|app|ly|me|gg|xyz|info|biz|tech|us|uk|so|to)\b",
            re.I,
        ),
    ),
]


def check_linkedin_post(text: str) -> list[str]:
    """Return rule violations; empty list means the post is valid."""
    text = text or ""
    violations = []
    if not text.strip():
        violations.append("post is empty")
    if len(text) > MAX_CHARS:
        violations.append(f"too long: {len(text)} characters (max {MAX_CHARS})")
    tags = list(HASHTAG.finditer(text))
    if len(tags) > MAX_HASHTAGS:
        violations.append(f"too many hashtags: {len(tags)} (max {MAX_HASHTAGS})")
    if tags and HASHTAG.sub("", text[tags[0].start() :]).strip(" \t\r\n,|"):
        violations.append(
            "hashtags must all be at the very end, with only hashtags after the first one"
        )
    violations += [msg for msg, rx in _LINKS if rx.search(text)]
    return violations


def generate_linkedin_post(brief: str, facts: str = "", thread_id: str | None = None) -> str:
    """Draft a post from the brief + key facts only; regenerate with the violations until it passes.
    Returns the post, or a string starting with 'error:'."""
    messages = [
        {
            "role": "system",
            "content": "You write LinkedIn posts. Rules:\n"
            + RULES
            + "\nReply with the post text only, no preamble, no quotes.",
        },
        {
            "role": "user",
            "content": f"Brief: {brief}" + (f"\n\nKey facts:\n{facts}" if facts else ""),
        },
    ]
    violations: list[str] = []
    for _ in range(1 + MAX_REGENERATIONS):
        try:
            resp = llm.chat(messages, purpose="linkedin", thread_id=thread_id)
        except llm.BudgetExceeded as e:
            return f"error: LinkedIn draft exceeded the token budget ({e}); shorten the brief"
        post = (resp.choices[0].message.content or "").strip()
        violations = check_linkedin_post(post)
        if not violations:
            return post
        messages += [
            {"role": "assistant", "content": post},
            {
                "role": "user",
                "content": "That draft breaks these rules: "
                + "; ".join(violations)
                + ". Rewrite it so every rule passes.",
            },
        ]
    return (
        f"error: could not produce a valid LinkedIn post after {1 + MAX_REGENERATIONS} attempts; "
        f"last violations: {'; '.join(violations)}"
    )


def _doc_key_facts(user_id: str, thread_id: str, doc_id: str, brief: str = "") -> str:
    """Key facts this thread already extracted for a doc (doc_notes), never the raw doc nor
    another thread's notes. Facts sharing words with the brief come first, then each page's
    facts in turn (its most important first), so every page is covered before any page gets a
    second fact; whole lines up to MAX_FACTS_CHARS."""
    from chatbot.memory.retrieval import STOPWORDS  # lazy: memory imports capabilities

    rows = (
        db.conn()
        .execute(
            "SELECT page, content FROM doc_notes WHERE user_id = ? AND thread_id = ? AND doc_id = ?"
            " AND kind = 'key_fact' ORDER BY page, id",
            (user_id, thread_id, doc_id),
        )
        .fetchall()
    )
    words = {w for w in re.findall(r"\w+", brief.lower()) if len(w) > 2 and w not in STOPWORDS}
    rank: dict[int, int] = {}
    keyed = []
    for r in rows:
        nth = rank[r["page"]] = rank.get(r["page"], -1) + 1
        hits = sum(w in r["content"].lower() for w in words)
        keyed.append(((-hits, nth, r["page"]), f"- {r['content']}"))
    out, size = [], 0
    for _, line in sorted(keyed):
        if size + len(line) + 1 > MAX_FACTS_CHARS:
            continue
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def write_linkedin_post(
    user_id: str, thread_id: str, brief: str = "", source_doc_id: str | None = None
) -> str:
    if not (brief or "").strip():
        return "error: brief is required"
    facts = ""
    if source_doc_id:
        doc_id = source_doc_id.strip().removesuffix(".md")
        from chatbot.memory import doc_notes  # lazy: memory imports capabilities

        # B9/R7-S2: note every page not noted yet (noted pages are skipped, so this is one
        # paginate when the doc is fully noted) instead of drafting from partial notes.
        doc_notes.summarize_doc(user_id, thread_id, doc_id)  # unknown doc -> ValueError
        facts = _doc_key_facts(user_id, thread_id, doc_id, brief)
        if not facts:
            return (
                f"error: no key facts could be read from {doc_id!r}, so no post was drafted. "
                "Put the facts in the brief, or check the doc with search_doc_notes or fetch_doc."
            )
    post = generate_linkedin_post(brief, facts, thread_id)
    if not post.startswith("error:"):
        _drafts[(user_id, thread_id)] = post
    return post


def validate_linkedin_post(user_id: str, thread_id: str, text: str = "") -> str:
    violations = check_linkedin_post(text)
    if violations:
        return "invalid: " + "; ".join(violations)
    _drafts[(user_id, thread_id)] = text
    return "valid"


INSTRUCTIONS = f"""LinkedIn post rules (enforced by code, not optional):
{RULES}
How to work:
- Use write_linkedin_post(brief, source_doc_id). Put every key fact you know (dates, names, numbers) in the brief.
  For a post about a document always pass source_doc_id: the drafter then also gets the doc's key facts from its
  notes (the app notes the doc first if needed). It returns a post that already passes the rules, or an error.
- Return the post to the user verbatim, as plain text (no markdown, no quote marks). If you edit it, run validate_linkedin_post(text) before replying.
- Never paste a whole document into the brief.
- The app adds a link that opens the validated post as a LinkedIn draft; never write that link yourself."""  # noqa: E501

CAPABILITY = {
    "name": "linkedin",
    "one_line": "REQUIRED for every LinkedIn post, also one about a document: draft and validate it"
    " (strict length/hashtag/no-link rules)",
    "instructions": INSTRUCTIONS,
    "tools": [
        (
            {
                "name": "write_linkedin_post",
                "description": "Draft a LinkedIn post that passes all rules.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "brief": {
                            "type": "string",
                            "description": "Topic, audience, tone and all key facts.",
                        },
                        "source_doc_id": {
                            "type": "string",
                            "description": "Optional doc id the post is about.",
                        },
                    },
                    "required": ["brief"],
                },
            },
            write_linkedin_post,
        ),
        (
            {
                "name": "validate_linkedin_post",
                "description": "Check a post against the LinkedIn rules.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            validate_linkedin_post,
        ),
    ],
}
