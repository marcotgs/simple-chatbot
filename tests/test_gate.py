"""Salience gate before fact extraction (GATE_MODEL). Offline: HTTP stubbed, fake LLM client.
Live check only with RUN_LIVE_TESTS=1. Run: python3 -m unittest discover tests"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.update(
    {
        "DB_PATH": os.path.join(_tmp, "test.db"),
        "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
        "GATE_MODEL": "",  # offline: the salience gate (on by default) never hits the network
        "MAX_INPUT_TOKENS": "8000",
        "TARGET_INPUT_TOKENS": "6500",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import config, core, db, llm  # noqa: E402
from chatbot.memory import extract, gate  # noqa: E402

GATE = "test/gate-model"  # synthetic id
FAKE_KEY = "sk-test-gate-secret-123"  # synthetic


class FakeClient:
    """Extractor stand-in: always stores one fact."""

    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        content = json.dumps(
            {"facts": [{"key": "pet_name", "value": "Mochi, the cat", "scope": "global"}]}
        )
        msg = SimpleNamespace(content=content, tool_calls=None)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reply(noul=None, raw=None):
    if raw is None:
        raw = json.dumps(
            {
                "model": GATE,
                "answers": {"remember": {"type": "noul", "noul": noul}},
                "usage": {"input_tokens": 120, "output_tokens": 20, "cost": 0.00001},
            }
        )
    return FakeResp(raw.encode())


def gate_rows(thread_id):
    return [
        tuple(r)
        for r in db.conn()
        .execute(
            "SELECT model, prompt_tokens, completion_tokens FROM llm_calls"
            " WHERE purpose = 'gate' AND thread_id = ?",
            (thread_id,),
        )
        .fetchall()
    ]


class GateTest(unittest.TestCase):
    def setUp(self):
        self._client, llm._client = llm._client, FakeClient()
        self.u, self.t = f"u-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        llm._client = self._client

    def _turn(self, urlopen, model=GATE):
        """One user turn through the extractor; returns (stored, urlopen mock, stderr)."""
        core.ingest_turn(self.u, self.t, "user", "My cat is called Mochi.")  # turn 1
        err = io.StringIO()
        with (
            mock.patch.object(config, "GATE_MODEL", model),
            mock.patch.object(config, "OPENROUTER_API_KEY", FAKE_KEY),
            mock.patch.object(gate._opener, "open", urlopen) as m,
            contextlib.redirect_stderr(err),
        ):
            stored = extract.maybe_extract(self.u, self.t, 1)
        return stored, m, err.getvalue()

    def test_gate_off_makes_no_gate_call(self):
        stored, m, _ = self._turn(mock.Mock(), model="")
        m.assert_not_called()
        self.assertEqual((stored, llm._client.calls), (1, 1))

    def test_no_skips_extractor_and_logs_gate_call(self):
        stored, m, _ = self._turn(mock.Mock(return_value=reply(0.05)))
        self.assertEqual((stored, llm._client.calls), (0, 0))
        req = m.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, gate.URL)
        self.assertEqual(body["model"], GATE)
        self.assertEqual(body["questions"]["remember"]["type"], "noul")
        self.assertIn("MESSAGES:\n[turn 1] My cat is called Mochi.", body["state"])
        self.assertEqual(gate_rows(self.t), [(GATE, 120, 20)])
        # Cursor advanced: the skipped turn is not re-sent on the next run.
        self.assertEqual(extract._cursor(self.u, self.t), 1)

    def test_yes_runs_extractor(self):
        stored, _, _ = self._turn(mock.Mock(return_value=reply(0.9)))
        self.assertEqual((stored, llm._client.calls), (1, 1))

    def test_failures_open_to_extractor_without_leaking_key(self):
        http_err = urllib.error.HTTPError(gate.URL, 500, "boom", {}, io.BytesIO(b"{}"))
        cases = {
            "timeout": mock.Mock(side_effect=TimeoutError()),
            "http": mock.Mock(side_effect=http_err),
            "garbage": mock.Mock(return_value=reply(raw="<html>not json")),
            "shape": mock.Mock(return_value=reply(raw='{"answers": {"other": {}}}')),
            "out_of_range": mock.Mock(return_value=reply(1.7)),
            "bool": mock.Mock(return_value=reply(True)),
        }
        for name, urlopen in cases.items():
            with self.subTest(name):
                self.setUp()
                stored, m, err = self._turn(urlopen)
                m.assert_called_once()
                self.assertEqual((stored, llm._client.calls), (1, 1))
                self.assertEqual(err.count("[memory] gate"), 1, err)
                self.assertNotIn(FAKE_KEY, err)
                self.assertNotIn(FAKE_KEY, config.CALL_LOG_PATH.read_text())

    def test_zero_input_tokens_counts_as_missing_usage(self):
        # A provider error path reports 0: log the estimate and flag usage_missing (as llm.chat).
        zero = json.dumps(
            {
                "answers": {"remember": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        )
        stored, _, _ = self._turn(mock.Mock(return_value=reply(raw=zero)))
        self.assertEqual(stored, 1)
        row = (
            db.conn()
            .execute(
                "SELECT estimated_tokens, prompt_tokens, usage_missing FROM llm_calls"
                " WHERE purpose = 'gate' AND thread_id = ?",
                (self.t,),
            )
            .fetchone()
        )
        self.assertGreater(row["estimated_tokens"], 0)
        self.assertEqual(row["prompt_tokens"], row["estimated_tokens"])
        self.assertEqual(row["usage_missing"], 1)

    def test_over_budget_turn_skips_gate_call(self):
        urlopen = mock.Mock()
        with (
            mock.patch.object(config, "TARGET_INPUT_TOKENS", 10),
            mock.patch.object(config, "GATE_MODEL", GATE),
            mock.patch.object(gate._opener, "open", urlopen),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            ok = gate.should_extract("x" * 500, self.t)
        urlopen.assert_not_called()
        self.assertTrue(ok)


class GateRedirectTest(unittest.TestCase):
    def test_redirect_is_not_followed_and_key_not_resent(self):
        # R3-SEC-W1: a 302 from the gate endpoint must not re-send the Authorization header to the
        # redirect target (nor trust its answer); the gate fails open and the extractor runs.
        seen = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                body = reply(0.0).getvalue()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

            def log_message(self, *a):
                pass

        target = HTTPServer(("127.0.0.1", 0), Target)

        class Origin(Target):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x")
                self.send_header("Content-Length", "0")
                self.end_headers()

        origin = HTTPServer(("127.0.0.1", 0), Origin)
        for srv in (target, origin):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
        with (
            mock.patch.object(gate, "URL", f"http://127.0.0.1:{origin.server_port}/d"),
            mock.patch.object(config, "GATE_MODEL", GATE),
            mock.patch.object(config, "OPENROUTER_API_KEY", FAKE_KEY),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            ok = gate.should_extract("MESSAGES:\n[turn 1] My cat is called Mochi.", "redir")
        self.assertEqual(seen, [])
        self.assertTrue(ok)
        self.assertIn("[memory] gate", err.getvalue())
        self.assertNotIn(FAKE_KEY, err.getvalue())


class GateConfigTest(unittest.TestCase):
    def test_default_on_and_empty_disables(self):
        # Fresh interpreter: reloading config here would rebind DB_PATH for the rest of the suite.
        dotenv = config.ROOT / ".env"
        if dotenv.is_file() and re.search(r"(?m)^\s*GATE_", dotenv.read_text()):
            self.skipTest(".env sets GATE_*, which the subprocess loads over the defaults")

        def gate_config(**env):
            base = {k: v for k, v in os.environ.items() if not k.startswith("GATE_")}
            out = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from chatbot import config;"
                    " print(repr((config.GATE_MODEL, config.GATE_THRESHOLD)))",
                ],
                env={**base, **env},
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True,
                text=True,
                check=True,
            )
            return out.stdout.strip()

        self.assertEqual(gate_config(), "('typesafe/jev-1.14', 0.3)")  # unset = on
        self.assertEqual(gate_config(GATE_MODEL=""), "('', 0.3)")  # empty = off
        self.assertEqual(gate_config(GATE_MODEL=GATE, GATE_THRESHOLD="0.45"), f"('{GATE}', 0.45)")

    @unittest.skipIf(
        os.environ.get("RUN_LIVE_TESTS") == "1",
        "live modules imported first in a combined run set the gate on; offline-only canary",
    )
    def test_offline_suite_runs_with_gate_off(self):
        # Canary: every offline test module sets GATE_MODEL="" before importing chatbot, so no
        # test turn reaches the decisions endpoint (and subprocesses inherit the empty value).
        self.assertEqual(config.GATE_MODEL, "")
        self.assertEqual(os.environ.get("GATE_MODEL"), "")


@unittest.skipUnless(os.environ.get("RUN_LIVE_TESTS") == "1", "live: set RUN_LIVE_TESTS=1")
class GateLiveTest(unittest.TestCase):
    """Two real decisions calls (~$0.00003 total)."""

    def test_live_yes_and_no(self):
        if not config.OPENROUTER_API_KEY or config.OPENROUTER_API_KEY.startswith("test-"):
            self.skipTest("no real key (run this module alone with .env)")
        with mock.patch.object(config, "GATE_MODEL", "typesafe/jev-1.14"):
            p_yes = gate._probability("MESSAGES:\n[turn 1] I live in Lisbon now.", "live")
            p_no = gate._probability("MESSAGES:\n[turn 1] haha ok thanks!", "live")
        self.assertGreaterEqual(p_yes, config.GATE_THRESHOLD)
        self.assertLess(p_no, config.GATE_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
