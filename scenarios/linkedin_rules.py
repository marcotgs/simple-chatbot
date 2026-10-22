"""Independent LinkedIn post checker: the authority for S7 (the implementation under test is not trusted)."""
import re

MAX_CHARS = 1300
MAX_HASHTAGS = 3
_TAG = re.compile(r"(?<![\w&/#])#\w+")
_TRAILING_TAGS = re.compile(r"(?:(?<![\w&/#])#\w+[ \t,|]*\n?[ \t]*)+\Z")
_LINK = re.compile(
    r"https?://|\bwww\.|\[[^\]]*\]\([^)]*\)|"
    r"\b[a-z0-9-]+\.(?:com|org|net|io|ai|co|dev|app|ly|gg|me|info|biz|link|page|site|xyz)\b", re.I)
_CLOSER = re.compile(r"^(let me know|feel free|hope (this|that)|want me to|would you like|i can also|if you('d| would) like)", re.I)
_PREAMBLE = re.compile(r"^(here('s| is)|sure|absolutely|of course|okay|ok|great|below)\b.*:\s*$|^.{0,80}:\s*$", re.I)


def check_post(text: str) -> list[str]:
    """[] when the post passes; otherwise one string per violated rule."""
    text = (text or "").strip()
    if not text:
        return ["empty post"]
    errors = []
    if len(text) > MAX_CHARS:
        errors.append(f"too long: {len(text)} > {MAX_CHARS} chars")
    tags = _TAG.findall(text)
    if len(tags) > MAX_HASHTAGS:
        errors.append(f"too many hashtags: {len(tags)} > {MAX_HASHTAGS}")
    m = _TRAILING_TAGS.search(text)
    body = text[:m.start()] if m else text
    if _TAG.search(body):
        errors.append(f"hashtag not at the end: {_TAG.search(body).group(0)}")
    link = _LINK.search(text)
    if link:
        errors.append(f"contains a link: {link.group(0)}")
    return errors


def extract_post(reply: str) -> str:
    """Best-effort: the post inside a model reply. Takes the longest fenced or '---'-separated
    block when present, then strips a one-line preamble ('Here's your post:') and trailing
    closers ('Let me know if...'). Anything else stays and is judged as part of the post."""
    reply = (reply or "").strip()
    fenced = re.findall(r"```[^\n]*\n(.*?)```", reply, re.S)
    if fenced:
        reply = max(fenced, key=len).strip()
    else:
        parts = [p.strip() for p in re.split(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", reply, flags=re.M)]
        reply = max(parts, key=len) if len(parts) > 1 else reply
    lines = reply.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and _PREAMBLE.match(lines[0].strip()):  # at most one preamble line
        lines.pop(0)
    while lines and (not lines[-1].strip() or _CLOSER.match(lines[-1].strip())):
        lines.pop()
    return "\n".join(lines).strip().strip('"').strip()
