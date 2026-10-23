"""Files sent to the Slack bot -> extracted text stored as a per-user doc for the docs capability.

Uploaded content is untrusted data: it is only ever returned through doc tools (tool results),
never placed in the system prompt.
"""

import io
import re
import urllib.error
import urllib.request
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from chatbot import config, db
from chatbot.capabilities import docs

TEXT_TYPES = {".txt", ".md", ".csv", ".json"}
SUPPORTED = sorted(TEXT_TYPES | {".pdf"})
DOWNLOAD_TIMEOUT = 60  # seconds
# Start of the note slack_app appends to the user turn for each file; memory.tools cuts the stored
# message here so the forget guard reads only what the user typed (file names are untrusted).
NOTE_PREFIX = "\n\n[Note: the file "


class UploadError(Exception):
    """Message is short and safe to show the user."""


def is_slack_url(url: str) -> bool:
    """https on slack.com or a subdomain; userinfo/port tricks fail because hostname is parsed."""
    try:
        u = urlsplit(url or "")
        host = (u.hostname or "").lower()
    except ValueError:
        return False
    return u.scheme == "https" and (host == "slack.com" or host.endswith(".slack.com"))


class _SlackOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """urllib re-sends Authorization on redirect, so a redirect off Slack must never be followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_slack_url(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect off Slack refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SlackOnlyRedirects)


def download(url: str, token: str, max_bytes: int = 0) -> bytes:
    """GET a Slack private file with the bot token. max_bytes > 0 caps the read."""
    if not is_slack_url(url):
        raise UploadError("that file link is not a Slack file URL")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with _opener.open(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            if resp.headers.get_content_type() == "text/html":
                # Slack answers a token without files:read with its sign-in page, not an error.
                raise UploadError(
                    "Slack sent a web page instead of the file (is the files:read scope added and "
                    "the app reinstalled?)"
                )
            data = resp.read(max_bytes + 1) if max_bytes > 0 else resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UploadError("the download from Slack failed") from e
    if max_bytes > 0 and len(data) > max_bytes:
        raise UploadError(f"the file is larger than the {max_bytes}-byte limit")
    return data


def safe_name(name: str) -> str:
    """Basename only (both separator kinds), no control chars, no leading dots."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = re.sub(r"[\x00-\x1f\x7f]", "", base).strip().lstrip(".")
    return base[:120] or "file"


def doc_id_for(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", PurePosixPath(safe_name(name)).stem).strip("-_")
    return docs.UPLOAD_PREFIX + (slug[:60] or "file")


def extract_text(name: str, data: bytes) -> str:
    ext = PurePosixPath(name).suffix.lower()
    if ext in TEXT_TYPES:
        return data.decode("utf-8", errors="replace")
    if ext != ".pdf":
        raise UploadError(f"I can read {', '.join(SUPPORTED)} files only")
    # ponytail: no page/size cap (Q-11: none for now); a hostile PDF can burn CPU here.
    from pypdf import PdfReader  # imported lazily: only PDF uploads need it

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UploadError("the PDF is password protected")
        bodies = [(p.extract_text() or "").strip() for p in reader.pages]
    except UploadError:
        raise
    except Exception as e:
        raise UploadError("I could not read that PDF") from e
    if not any(bodies):
        return ""  # scanned images only
    return "\n\n".join(f"[PDF page {i}]\n{b}" for i, b in enumerate(bodies, 1))


def ingest(user_id: str, file: dict, token: str) -> tuple[str, int, int]:
    """Download one Slack file object, store its text. Returns (doc_id, pages, chars)."""
    name = safe_name(file.get("name") or file.get("title") or "")
    ext = PurePosixPath(name).suffix.lower()
    if ext not in TEXT_TYPES and ext != ".pdf":
        raise UploadError(f"I can read {', '.join(SUPPORTED)} files only")
    cap = config.MAX_FILE_BYTES
    if cap > 0 and int(file.get("size") or 0) > cap:
        raise UploadError(f"the file is larger than the {cap}-byte limit")
    url = file.get("url_private_download") or file.get("url_private") or ""
    if not url:
        raise UploadError("Slack sent no download link for it")
    data = download(url, token, cap)
    if not data:
        raise UploadError("the file is empty")
    text = extract_text(name, data)
    if not text.strip():
        raise UploadError("I found no text in it")
    doc_id = doc_id_for(name)
    folder = docs.user_dir(user_id)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    (folder / f"{doc_id}.md").write_text(text, encoding="utf-8")
    c = db.conn()
    with c:  # a re-upload under the same name must not answer from the old file's notes
        c.execute("DELETE FROM doc_notes WHERE user_id = ? AND doc_id = ?", (user_id, doc_id))
    return doc_id, len(docs.paginate(text)), len(text)
