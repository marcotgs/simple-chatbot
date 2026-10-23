"""Q-13 (c) / BUG-L2: an extracted fact may replace an existing different value of the same key only
when the extractor flags the turn as an explicit correction. Offline: llm.chat is a scripted fake.
Run: python3 -m unittest tests.test_overwrite_guard -v"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("CALL_LOG_PATH", os.path.join(_tmp, "calls.jsonl"))
os.environ["GATE_MODEL"] = ""  # offline: the salience gate (on by default) never hits the network
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import core, db, llm  # noqa: E402
from chatbot.memory import extract, facts  # noqa: E402


class OverwriteGuardTest(unittest.TestCase):
    def setUp(self):
        self.reply = {"facts": []}
        fake = lambda *a, **k: SimpleNamespace(  # noqa: E731
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(self.reply), tool_calls=None)
                )
            ]
        )
        p = mock.patch.object(llm, "chat", fake)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(core, "after_turn", lambda *a: None)  # extraction driven by say()
        p.start()
        self.addCleanup(p.stop)
        self.u, self.t = f"og-{uuid.uuid4().hex[:8]}", "slack-dm"

    def say(self, *items):
        """One user turn; the extractor returns `items` (dicts merged over a global set)."""
        core.ingest_turn(self.u, self.t, "user", "synthetic message")
        core.ingest_turn(self.u, self.t, "assistant", "ok")
        n = self._turn()
        self.reply = {
            "facts": [{"scope": "global", "action": "set", "turn": n, **it} for it in items]
        }
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            extract.maybe_extract(self.u, self.t, n)
        return err.getvalue()

    def _turn(self):
        return (
            db.conn()
            .execute(
                "SELECT MAX(turn_no) FROM messages WHERE user_id = ? AND thread_id = ?",
                (self.u, self.t),
            )
            .fetchone()[0]
        )

    def active(self, key):
        return [f["value"] for f in facts.get_active_facts(self.u, self.t) if f["key"] == key]

    def test_overwrite_without_correction_is_rejected_and_logged_without_value(self):
        self.say({"key": "partner_name", "value": "Alex Example"})
        log = self.say({"key": "partner_name", "value": "Robin Sample", "correction": False})
        self.assertEqual(self.active("partner_name"), ["Alex Example"])
        self.assertIn("rejected overwrite", log)
        self.assertNotIn("Robin", log)
        self.assertNotIn("Alex", log)

    def test_missing_correction_field_defaults_to_reject(self):
        self.say({"key": "partner_name", "value": "Alex Example"})
        self.say({"key": "partner_name", "value": "Robin Sample"})
        self.say({"key": "partner_name", "value": "Robin Sample", "correction": "true"})
        self.assertEqual(self.active("partner_name"), ["Alex Example"])

    def test_correction_supersedes_and_keeps_history(self):
        self.say({"key": "partner_name", "value": "Alex Example"})
        self.say({"key": "partner_name", "value": "Robin Sample", "correction": True})
        self.assertEqual(self.active("partner_name"), ["Robin Sample"])
        hist = facts.get_fact_history(self.u, "partner_name")
        self.assertEqual(
            [(h["value"], h["status"]) for h in hist],
            [("Alex Example", "superseded"), ("Robin Sample", "current")],
        )
        self.assertEqual(hist[0]["superseded_by"], hist[1]["id"])

    def test_new_key_and_same_value_repeat_unaffected(self):
        self.say({"key": "home_city", "value": "Testville"})
        self.say({"key": "home_city", "value": "testville"})  # same value, no correction flag
        self.say({"key": "manager", "value": "Sam Placeholder"})
        self.assertEqual(self.active("home_city"), ["Testville"])
        self.assertEqual(self.active("manager"), ["Sam Placeholder"])

    def test_retract_unaffected(self):
        self.say({"key": "manager", "value": "Sam Placeholder"})
        self.say({"key": "manager", "action": "retract"})
        self.assertEqual(self.active("manager"), [])

    def test_other_scope_is_not_an_overwrite(self):
        self.say({"key": "reply_tone", "value": "casual"})
        self.say({"key": "reply_tone", "value": "formal", "scope": "session"})
        self.assertEqual(sorted(self.active("reply_tone")), ["casual", "formal"])


if __name__ == "__main__":
    unittest.main()
