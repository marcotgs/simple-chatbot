"""Offline Slack DM handler checks: fake `say`, fake core.send, no tokens, no network."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update(
    {
        "DB_PATH": os.path.join(_tmp, "test.db"),
        "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
        "GATE_MODEL": "",  # offline: the salience gate (on by default) never hits the network
        "OPENROUTER_API_KEY": "test-not-a-key",
        "SLACK_USER_ID": "U_OWNER",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import core, llm, slack_app, uploads  # noqa: E402
from chatbot.capabilities import docs  # noqa: E402
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
        ev = {
            "type": "message",
            "channel_type": "im",
            "user": "U_OWNER",
            "text": "hello",
            "ts": "1700000000.000100",
            "client_msg_id": "abc-1",
        }
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

    def test_event_without_dedupe_key_is_dropped(self):
        # S-SEC7: no client_msg_id and no ts -> cannot dedupe a retry, so fail closed.
        ev = self._event()
        del ev["client_msg_id"], ev["ts"]
        slack_app.handle_message(ev, self._say)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.said, [])

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


class SlackCommandAndMentionTest(unittest.TestCase):
    """T16: `/bot <text>` and `@bot <text>`: owner-only, replies only the requester can see."""

    def setUp(self):
        self.calls, self.events = [], []
        self._orig_send = core.send
        core.send = lambda u, t, text: self.calls.append((u, t, text)) or "answer"
        slack_app._seen.clear()
        slack_app._seen_order.clear()

    def tearDown(self):
        core.send = self._orig_send

    def _ack(self):
        self.events.append("ack")

    def _respond(self, text=None, response_type=None, **kw):
        self.events.append(("respond", text, response_type))

    def _command(self, **kw):
        return {"user_id": "U_OWNER", "text": "hello", "trigger_id": "tr-1", **kw}

    def test_slash_command_acks_first_then_answers_ephemerally(self):
        slack_app.handle_command(self._ack, self._command(), self._respond)
        self.assertEqual(self.calls, [(DEFAULT_USER, "slack-dm", "hello")])
        self.assertEqual(self.events, ["ack", ("respond", "answer", "ephemeral")])

    def test_slash_command_refuses_non_owner_and_hints_on_empty_text(self):
        cases = {
            "stranger": (self._command(user_id="U_STRANGER", trigger_id="a"), "only"),
            "empty": (self._command(text="   ", trigger_id="b"), "/bot"),
        }
        for name, (cmd, needle) in cases.items():
            with self.subTest(name):
                self.events.clear()
                slack_app.handle_command(self._ack, cmd, self._respond)
                self.assertEqual(self.events[0], "ack")
                _, text, rtype = self.events[1]
                self.assertEqual(rtype, "ephemeral")
                self.assertIn(needle, text)
        self.assertEqual(self.calls, [])

    def test_slash_command_redelivery_answers_once(self):
        slack_app.handle_command(self._ack, self._command(), self._respond)
        slack_app.handle_command(self._ack, self._command(), self._respond)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.events.count("ack"), 2)  # every delivery is still acked

    def _mention(self, **kw):
        ev = {
            "type": "app_mention",
            "user": "U_OWNER",
            "text": "<@UBOT123> what is on my plate?",
            "channel": "C1",
            "ts": "1700000000.000200",
            "client_msg_id": "m-1",
        }
        ev.update(kw)
        return ev

    def test_mention_strips_bot_and_replies_ephemerally_to_owner(self):
        client = _FakeClient()
        slack_app.handle_mention(self._mention(thread_ts="1700000000.000001"), client)
        self.assertEqual(self.calls, [(DEFAULT_USER, "slack-dm", "what is on my plate?")])
        self.assertEqual(
            client.posts,
            [
                {
                    "channel": "C1",
                    "user": "U_OWNER",
                    "text": "answer",
                    "thread_ts": "1700000000.000001",
                }
            ],
        )

    def test_mention_cases_never_post_publicly_or_reach_core_wrongly(self):
        cases = {
            "stranger": (self._mention(user="U_STRANGER", client_msg_id="s"), "only"),
            "empty": (self._mention(text="<@UBOT123>  ", client_msg_id="e"), "@"),
        }
        for name, (ev, needle) in cases.items():
            with self.subTest(name):
                client = _FakeClient()
                slack_app.handle_mention(ev, client)
                self.assertEqual(len(client.posts), 1)
                self.assertEqual(client.posts[0]["user"], ev["user"])
                self.assertIn(needle, client.posts[0]["text"])
        self.assertEqual(self.calls, [])

    def test_mention_dedupes_and_fails_closed_without_key(self):
        client = _FakeClient()
        slack_app.handle_mention(self._mention(), client)
        slack_app.handle_mention(self._mention(), client)
        ev = self._mention()
        del ev["client_msg_id"], ev["ts"]
        slack_app.handle_mention(ev, client)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(client.posts), 1)


class _FakeClient:
    def __init__(self):
        self.posts = []

    def chat_postEphemeral(self, **kw):  # noqa: N802 (slack_sdk method name)
        kw = {k: v for k, v in kw.items() if v is not None}
        self.posts.append(kw)


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SlackFileShareTest(unittest.TestCase):
    """file_share DMs: ingest each file (fake download), ack, then answer any text with a note."""

    _fake_send, _say, _event = (
        SlackHandlerTest._fake_send,
        SlackHandlerTest._say,
        SlackHandlerTest._event,
    )

    def setUp(self):
        SlackHandlerTest.setUp(self)
        self._orig_up = (docs.UPLOADS_DIR, uploads.download)
        docs.UPLOADS_DIR = Path(tempfile.mkdtemp())
        self.downloads = []
        uploads.download = lambda url, token, max_bytes=0: self.downloads.append(url) or b"kiwi"

    def tearDown(self):
        SlackHandlerTest.tearDown(self)
        docs.UPLOADS_DIR, uploads.download = self._orig_up

    def _share(self, text="", names=("notes.txt",), **kw):
        files = [
            {
                "id": f"F{i}",
                "name": n,
                "size": 4,
                "url_private_download": f"https://files.slack.com/{i}",
            }
            for i, n in enumerate(names)
        ]
        return self._event(subtype="file_share", text=text, files=files, **kw)

    def test_file_without_text_is_stored_and_acked_without_core(self):
        slack_app.handle_message(self._share(), self._say)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.said), 1)
        self.assertIn("notes.txt", self.said[0])
        self.assertIn("upload-notes", self.said[0])
        self.assertIn("4 characters", self.said[0])
        self.assertIn("kiwi", docs.fetch_doc(DEFAULT_USER, "t", doc_id="upload-notes"))

    def test_text_with_file_reaches_core_with_doc_note(self):
        slack_app.handle_message(self._share("what does it say?"), self._say)
        self.assertEqual(len(self.calls), 1)
        text = self.calls[0][2]
        self.assertTrue(text.startswith("what does it say?"))
        self.assertIn("document upload-notes", text)
        self.assertNotIn("kiwi", text)  # file content only through doc tools, never inline
        self.assertEqual(self.said[-1], "hi back")

    def test_unsupported_and_good_file_together(self):
        slack_app.handle_message(self._share("hi", names=("a.exe", "b.md")), self._say)
        self.assertIn("Could not use a.exe", self.said[0])
        self.assertIn("Got b.md", self.said[0])
        self.assertEqual(self.downloads, ["https://files.slack.com/1"])
        self.assertIn("upload-b", self.calls[0][2])
        self.assertNotIn("a.exe", self.calls[0][2])

    def test_non_owner_file_refused_without_download(self):
        slack_app.handle_message(self._share(user="U_STRANGER"), self._say)
        self.assertEqual(self.said, [slack_app.REFUSAL])
        self.assertEqual(self.downloads, [])

    def test_file_redelivery_ingested_once(self):
        ev = self._share("x")
        slack_app.handle_message(ev, self._say)
        slack_app.handle_message(dict(ev), self._say)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(len(self.calls), 1)

    def test_file_event_without_dedupe_key_is_dropped(self):
        ev = self._share()
        del ev["client_msg_id"], ev["ts"]
        slack_app.handle_message(ev, self._say)
        self.assertEqual((self.downloads, self.said), ([], []))

    def test_unexpected_ingest_error_is_reported_not_raised(self):
        def boom(*a, **k):
            raise RuntimeError("disk full /secret/path")

        uploads.download = boom
        slack_app.handle_message(self._share(), self._say)
        self.assertIn("something went wrong", self.said[0])
        self.assertNotIn("secret", self.said[0])


if __name__ == "__main__":
    unittest.main()
