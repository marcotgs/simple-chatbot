"""Offline checks of the LinkedIn draft link: built in code from the validated post, redacted,
length-capped, appended to the reply (never stored), rendered safely on the web page."""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["CALL_LOG_PATH"] = os.path.join(_tmp, "calls.jsonl")
os.environ["GATE_MODEL"] = ""  # offline: the salience gate (on by default) never hits the network
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatbot import config, core, db, llm, memory, server  # noqa: E402
from chatbot.capabilities import linkedin  # noqa: E402

POST = "Launch day: we shipped the new onboarding flow & thanks to the team.\n\n#product #launch"
PREFIX = "https://www.linkedin.com/feed/?shareActive=true&text="


def _text_param(line: str) -> str:
    url = line.split(": ", 1)[1]
    return parse_qs(urlparse(url).query)["text"][0]


def call(name, args, cid):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class FakeChat:
    """Chat calls pop scripted tool-call steps, then reply `final`; the drafter gets `post`."""

    def __init__(self, steps, post=POST, final="Here is your post."):
        self.steps, self.post, self.final = list(steps), post, final

    def __call__(self, messages, tools=None, purpose="chat", thread_id=None):
        if purpose == "linkedin":
            msg = SimpleNamespace(content=self.post, tool_calls=None)
        elif purpose == "chat":
            tcs = self.steps.pop(0) if self.steps else None
            msg = SimpleNamespace(content=None if tcs else self.final, tool_calls=tcs)
        else:
            msg = SimpleNamespace(content="", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def draft_steps():
    return [
        [call("load_capability", {"name": "linkedin"}, "c1")],
        [call("write_linkedin_post", {"brief": "launch"}, "c2")],
    ]


def uid():
    return f"draft-{uuid.uuid4().hex[:8]}"


class DraftLineTest(unittest.TestCase):
    def test_link_is_linkedin_composer_with_exact_post(self):
        line = linkedin.draft_line(POST)
        self.assertTrue(line.startswith(f"{linkedin.DRAFT_LABEL}: {PREFIX}"), line)
        self.assertEqual(_text_param(line), POST)
        url = line.split(": ", 1)[1]
        self.assertNotIn(" ", url)
        self.assertEqual(url.count("&"), 1)  # the post's own '&' is percent-encoded

    def test_long_post_falls_back_to_copy_text(self):
        post = "é" * 1200  # valid length, but ~7200 chars once percent-encoded
        line = linkedin.draft_line(post)
        self.assertNotIn("http", line)
        self.assertIn("copy the text above", line)


class SendAppendsLinkTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(memory, "after_turn", lambda *a: None)
        p.start()
        self.addCleanup(p.stop)

    def _stored_reply(self, user):
        return (
            db.conn()
            .execute(
                "SELECT content FROM messages WHERE user_id = ? AND role = 'assistant'", (user,)
            )
            .fetchone()[0]
        )

    def test_reply_gets_link_but_stored_reply_does_not(self):
        user = uid()
        with mock.patch.object(llm, "chat", FakeChat(draft_steps())):
            reply = core.send(user, "t", "write a post")
        self.assertEqual(reply, "Here is your post.\n\n" + linkedin.draft_line(POST))
        self.assertEqual(self._stored_reply(user), "Here is your post.")

    def test_no_link_without_a_validated_post(self):
        cases = {
            "no tool": ([], POST),
            "invalid post": (draft_steps(), "see https://example.com " * 3),
        }
        for name, (steps, post) in cases.items():
            with self.subTest(name), mock.patch.object(llm, "chat", FakeChat(steps, post)):
                self.assertNotIn("linkedin.com", core.send(uid(), "t", "hi"))

    def test_draft_from_a_failed_turn_does_not_leak_into_the_next(self):
        user = uid()
        with mock.patch.object(llm, "chat", FakeChat([])):
            linkedin._drafts[(user, "t")] = POST  # left over by a turn that raised
            self.assertNotIn("linkedin.com", core.send(user, "t", "hi"))

    def test_model_edited_post_validated_by_tool_gets_the_link(self):
        edited = "Short edited version.\n\n#launch"
        steps = [
            [call("load_capability", {"name": "linkedin"}, "c1")],
            [call("validate_linkedin_post", {"text": edited}, "c2")],
        ]
        with mock.patch.object(llm, "chat", FakeChat(steps)):
            reply = core.send(uid(), "t", "shorten it")
        self.assertEqual(_text_param(reply.rsplit("\n\n", 1)[1]), edited)

    def test_forgotten_value_never_reaches_the_url(self):
        secret = "quillon-test-8841"
        post = f"Big news from {secret} today.\n\n#launch"
        redact = lambda t: t.replace(secret, "[forgotten]")  # noqa: E731
        with (
            mock.patch.object(llm, "chat", FakeChat(draft_steps(), post)),
            mock.patch.object(memory, "filter_reply", redact),
        ):
            reply = core.send(uid(), "t", "write a post")
        self.assertIn(linkedin.DRAFT_LABEL, reply)
        self.assertNotIn(secret, reply)
        self.assertNotIn("quillon", reply)  # not even percent-encoded
        self.assertIn("Big news from [forgotten] today.", _text_param(reply.rsplit("\n\n", 1)[1]))


class ServerSendTest(unittest.TestCase):
    """POST /send returns core.send's reply: the stored text plus the appended draft line."""

    def test_send_response_is_reply_plus_link(self):
        patches = [
            mock.patch.object(config, "WEB_TOKEN", ""),
            mock.patch.object(memory, "after_turn", lambda *a: None),
            mock.patch.object(llm, "chat", FakeChat(draft_steps())),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"thread_id": f"t-{uid()}", "text": "write a post"})
        conn.request(
            "POST",
            "/send",
            body,
            {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"},
        )
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        reply = json.loads(r.read())["reply"]
        conn.close()
        self.assertEqual(reply, "Here is your post.\n\n" + linkedin.draft_line(POST))


class WebPageTest(unittest.TestCase):
    def test_page_renders_only_linkedin_https_links_without_innerhtml(self):
        html = (ROOT / "web" / "index.html").read_text()
        self.assertIn('u.protocol === "https:" && u.hostname === "www.linkedin.com"', html)
        self.assertIn('rel = "noopener noreferrer"', html)
        self.assertIn(linkedin.DRAFT_LABEL.replace("(", "\\(").replace(")", "\\)"), html)
        self.assertNotIn("innerHTML =", html)


if __name__ == "__main__":
    unittest.main()
