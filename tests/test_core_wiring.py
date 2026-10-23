"""Offline checks of core wiring: tool registration, per-send tool exposure, context assembly,
reply filtering and the budget-exceeded rollback. Run: python3 -m unittest discover tests"""

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
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["CALL_LOG_PATH"] = os.path.join(_tmp, "calls.jsonl")
os.environ["GATE_MODEL"] = ""  # offline: the salience gate (on by default) never hits the network
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import config, core, db, llm, memory  # noqa: E402
from chatbot.memory import facts  # noqa: E402

MEMORY_TOOLS = {"search_history", "search_doc_notes", "summarize_doc", "get_fact_history", "forget"}


def call(name, args="{}", cid="c1"):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))


class FakeChat:
    """Replaces llm.chat. Chat calls pop scripted tool-call steps, then reply 'done';
    every other purpose (extract, summary) gets an empty answer."""

    def __init__(self, steps=()):
        self.steps = list(steps)
        self.chat_tools = []  # tool names offered on each chat call
        self.chat_messages = []

    def __call__(self, messages, tools=None, purpose="chat", thread_id=None):
        if purpose == "chat":
            self.chat_tools.append({t["function"]["name"] for t in tools or []})
            self.chat_messages.append(messages)
            tcs = self.steps.pop(0) if self.steps else None
            msg = SimpleNamespace(content=None if tcs else "done", tool_calls=tcs)
        else:
            msg = SimpleNamespace(content="", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def uid():
    return f"wiring-{uuid.uuid4().hex[:8]}"


class RegistrationTest(unittest.TestCase):
    def test_tools_registered_on_import(self):
        self.assertTrue(
            {"load_capability", "fetch_doc", "send_message", "write_linkedin_post"} | MEMORY_TOOLS
            <= set(core.TOOLS)
        )


class ExposureTest(unittest.TestCase):
    def test_capability_tools_only_after_load_and_reset_per_send(self):
        fake = FakeChat(
            [
                [call("fetch_doc", '{"doc_id": "Q3-plan"}')],
                [call("load_capability", '{"name": "docs"}')],
                [call("fetch_doc", '{"doc_id": "Q3-plan"}')],
            ]
        )
        user = uid()
        with mock.patch.object(llm, "chat", fake):
            self.assertEqual(core.send(user, "t", "read Q3-plan"), "done")
            first, after_load = fake.chat_tools[0], fake.chat_tools[2]
            self.assertEqual(first, MEMORY_TOOLS | {"load_capability"})
            self.assertIn("fetch_doc", after_load)
            self.assertNotIn("send_message", after_load)  # other capabilities stay hidden
            errors = [m["content"] for m in fake.chat_messages[1] if m["role"] == "tool"]
            self.assertIn("load_capability", errors[0])
            self.assertTrue(errors[0].startswith("error"))
            results = [m["content"] for m in fake.chat_messages[3] if m["role"] == "tool"]
            self.assertIn("[Q3-plan page 1/", results[-1])

            core.send(user, "t", "again")
            self.assertNotIn("fetch_doc", fake.chat_tools[-1])  # loaded set reset per send


class ContextTest(unittest.TestCase):
    def test_single_system_message_has_prompt_index_and_memory(self):
        user = uid()
        facts.upsert_fact(user, None, "global", "manager", "Ada Example")
        core.ingest_turn(user, "t", "user", "hello")
        msgs = core.build_context(user, "t")
        self.assertEqual([m["role"] for m in msgs].count("system"), 1)
        system = msgs[0]["content"]
        self.assertIn(core.SYSTEM_PROMPT, system)
        self.assertIn("load_capability", system)
        self.assertIn("Ada Example", system)
        self.assertEqual(msgs[-1]["content"], "hello")

    def test_reply_is_filtered_before_store_and_return(self):
        user = uid()
        with (
            mock.patch.object(llm, "chat", FakeChat()),
            mock.patch.object(memory, "filter_reply", lambda t: t.replace("done", "[forgotten]")),
        ):
            self.assertEqual(core.send(user, "t", "hi"), "[forgotten]")
        stored = (
            db.conn()
            .execute(
                "SELECT content FROM messages WHERE user_id = ? AND role = 'assistant'", (user,)
            )
            .fetchone()[0]
        )
        self.assertEqual(stored, "[forgotten]")


class SinglePathTest(unittest.TestCase):
    def test_send_and_loader_share_after_turn(self):
        seen = []
        rec = lambda u, t, n: seen.append(n)  # noqa: E731
        user = uid()
        with (
            mock.patch.object(llm, "chat", FakeChat()),
            mock.patch.object(memory, "after_turn", rec),
        ):
            core.send(user, "t", "hi")
            core.ingest_turn(user, "t", "user", "old question")
            core.ingest_turn(user, "t", "assistant", "old answer")
        self.assertEqual(seen, [1, 2])  # one after_turn per closed turn, same call either way


class TrimNoteTest(unittest.TestCase):
    def _fill(self, user, n_turns, size):
        with mock.patch.object(memory, "after_turn", lambda *a, **k: None):
            for i in range(n_turns):
                core.ingest_turn(user, "t", "user", f"q{i} " + "x" * size)
                core.ingest_turn(user, "t", "assistant", f"a{i} " + "y" * size)

    def test_over_budget_window_is_shortened_not_dropped(self):
        # The summary only covers turns older than RECENT_TURNS, so dropping a message from the
        # window would lose it from context and summary alike. Shorten long ones instead.
        user = uid()
        self._fill(user, core.RECENT_TURNS // 2, 3000)
        msgs = core.build_context(user, "t")
        self.assertEqual(len(msgs), core.RECENT_TURNS + 1)  # whole window kept...
        self.assertTrue(msgs[1]["content"].startswith("q0 "))
        self.assertLessEqual(llm.estimate_tokens(msgs), config.TARGET_INPUT_TOKENS)
        self.assertIn(core.TRIM_NOTE, msgs[0]["content"])  # ...and the model is told where to look
        self.assertEqual(msgs[0]["content"].count(core.TRIM_NOTE), 1)
        again = core.trim_to_budget(msgs, target=int(config.TARGET_INPUT_TOKENS * 0.7))
        self.assertEqual(
            again[0]["content"].count(core.TRIM_NOTE), 1
        )  # retry trim does not stack notes
        self.assertLessEqual(llm.estimate_tokens(again), int(config.TARGET_INPUT_TOKENS * 0.7))

    def test_mark_matches_what_can_recover_the_text(self):
        # search_history cannot return tool results (never stored) or the current turn (excluded).
        tc = [
            {"id": "c1", "type": "function", "function": {"name": "fetch_doc", "arguments": "{}"}}
        ]
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "old " + "u" * 3000},
            {"role": "assistant", "content": "old " + "a" * 3000},
            {"role": "user", "content": "current " + "c" * 3000},
            {"role": "assistant", "content": "", "tool_calls": tc},
            {"role": "tool", "tool_call_id": "c1", "content": "page " + "p" * 6000},
            {"role": "tool", "tool_call_id": "c2", "content": "ok"},
        ]
        out = core.trim_to_budget(msgs, target=2000)
        self.assertEqual(len(out), len(msgs))
        self.assertTrue(out[1]["content"].endswith(core.SHORTENED_MARK))
        self.assertEqual(out[3]["content"], msgs[3]["content"])  # current user message intact
        self.assertTrue(out[5]["content"].endswith(core.TOOL_SHORTENED_MARK))
        self.assertNotIn("search_history", out[5]["content"])

    def test_no_note_when_nothing_dropped(self):
        user = uid()
        with mock.patch.object(memory, "after_turn", lambda *a, **k: None):
            core.ingest_turn(user, "t", "user", "hello")
        self.assertNotIn(core.TRIM_NOTE, core.build_context(user, "t")[0]["content"])


class ForgetInLoopTest(unittest.TestCase):
    """After forget runs, later steps of the same send must not see the value anywhere."""

    VALUE = "742 Synthetic Lane"

    def test_context_is_rebuilt_and_redacted_after_forget(self):
        state = {"forgotten": False}

        def forget(user_id, thread_id, subject):
            state["forgotten"] = True
            return "Forgot it. Storage verified clean."

        def redact(text):
            return (
                (text or "").replace(self.VALUE, "[forgotten]")
                if state["forgotten"]
                else text or ""
            )

        seen = []

        def fake(messages, tools=None, purpose="chat", thread_id=None):
            if purpose != "chat":
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=None))]
                )
            seen.append(json.dumps(messages))
            if len(seen) == 1:
                msg = SimpleNamespace(
                    content=f"Forgetting {self.VALUE} now.",
                    tool_calls=[call("forget", json.dumps({"subject": self.VALUE}))],
                )
            else:
                msg = SimpleNamespace(content=f"Done, {self.VALUE} is gone.", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        tools = {**core.TOOLS, "forget": {"schema": core.TOOLS["forget"]["schema"], "fn": forget}}
        with (
            mock.patch.object(llm, "chat", fake),
            mock.patch.dict(core.TOOLS, tools),
            mock.patch.object(memory, "filter_reply", redact),
            mock.patch.object(
                memory,
                "memory_text",
                lambda u, t: "" if state["forgotten"] else f"- address: {self.VALUE}",
            ),
        ):
            reply = core.send(uid(), "t", f"I live at {self.VALUE}. Please forget my address.")
        self.assertIn(self.VALUE, seen[0])  # sanity: it was in the first call
        self.assertNotIn(self.VALUE, seen[1])  # system block, user turn, assistant text, tool args
        self.assertNotIn(self.VALUE, reply)


class BudgetRollbackTest(unittest.TestCase):
    def test_budget_exceeded_leaves_no_orphan_user_message(self):
        user, word = uid(), f"orphanprobe{uuid.uuid4().hex[:6]}"
        # A memory block that alone blows the budget: messages[0] cannot be trimmed.
        with mock.patch.object(memory, "memory_text", lambda u, t: "x" * 40000):
            with self.assertRaises(llm.BudgetExceeded):
                core.send(user, "t", f"tell me about {word}")
        c = db.conn()
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM messages WHERE user_id = ?", (user,)).fetchone()[0], 0
        )
        self.assertEqual(
            c.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?", (word,)
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
