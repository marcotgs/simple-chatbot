"""Stdlib HTTP server: the web chat page plus a curl-able JSON API over core.send()."""
import hmac
import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from chatbot import config, core, db, llm

DEFAULT_USER = "user-1"  # single-user app; Slack maps its users itself
MAX_BODY = 64 * 1024
INDEX_HTML = config.ROOT / "web" / "index.html"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if not self._authorized(url):
            return
        if url.path == "/":
            self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/threads":
            rows = db.conn().execute(
                "SELECT thread_id, MAX(created_at) AS last_at, COUNT(*) AS n FROM messages"
                " WHERE user_id = ? GROUP BY thread_id ORDER BY last_at DESC",
                (DEFAULT_USER,),
            ).fetchall()
            self._json(200, {"threads": [dict(r) for r in rows]})
        elif url.path == "/history":
            thread_id = parse_qs(url.query).get("thread_id", [""])[0]
            if not thread_id:
                return self._json(400, {"error": "thread_id required"})
            rows = db.conn().execute(
                "SELECT role, content, created_at FROM messages WHERE user_id = ? AND thread_id = ?"
                " ORDER BY id",
                (DEFAULT_USER, thread_id),
            ).fetchall()
            self._json(200, {"thread_id": thread_id, "messages": [dict(r) for r in rows]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if not self._authorized(url):
            return
        if url.path != "/send":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._json(413 if length > MAX_BODY else 400, {"error": "bad body size"})
        try:
            body = json.loads(self.rfile.read(length))
            thread_id = str(body["thread_id"]).strip()
            text = str(body["text"])
            user_id = str(body.get("user_id") or DEFAULT_USER)
        except (ValueError, KeyError, TypeError):
            return self._json(400, {"error": "expected JSON {thread_id, text, user_id?}"})
        if not thread_id or not text.strip():
            return self._json(400, {"error": "thread_id and text must be non-empty"})
        try:
            reply = core.send(user_id, thread_id, text)  # serialised per thread inside send()
        except llm.BudgetExceeded as e:
            return self._json(413, {"error": f"token budget exceeded: {e}"})
        except Exception:
            traceback.print_exc()
            return self._json(500, {"error": "internal error, see server log"})
        self._json(200, {"reply": reply})

    def _authorized(self, url) -> bool:
        if not config.WEB_TOKEN:
            return True
        given = self.headers.get("X-Web-Token") or parse_qs(url.query).get("token", [""])[0]
        if hmac.compare_digest(given.encode(), config.WEB_TOKEN.encode()):
            return True
        self._json(401, {"error": "missing or wrong web token"})
        return False

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _send(self, status: int, data: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        # Default logs the full path, which can carry ?token=; log method + path only.
        print(f"{self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}")


def main() -> None:
    db.conn()  # create schema up front so startup errors surface immediately
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"serving on http://{config.HOST}:{config.PORT}/ (model {config.LLM_MODEL})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
