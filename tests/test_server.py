"""Offline HTTP server checks: /healthz without auth, refusal to bind publicly without a token,
Host/Origin/Content-Type guards against CSRF and DNS rebinding."""

import http.client
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["CALL_LOG_PATH"] = os.path.join(_tmp, "calls.jsonl")
os.environ["GATE_MODEL"] = ""  # offline: the salience gate (on by default) never hits the network
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import config, server  # noqa: E402


class HealthzTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(config, "WEB_TOKEN", "synthetic-test-token")
        self.patch.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.patch.stop()

    def _get(self, path, headers=None):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(self.base + path, headers=headers or {}), timeout=5
            ) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_healthz_ok_without_token(self):
        self.assertEqual(self._get("/healthz"), (200, b"ok"))

    def test_page_cannot_be_framed(self):
        # R3-SEC-S3: clickjacking guard on the chat page.
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            self.assertEqual(r.headers["X-Frame-Options"], "DENY")
            self.assertEqual(r.headers["Content-Security-Policy"], "frame-ancestors 'none'")

    def test_api_still_needs_token(self):
        self.assertEqual(self._get("/")[0], 200)  # static page only; it carries no data
        self.assertEqual(self._get("/threads")[0], 401)
        self.assertEqual(self._get("/threads", {"X-Web-Token": "synthetic-test-token"})[0], 200)


class RequestGuardTest(unittest.TestCase):
    """F-1: CSRF / DNS rebinding on the unauthenticated loopback API (WEB_TOKEN empty)."""

    def setUp(self):
        self.patches = [
            mock.patch.object(config, "WEB_TOKEN", ""),
            mock.patch.object(config, "ALLOWED_HOSTS", ["bot.example.test"]),
            mock.patch.object(server.core, "send", lambda u, t, x: "pong"),
        ]
        for p in self.patches:
            p.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.local = f"127.0.0.1:{self.port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for p in self.patches:
            p.stop()

    def _req(self, method, path, headers, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k, v)
        data = body.encode() if body is not None else None
        if data is not None:
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        out = (r.status, dict(r.getheaders()), r.read())
        c.close()
        return out

    def _post(self, headers, body='{"thread_id": "t", "text": "hi"}'):
        return self._req("POST", "/send", headers, body)

    def test_loopback_hosts_and_configured_host_allowed(self):
        for host in (
            self.local,
            f"localhost:{self.port}",
            f"[::1]:{self.port}",
            "BOT.example.test",
        ):
            with self.subTest(host=host):
                self.assertEqual(self._req("GET", "/threads", {"Host": host})[0], 200)

    def test_wrong_or_missing_host_is_403_on_every_route(self):
        for host in ("evil.test", f"evil.test:{self.port}", "127.0.0.1:1", None):
            for method, path in (
                ("GET", "/"),
                ("GET", "/threads"),
                ("GET", "/history?thread_id=t"),
                ("POST", "/send"),
            ):
                with self.subTest(host=host, path=path):
                    h = {"Content-Type": "application/json"}
                    if host:
                        h["Host"] = host
                    status, _, body = self._req(method, path, h, "{}" if method == "POST" else None)
                    self.assertEqual(status, 403)
                    self.assertNotIn(b"pong", body)

    def test_healthz_answers_any_host_without_data(self):
        self.assertEqual(
            self._req("GET", "/healthz", {"Host": "fly-internal:8080"})[::2], (200, b"ok")
        )

    def test_send_requires_json_content_type(self):
        for ctype in (
            None,
            "text/plain",
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        ):
            with self.subTest(ctype=ctype):
                h = {"Host": self.local}
                if ctype:
                    h["Content-Type"] = ctype
                self.assertEqual(self._post(h)[0], 415)
        ok = self._post({"Host": self.local, "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual((ok[0], ok[2]), (200, b'{"reply": "pong"}'))

    def test_cross_origin_refused_same_origin_allowed(self):
        base = {"Host": self.local, "Content-Type": "application/json"}
        for origin in (
            "http://evil.test",
            "null",
            f"http://localhost:{self.port}",
            "https://bot.example.test",
        ):
            with self.subTest(origin=origin):
                self.assertEqual(self._post({**base, "Origin": origin})[0], 403)
                self.assertEqual(
                    self._req("GET", "/threads", {"Host": self.local, "Origin": origin})[0], 403
                )
        self.assertEqual(self._post({**base, "Origin": f"http://{self.local}"})[0], 200)
        fly = {
            "Host": "bot.example.test",
            "Content-Type": "application/json",
            "Origin": "https://bot.example.test",
        }
        self.assertEqual(self._post(fly)[0], 200)

    def test_no_cors_headers_emitted(self):
        for _status, headers, _ in (
            self._post({"Host": self.local, "Content-Type": "application/json"}),
            self._req("GET", "/threads", {"Host": self.local}),
        ):
            self.assertFalse([k for k in headers if k.lower().startswith("access-control-")])


class StartupGuardTest(unittest.TestCase):
    def test_startup_error_table(self):
        cases = [
            ("127.0.0.1", "", False),
            ("localhost", "", False),
            ("::1", "", False),
            ("0.0.0.0", "", True),
            ("192.168.1.5", "", True),
            ("example.test", "", True),
            ("0.0.0.0", "tok", False),
        ]
        for host, token, refused in cases:
            with self.subTest(host=host, token=bool(token)):
                self.assertEqual(bool(server.startup_error(host, token)), refused)

    def test_main_exits_2_on_public_host_without_token(self):
        with (
            mock.patch.object(config, "HOST", "0.0.0.0"),
            mock.patch.object(config, "WEB_TOKEN", ""),
            mock.patch.object(server, "ThreadingHTTPServer") as srv,
        ):
            with self.assertRaises(SystemExit) as cm:
                server.main()
        self.assertEqual(cm.exception.code, 2)
        srv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
