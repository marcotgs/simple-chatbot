"""Regression tests for review-1 findings (general C1-C3, W9; security C-SEC1, C-SEC2, W-SEC4, F-1).
Offline: llm.chat is replaced by a fake. Run: python3 -m unittest tests.test_regressions -v"""

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["CALL_LOG_PATH"] = os.path.join(_tmp, "calls.jsonl")
os.environ["GATE_MODEL"] = ""  # offline: the salience gate (on by default) never hits the network
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import config, core, db, llm, memory, server  # noqa: E402
from chatbot.capabilities import docs, messaging  # noqa: E402
from chatbot.memory import doc_notes, facts, forget, retrieval  # noqa: E402
from scenarios import checks  # noqa: E402

ADDRESS = "742 Evergreen Terrace, Springfield 99999"  # synthetic (fictional)
PARTIALS = ["742 Evergreen Terrace", "Evergreen Terrace"]


class FakeLLM:
    """Replaces llm.chat. handlers: purpose -> str | callable(last user text) -> str."""

    def __init__(self, **handlers):
        self.handlers, self.calls = handlers, []

    def __call__(self, messages, tools=None, purpose="chat", thread_id=None):
        user_text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        self.calls.append(purpose)
        h = self.handlers.get(purpose, "")
        content = h(user_text) if callable(h) else h
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))]
        )


def uid():
    return f"reg-{uuid.uuid4().hex[:8]}"


def say(user_id, thread_id, text, reply="Noted."):
    core.ingest_turn(user_id, thread_id, "user", text)
    core.ingest_turn(user_id, thread_id, "assistant", reply)


def user_messages(user_id):
    return [
        r[0]
        for r in db.conn().execute(
            "SELECT content FROM messages WHERE user_id = ? ORDER BY id", (user_id,)
        )
    ]


def resolve_to(fact_ids=(), values=()):
    return json.dumps({"fact_ids": list(fact_ids), "values": list(values), "search_terms": []})


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLLM(
            chat="ok",
            extract='{"facts": []}',
            summary="Short summary.",
            forget_find='{"values": []}',
        )
        p = mock.patch.object(llm, "chat", self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.u, self.t = uid(), "slack-dm"


class ForgetNonAsciiTest(Base):
    """C-SEC1: LIKE prefilter and casefolded regex miss 'ß' / 'É'; storage must still end clean."""

    def _forget_and_scan(self, message, value, needles):
        say(self.u, self.t, message)
        self.assertNotEqual(checks.scan_db(config.DB_PATH, needles), [])  # positive control
        self.fake.handlers["forget_resolve"] = resolve_to(values=[value])
        status = forget.forget(self.u, "my address", self.t)
        leaks = {
            "db rows/fts": checks.scan_db(config.DB_PATH, needles),
            "raw files": checks.scan_files(
                [*checks.db_files(config.DB_PATH), config.CALL_LOG_PATH], needles
            ),
            "search_history": checks.text_leaks(
                retrieval.search_history(self.u, self.t, value), needles
            ),
        }
        self.assertEqual({k: v for k, v in leaks.items() if v}, {}, status)
        self.assertIn("verified clean", status)

    def test_forget_hauptstrasse_leaves_no_trace(self):
        self._forget_and_scan(
            "I live at Hauptstraße 5, Berlin", "Hauptstraße 5", ["Hauptstraße 5", "Hauptstraße"]
        )

    def test_forget_rue_emile_leaves_no_trace(self):
        # FTS unicode61 indexes 'Émile' as 'emile': the folded form is a needle too.
        self._forget_and_scan(
            "Send it to Rue Émile 12, Lyon", "Rue Émile 12", ["Rue Émile 12", "Émile", "emile"]
        )


class ForgetPunctuationTest(Base):
    """C-SEC2: a value without letters/digits compiles to an empty-match regex and corrupts every
    message."""

    HISTORY = ["Hi, there. The launch is 2026-09-17 (final).", "Wait... really?? --- ok"]

    def test_punctuation_only_request_redacts_nothing_and_says_so(self):
        for value in ("???", "---"):
            with self.subTest(value=value):
                u = uid()
                for text in self.HISTORY:
                    say(u, self.t, text)
                before = user_messages(u)
                self.fake.handlers["forget_resolve"] = resolve_to(values=[value])
                status = forget.forget(u, value, self.t)
                self.assertEqual(user_messages(u), before)
                self.assertFalse(status.startswith("Forgotten"), status)
                self.assertNotIn("verified clean", status)

    def test_punctuation_only_fact_value_leaves_messages_intact(self):
        for text in self.HISTORY:
            say(self.u, self.t, text)
        before = user_messages(self.u)
        fid = facts.upsert_fact(self.u, None, "global", "nickname", "???")
        self.fake.handlers["forget_resolve"] = resolve_to(fact_ids=[fid])
        forget.forget(self.u, "my nickname", self.t)
        self.assertEqual(user_messages(self.u), before)


class ForgetPartialMentionTest(Base):
    """C2: forgetting the full address must also scrub mentions without the city."""

    def _forget_full_address(self):
        fid = facts.upsert_fact(self.u, None, "global", "home_address", ADDRESS)
        say(self.u, self.t, f"My home address is {ADDRESS}.")
        say(self.u, self.t, "ship it to 742 Evergreen Terrace please")
        c = db.conn()
        with c:
            c.execute(
                "INSERT INTO summaries (user_id, thread_id, upto_turn, content) VALUES (?, ?, 1, "
                "?)",
                (self.u, self.t, "User lives at 742 Evergreen Terrace (turn 1)."),
            )
        self.fake.handlers["forget_resolve"] = resolve_to(fact_ids=[fid])
        return forget.forget(self.u, "my address", self.t)

    def test_partial_address_scrubbed_from_messages(self):
        status = self._forget_full_address()
        leaks = [m for m in user_messages(self.u) if checks.text_leaks(m, PARTIALS)]
        self.assertEqual(leaks, [], status)

    def test_partial_address_scrubbed_from_summaries(self):
        status = self._forget_full_address()
        rows = [
            r[0]
            for r in db.conn().execute("SELECT content FROM summaries WHERE user_id = ?", (self.u,))
        ]
        self.assertEqual([r for r in rows if checks.text_leaks(r, PARTIALS)], [], status)
        self.assertEqual(checks.text_leaks(memory.memory_text(self.u, self.t), PARTIALS), [])


class SendMessageRecipientTest(Base):
    """W9 / W-SEC4: the forgotten-data guard must cover `to`, not only `text`."""

    def test_forgotten_value_as_recipient_is_blocked(self):
        say(self.u, self.t, "my number is 555-0142, text me there")
        self.fake.handlers["forget_resolve"] = resolve_to(values=["555-0142"])
        forget.forget(self.u, "my number", self.t)
        out = messaging.send_message(self.u, self.t, to="555-0142", text="Hi, see you at six.")
        self.assertTrue(out.startswith("error"), out)
        rows = (
            db.conn()
            .execute("SELECT COUNT(*) FROM send_log WHERE user_id = ?", (self.u,))
            .fetchone()[0]
        )
        self.assertEqual(rows, 0)

    def test_digit_normalised_forms_are_blocked(self):
        # S-SEC11: the guard must not be token-exact for phone-style values.
        say(self.u, self.t, "my number is 555-0142, text me there")
        self.fake.handlers["forget_resolve"] = resolve_to(values=["555-0142"])
        forget.forget(self.u, "my number", self.t)
        for to in ("5550142", "(555) 0142", "555.0142"):
            with self.subTest(to=to):
                out = messaging.send_message(self.u, self.t, to=to, text="Hi, see you at six.")
                self.assertTrue(out.startswith("error"), out)
        out = messaging.send_message(self.u, self.t, to="Jordan", text="call 5550142 later")
        self.assertTrue(out.startswith("error"), out)


class ForgetGenericValueTest(Base):
    """W-SEC1: a resolver mistake must not create a global tombstone for a generic value."""

    def _tombstones(self):
        q = "SELECT COUNT(*) FROM forgotten WHERE user_id = ?"
        return db.conn().execute(q, (self.u,)).fetchone()[0]

    def test_short_numeric_fact_value_deletes_row_only(self):
        say(self.u, self.t, "Our launch year is 2026 and the Q3 plan ships in 2026.")
        before = user_messages(self.u)
        fid = facts.upsert_fact(self.u, None, "global", "launch_year", "2026")
        self.fake.handlers["forget_resolve"] = resolve_to(fact_ids=[fid])
        status = forget.forget(self.u, "my launch year", self.t)
        self.assertIn("Removed 1 fact(s)", status)
        self.assertNotIn("WARNING", status)  # created_at timestamps contain 2026
        self.assertEqual(facts.get_fact_history(self.u, "launch_year"), [])
        self.assertEqual(user_messages(self.u), before)
        self.assertEqual(self._tombstones(), 0)
        self.assertEqual(
            memory.filter_reply("Your dentist is on 2026-03-04"), "Your dentist is on 2026-03-04"
        )

    def test_model_returned_subject_phrase_is_rejected(self):
        say(self.u, self.t, "What is the launch date again?")
        before = user_messages(self.u)
        self.fake.handlers["forget_resolve"] = resolve_to(values=["launch date"])
        status = forget.forget(self.u, "the launch date", self.t)
        self.assertTrue(status.startswith("Nothing stored matches"), status)
        self.assertEqual(user_messages(self.u), before)
        self.assertEqual(self._tombstones(), 0)
        self.assertEqual(memory.filter_reply("The launch date is set."), "The launch date is set.")


class LiveExtractionTest(Base):
    """C1: a rephrased fact in a short live session is extracted, so another session sees it."""

    def test_rephrased_fact_in_three_turn_session_reaches_other_session(self):
        self.fake.handlers["extract"] = lambda text: json.dumps(
            {
                "facts": [
                    {
                        "key": "manager",
                        "value": "Priya Raman",
                        "scope": "global",
                        "action": "set",
                        "turn": int(re.search(r"\[turn (\d+)\][^\n]*boss now", text).group(1)),
                    }
                ]
            }
            if "boss now" in text
            else {"facts": []}
        )
        for text in (
            "Hi, quick session today.",
            "Heads up: Priya Raman is my boss now.",
            "Thanks, that's all.",
        ):
            core.send(self.u, "session-a", text)
        self.assertIn("Priya Raman", memory.memory_text(self.u, "session-b"))


class SummarizeDocRealReaderTest(Base):
    """C3: summarize_doc with the default reader (capabilities.docs) must work, not error."""

    def test_summarize_q3_plan_stores_launch_date_note(self):
        def note(text):  # a "model" that only notes the final launch line of the page it is given
            m = re.search(r"Final launch date: (\d{4}-\d{2}-\d{2})", text)
            return json.dumps(
                {
                    "summary": "page notes",
                    "key_facts": [f"Launch date (final): {m.group(1)}"] if m else [],
                }
            )

        self.fake.handlers["doc_note"] = note
        with mock.patch.object(docs, "paginate", wraps=docs.paginate) as spy:
            out = doc_notes.summarize_doc(self.u, self.t, "Q3-plan")
        self.assertFalse(out.startswith("error"), out)
        self.assertEqual(spy.call_count, 1)  # R4-S1: one pagination, not one per page
        self.assertIn("2026-09-17", doc_notes.search_doc_notes(self.u, "launch date", "Q3-plan"))


class ServerRequestGuardTest(unittest.TestCase):
    """F-1 (FR-1B): CSRF / DNS-rebinding guards with the DEFAULT config (no ALLOWED_HOSTS
    patching)."""

    def setUp(self):
        for p in (
            mock.patch.object(config, "WEB_TOKEN", ""),
            mock.patch.object(server.core, "send", lambda *a: "pong"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]
        self.host = f"127.0.0.1:{self.port}"

    def _post(self, **headers):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = b'{"thread_id": "t", "text": "hi"}'
        c.putrequest("POST", "/send", skip_host=True, skip_accept_encoding=True)
        headers = {
            "Host": self.host,
            "Content-Type": "application/json",
            **{k.replace("_", "-"): v for k, v in headers.items()},
        }
        for k, v in headers.items():
            c.putheader(k, v)
        c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        status = c.getresponse().status
        c.close()
        return status

    def test_same_origin_json_post_is_served(self):  # positive control for the three below
        self.assertEqual(self._post(), 200)

    def test_text_plain_post_is_415(self):
        self.assertEqual(self._post(Content_Type="text/plain"), 415)

    def test_foreign_origin_is_403(self):
        self.assertEqual(self._post(Origin="http://evil.example"), 403)

    def test_wrong_host_is_403(self):
        self.assertEqual(self._post(Host="evil.example"), 403)


if __name__ == "__main__":
    unittest.main()
