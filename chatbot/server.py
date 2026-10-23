"""Stdlib HTTP server: the web chat page plus a curl-able JSON API over core.send()."""

import hmac
import ipaddress
import json
import logging
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from chatbot import config, core, db, llm

DEFAULT_USER = "user-1"  # single-user app; Slack maps its users itself
MAX_BODY = 64 * 1024
INDEX_HTML = config.ROOT / "web" / "index.html"
log = logging.getLogger("chatbot.server")


def startup_error(host: str, token: str) -> str:
    """'' when it is safe to start; otherwise why not (a public bind without WEB_TOKEN burns the API
    key)."""
    if token:
        return ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    return (
        ""
        if loopback
        else (
            f"refusing to start: HOST={host!r} is not loopback and WEB_TOKEN is empty. "
            "Set WEB_TOKEN, or bind HOST=127.0.0.1."
        )
    )


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # seconds per socket read: a slow or stalled client cannot pin a thread forever

    def do_GET(self):
        url = urlparse(self.path)
        if (
            url.path == "/healthz"
        ):  # liveness only, no data: answered for any Host (Fly checks), before auth
            return self._send(200, b"ok", "text/plain; charset=utf-8")
        if not self._same_origin():
            return
        if (
            url.path == "/"
        ):  # static page, no data; it keeps the token in localStorage, so reloads work
            return self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        if not self._authorized(url):
            return
        if url.path == "/threads":
            rows = (
                db.conn()
                .execute(
                    "SELECT thread_id, MAX(created_at) AS last_at, COUNT(*) AS n FROM messages"
                    " WHERE user_id = ? GROUP BY thread_id ORDER BY last_at DESC",
                    (DEFAULT_USER,),
                )
                .fetchall()
            )
            self._json(200, {"threads": [dict(r) for r in rows]})
        elif url.path == "/history":
            thread_id = parse_qs(url.query).get("thread_id", [""])[0]
            if not thread_id:
                return self._json(400, {"error": "thread_id required"})
            rows = (
                db.conn()
                .execute(
                    "SELECT role, content, created_at FROM messages WHERE user_id = ? AND "
                    "thread_id = ?"
                    " ORDER BY id",
                    (DEFAULT_USER, thread_id),
                )
                .fetchall()
            )
            self._json(200, {"thread_id": thread_id, "messages": [dict(r) for r in rows]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if not self._same_origin() or not self._authorized(url):
            return
        if url.path != "/send":
            return self._json(404, {"error": "not found"})
        # JSON only: a cross-site form/text POST ("simple request", no preflight) gets 415. No CORS
        # headers, ever.
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
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

    def _same_origin(self) -> bool:
        """Host must be loopback on our port or in ALLOWED_HOSTS (DNS rebinding); an Origin header,
        when present, must be this same host (CSRF). Answers 403 itself on failure."""
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        if host not in {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
            *config.ALLOWED_HOSTS,
        }:
            self._json(403, {"error": "host not allowed (set ALLOWED_HOSTS)"})
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in (
            f"http://{host}",
            f"https://{host}",
        ):
            self._json(403, {"error": "cross-origin request refused"})
            return False
        return True

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
        self.send_header("X-Frame-Options", "DENY")  # no framing of the chat page (clickjacking)
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        # Default logs the full path, which can carry ?token=; log method + path only.
        print(f"{self.command} {urlparse(self.path).path} -> {args[1] if len(args) > 1 else ''}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    err = startup_error(config.HOST, config.WEB_TOKEN)
    if err:
        print(err, file=sys.stderr)
        raise SystemExit(2)
    db.conn()  # create schema up front so startup errors surface immediately
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    if config.SLACK_BOT_TOKEN and config.SLACK_APP_TOKEN:
        from chatbot import slack_app

        slack_app.start_in_thread()
        log.info("slack enabled (Socket Mode thread started)")
    else:
        log.info("slack disabled: SLACK_BOT_TOKEN / SLACK_APP_TOKEN not set")
    print(f"serving on http://{config.HOST}:{config.PORT}/ (model {config.LLM_MODEL})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
