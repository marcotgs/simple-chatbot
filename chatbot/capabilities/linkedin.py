"""LinkedIn posts: deterministic rule checker + a drafting loop that only returns passing posts."""
import re

from chatbot import db, llm

MAX_CHARS = 1300
MAX_HASHTAGS = 3
MAX_REGENERATIONS = 3  # after the first draft
MAX_FACTS_CHARS = 2000

RULES = f"""- At most {MAX_CHARS} characters in total.
- At most {MAX_HASHTAGS} hashtags, and all hashtags go at the very end (nothing but hashtags after the first one).
- No links of any kind: no http:// or https://, no www., no bare domains like example.com."""

_HASHTAG = re.compile(r"(?<![\w#/])#\w+")
# ponytail: bare domains matched against a fixed TLD list; extend the list if other TLDs slip through.
_LINKS = [
    ("contains an http(s):// link", re.compile(r"https?://", re.I)),
    ("contains a www. link", re.compile(r"\bwww\.", re.I)),
    ("contains a bare domain", re.compile(
        r"\b[a-z0-9-]+\.(?:com|io|net|org|co|ai|dev|app|ly|me|gg|xyz|info|biz|tech|us|uk|so|to)\b", re.I)),
]


def check_linkedin_post(text: str) -> list[str]:
    """Return rule violations; empty list means the post is valid."""
    text = text or ""
    violations = []
    if not text.strip():
        violations.append("post is empty")
    if len(text) > MAX_CHARS:
        violations.append(f"too long: {len(text)} characters (max {MAX_CHARS})")
    tags = list(_HASHTAG.finditer(text))
    if len(tags) > MAX_HASHTAGS:
        violations.append(f"too many hashtags: {len(tags)} (max {MAX_HASHTAGS})")
    if tags and _HASHTAG.sub("", text[tags[0].start():]).strip():
        violations.append("hashtags must all be at the very end, with only hashtags after the first one")
    violations += [msg for msg, rx in _LINKS if rx.search(text)]
    return violations


def generate_linkedin_post(brief: str, facts: str = "", thread_id: str | None = None) -> str:
    """Draft a post from the brief + key facts only; regenerate with the violations until it passes.
    Returns the post, or a string starting with 'error:'."""
    messages = [
        {"role": "system", "content": "You write LinkedIn posts. Rules:\n" + RULES
                                      + "\nReply with the post text only, no preamble, no quotes."},
        {"role": "user", "content": f"Brief: {brief}" + (f"\n\nKey facts:\n{facts}" if facts else "")},
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
            {"role": "user", "content": "That draft breaks these rules: " + "; ".join(violations)
                                        + ". Rewrite it so every rule passes."},
        ]
    return (f"error: could not produce a valid LinkedIn post after {1 + MAX_REGENERATIONS} attempts; "
            f"last violations: {'; '.join(violations)}")


def _doc_key_facts(doc_id: str, thread_id: str) -> str:
    """Key facts already extracted for a doc (doc_notes), never the raw doc."""
    rows = db.conn().execute(
        "SELECT content FROM doc_notes WHERE doc_id = ? AND kind = 'key_fact'"
        " AND (thread_id IS NULL OR thread_id = ?) ORDER BY id LIMIT 20",
        (doc_id, thread_id),
    ).fetchall()
    return "\n".join(f"- {r['content']}" for r in rows)[:MAX_FACTS_CHARS]


def write_linkedin_post(user_id: str, thread_id: str, brief: str = "", source_doc_id: str | None = None) -> str:
    if not (brief or "").strip():
        return "error: brief is required"
    facts = _doc_key_facts(source_doc_id, thread_id) if source_doc_id else ""
    return generate_linkedin_post(brief, facts, thread_id)


def validate_linkedin_post(user_id: str, thread_id: str, text: str = "") -> str:
    violations = check_linkedin_post(text)
    return "valid" if not violations else "invalid: " + "; ".join(violations)


INSTRUCTIONS = f"""LinkedIn post rules (enforced by code, not optional):
{RULES}
How to work:
- Use write_linkedin_post(brief, source_doc_id). Put every key fact (dates, names, numbers) in the brief;
  the drafter does not read documents. It returns a post that already passes the rules, or an error.
- Return the post to the user verbatim. If you edit it, run validate_linkedin_post(text) before replying.
- Never paste a whole document into the brief."""

CAPABILITY = {
    "name": "linkedin",
    "one_line": "draft and validate LinkedIn posts (strict length/hashtag/no-link rules)",
    "instructions": INSTRUCTIONS,
    "tools": [
        ({"name": "write_linkedin_post", "description": "Draft a LinkedIn post that passes all rules.",
          "parameters": {"type": "object", "properties": {
              "brief": {"type": "string", "description": "Topic, audience, tone and all key facts."},
              "source_doc_id": {"type": "string", "description": "Optional doc id the post is about."}},
              "required": ["brief"]}}, write_linkedin_post),
        ({"name": "validate_linkedin_post", "description": "Check a post against the LinkedIn rules.",
          "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                         "required": ["text"]}}, validate_linkedin_post),
    ],
}
