"""Offline Slack DM handler checks: fake `say`, fake core.send, no tokens, no network."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DB_PATH": os.path.join(_tmp, "test.db"),
    "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
    "OPENROUTER_API_KEY": "test-not-a-key",
    "SLACK_USER_ID": "U_OWNER",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import core, llm, slack_app  # noqa: E402
from chatbot.server import DEFAULT_USER  # noqa: E402

# config may already be imported by another test module with a different env.
slack_app.USER_MAP = {"U_OWNER": DEFAULT_USER}


class SlackHandlerTest(unittest.TestCase):
    def setUp(self):
        self.calls, self.said = [], []
        self.reply = "hi back"
        self._orig_send = core.send
        core.send = self._fake_send
        slack_app._seen.clear()
        slack_app._seen_order.clear()

    def tearDown(self):
        core.send = self._orig_send

    def _fake_send(self, user_id, thread_id, text):
        self.calls.append((user_id, thread_id, text))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def _say(self, text):
        self.said.append(text)

    def _event(self, **kw):
        ev = {"type": "message", "channel_type": "im", "user": "U_OWNER", "text": "hello",
              "ts": "1700000000.000100", "client_msg_id": "abc-1"}
        ev.update(kw)
        return ev

    def test_dm_from_owner_routes_to_core_and_replies(self):
        slack_app.handle_message(self._event(), self._say)
        self.assertEqual(self.calls, [(DEFAULT_USER, "slack-dm", "hello")])
        self.assertEqual(self.said, ["hi back"])

    def test_ignored_events_never_reach_core(self):
        cases = {
            "bot message": self._event(bot_id="B1"),
            "bot_message subtype": self._event(subtype="bot_message"),
            "edit": self._event(subtype="message_changed", message={"text": "x"}),
            "delete": self._event(subtype="message_deleted"),
            "channel, not DM": self._event(channel_type="channel"),
        }
        for name, ev in cases.items():
            with self.subTest(name):
                slack_app.handle_message(ev, self._say)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.said, [])

    def test_unknown_user_refused_without_core_call(self):
        slack_app.handle_message(self._event(user="U_STRANGER"), self._say)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.said), 1)
        self.assertIn("only", self.said[0].lower())

    def test_duplicate_delivery_calls_core_once(self):
        ev = self._event()
        slack_app.handle_message(ev, self._say)
        slack_app.handle_message(dict(ev), self._say)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.said, ["hi back"])

    def test_dedupe_falls_back_to_ts(self):
        ev = self._event()
        del ev["client_msg_id"]
        slack_app.handle_message(ev, self._say)
        slack_app.handle_message(dict(ev), self._say)
        self.assertEqual(len(self.calls), 1)

    def test_core_errors_become_friendly_messages(self):
        cases = {
            "empty": ValueError("text is empty"),
            "budget": llm.BudgetExceeded("prompt 9000 > 8000"),
            "crash": RuntimeError("boom sk-secret-token at /srv/x.py line 3"),
        }
        for i, (name, err) in enumerate(cases.items()):
            with self.subTest(name):
                self.said.clear()
                self.reply = err
                with self.assertLogs(slack_app.log, "ERROR") if name == "crash" else _nullctx():
                    slack_app.handle_message(self._event(client_msg_id=f"err-{i}"), self._say)
                self.assertEqual(len(self.said), 1)
                msg = self.said[0]
                for leak in ("Traceback", "boom", "sk-secret", ".py", "9000"):
                    self.assertNotIn(leak, msg)

    def test_missing_tokens_refuse_to_start(self):
        orig = slack_app.config.SLACK_BOT_TOKEN, slack_app.config.SLACK_APP_TOKEN
        slack_app.config.SLACK_BOT_TOKEN = slack_app.config.SLACK_APP_TOKEN = ""
        try:
            with self.assertRaises(RuntimeError):
                slack_app.start()
        finally:
            slack_app.config.SLACK_BOT_TOKEN, slack_app.config.SLACK_APP_TOKEN = orig


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
