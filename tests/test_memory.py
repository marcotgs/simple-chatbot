"""Offline memory checks with a fake model (llm.chat patched). Run: python3 -m unittest discover tests"""
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

_tmp = tempfile.mkdtemp()
os.environ.update({
    "DB_PATH": os.path.join(_tmp, "test.db"),
    "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
    "MAX_INPUT_TOKENS": "8000",
    "TARGET_INPUT_TOKENS": "6500",
    "OPENROUTER_API_KEY": "test-not-a-key",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import config, core, db, llm, memory  # noqa: E402
from chatbot.memory import doc_notes, extract, facts, forget, retrieval, summary  # noqa: E402

ADDRESS = "17 Fakeham Lane, Testville"  # synthetic


class FakeLLM:
    """Replaces llm.chat. handlers: purpose -> str | callable(user_text) -> str."""

    def __init__(self, **handlers):
        self.handlers = handlers
        self.calls = []  # (purpose, estimated_tokens, user_text)

    def __call__(self, messages, tools=None, purpose="chat", thread_id=None):
        user_text = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        self.calls.append((purpose, llm.estimate_tokens(messages, tools), user_text))
        h = self.handlers.get(purpose, "")
        content = h(user_text) if callable(h) else h
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))])

    def count(self, purpose):
        return sum(1 for p, _, _ in self.calls if p == purpose)


# Rule-based stand-in for the extractor: phrase in a message line -> fact.
RULES = {
    "My manager is Priya Raman": {"key": "manager", "value": "Priya Raman", "scope": "global"},
    "Always use British spelling": {"key": "spelling_preference", "value": "British spelling", "scope": "global"},
    "Switch to American spelling": {"key": "spelling_preference", "value": "American spelling", "scope": "global"},
    "For this draft only, make it playful": {"key": "tone", "value": "playful", "scope": "ephemeral"},
    "In this chat, keep it formal": {"key": "tone", "value": "formal", "scope": "session"},
    "I live at": {"key": "home_address", "value": ADDRESS, "scope": "global"},
}


def fake_extract(user_text):
    out = []
    for line in user_text.split("\n"):
        if not line.startswith("[turn "):
            continue
        turn = int(line[6:line.index("]")])
        out += [{**f, "action": "set", "turn": turn} for phrase, f in RULES.items() if phrase in line]
    return json.dumps({"facts": out})


def echo_summary(user_text):
    """Worst case summariser: copies previous summary + transcript verbatim, so any unredacted value leaks."""
    return user_text.replace("PREVIOUS SUMMARY:\n", "", 1)


def uid():
    return f"u-{uuid.uuid4().hex[:8]}"


def turn(user_id, thread_id, text, reply="ok"):
    core.ingest_turn(user_id, thread_id, "user", text)
    core.ingest_turn(user_id, thread_id, "assistant", reply)
    n = db.conn().execute("SELECT MAX(turn_no) FROM messages WHERE user_id = ? AND thread_id = ?",
                          (user_id, thread_id)).fetchone()[0]
    memory.after_turn(user_id, thread_id, n)  # idempotent if core already wired it
    return n


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self._chat, self._batch = llm.chat, config.FACT_EXTRACT_BATCH
        self.fake = FakeLLM(extract=fake_extract, summary=echo_summary)
        llm.chat = self.fake

    def tearDown(self):
        llm.chat, config.FACT_EXTRACT_BATCH = self._chat, self._batch
        over = [c for c in self.fake.calls if c[1] > config.TARGET_INPUT_TOKENS]
        self.assertEqual(over, [], "a memory side call exceeded the input budget")

    # S1
    def test_global_fact_visible_in_every_thread(self):
        u = uid()
        turn(u, "session-a", "Quick note: My manager is Priya Raman.")
        self.assertEqual(self.fake.count("extract"), 1)  # heuristic fired before the batch
        for thread in ("session-b", "slack-dm"):
            self.assertIn("manager: Priya Raman", memory.memory_text(u, thread))

    # S4
    def test_supersession_chain_and_history(self):
        u = uid()
        turn(u, "slack-dm", "Always use British spelling")
        turn(u, "slack-dm", "lunch was nice")
        turn(u, "web-1", "Switch to American spelling")
        active = [f for f in facts.get_active_facts(u, "slack-dm") if f["key"] == "spelling_preference"]
        self.assertEqual([f["value"] for f in active], ["American spelling"])
        hist = facts.get_fact_history(u, "spelling")
        self.assertEqual([(h["value"], h["status"]) for h in hist],
                         [("British spelling", "superseded"), ("American spelling", "current")])
        self.assertEqual(hist[0]["superseded_by"], hist[1]["id"])
        self.assertIsNotNone(hist[0]["valid_to"])
        ctx = memory.memory_text(u, "slack-dm")
        self.assertIn("American spelling", ctx)
        self.assertNotIn("British", ctx)
        tool_out = memory.tools._get_fact_history(u, "slack-dm", key_or_query="what did I say about spelling before?")
        self.assertIn("British spelling [superseded", tool_out)

    def test_same_value_is_not_a_new_version(self):
        u = uid()
        a = facts.upsert_fact(u, None, "global", "Manager", "Priya Raman")
        b = facts.upsert_fact(u, None, "global", "manager", "priya raman")
        self.assertEqual(a, b)

    # S5
    def test_scope_isolation(self):
        u = uid()
        turn(u, "session-a", "For this draft only, make it playful")
        self.assertEqual(facts.get_fact_history(u, "tone"), [])  # ephemeral: never stored
        turn(u, "session-a", "In this chat, keep it formal")
        self.assertIn("tone: formal", memory.memory_text(u, "session-a"))
        self.assertNotIn("formal", memory.memory_text(u, "session-b"))
        self.assertEqual(facts.get_fact_history(u, "tone", thread_id="session-b"), [])
        self.assertEqual(retrieval.search_history(u, "session-b", "playful formal"), "No earlier messages match.")

    def test_batch_gating(self):
        u, t = uid(), "slack-dm"
        config.FACT_EXTRACT_BATCH = 5
        for i in range(4):
            turn(u, t, f"weather chat {i}")
        self.assertEqual(self.fake.count("extract"), 0)
        turn(u, t, "more weather")
        self.assertEqual(self.fake.count("extract"), 1)
        self.assertEqual(self.fake.calls[-1][2].count("[turn "), 5)  # all 5 pending turns in one call
        turn(u, t, "Remember: My manager is Priya Raman")  # trigger: immediate, only the new turn
        self.assertEqual(self.fake.count("extract"), 2)
        self.assertEqual(self.fake.calls[-1][2].count("[turn "), 1)
        config.FACT_EXTRACT_BATCH = 1
        turn(u, t, "sunny again")
        self.assertEqual(self.fake.count("extract"), 3)

    def test_bad_json_never_breaks_a_turn(self):
        u = uid()
        self.fake.handlers["extract"] = "sure! here you go {not json"
        turn(u, "t", "My manager is Priya Raman")  # must not raise
        self.assertEqual(facts.get_active_facts(u, "t"), [])
        self.assertEqual(extract._cursor(u, "t"), 1)  # skipped, not retried forever
        self.fake.handlers["extract"] = ('```json\n{"facts": [{"key": "manager", "value": "Priya Raman",'
                                         ' "scope": "global", "turn": 2},]}\n```')
        turn(u, "t", "My manager is Priya Raman")
        self.assertEqual([f["value"] for f in facts.get_active_facts(u, "t")], ["Priya Raman"])

    def test_model_error_is_contained(self):
        def boom(_):
            raise llm.BudgetExceeded("simulated")
        self.fake.handlers["extract"] = boom
        turn(uid(), "t", "My manager is Priya Raman")  # no exception escapes after_turn

    # S2 + safe FTS
    def test_fts_safe_query_and_old_detail(self):
        u, t = uid(), "slack-dm"
        c = db.conn()
        with c:
            for i in range(1, 501):
                text = "The locker code at the Oslo office is 7319" if i == 20 else f"status update number {i}"
                c.execute("INSERT INTO messages (user_id, thread_id, role, content, turn_no) VALUES (?, ?, 'user', ?, ?)",
                          (u, t, text, i))
        c.commit()
        out = retrieval.search_history(u, t, "what was the locker code?")
        self.assertTrue(out.startswith("[turn 20 user]"), out)
        self.assertIn("7319", out)
        for hostile in ['"', '""', 'AND OR NOT', 'NEAR(a b, 2)', 'code*', 'content:code', '^code', ')(', "'; DROP TABLE messages;--",
                        'code" OR "x', '* : " ^', '']:
            self.assertIsInstance(retrieval.search_history(u, t, hostile), str)
            q = retrieval.fts_query(hostile)
            self.assertTrue(all(p.startswith('"') and p.endswith('"') and p.count('"') == 2
                                for p in q.split(" OR ") if q), q)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM messages WHERE user_id = ?", (u,)).fetchone()[0], 500)

    def test_retrieval_result_is_capped(self):
        u, t = uid(), "t"
        for i in range(12):
            turn(u, t, "budget " * 400)
        turn(u, t, "question")
        out = retrieval.search_history(u, t, "budget", limit=50)
        self.assertLessEqual(len(out), retrieval.MAX_RESULT_CHARS)

    def test_summary_rolls_within_budget_and_cap(self):
        u, t = uid(), "slack-dm"
        for i in range(40):
            turn(u, t, f"turn detail {i} " + "x" * 600)
        s = summary.get_summary(u, t)
        self.assertIsNotNone(s)
        self.assertLessEqual(len(s["content"]), summary.SUMMARY_MAX_CHARS)
        self.assertLess(s["upto_turn"], 40 - core.RECENT_TURNS // 2 + 1)  # recent window not summarised
        self.assertGreater(self.fake.count("summary"), 0)

    # S6
    def test_forget_purges_everywhere_and_guards_output(self):
        u, dm = uid(), "slack-dm"
        turn(u, dm, f"I live at {ADDRESS}, near the park")
        for i in range(30):
            turn(u, dm, f"filler message {i}")
        turn(u, "web-2", f"send the parcel to {ADDRESS.upper()}")
        self.assertIn(ADDRESS, summary.get_summary(u, dm)["content"])  # echo summariser copied it
        c = db.conn()
        with c:
            c.execute("INSERT INTO doc_notes (user_id, doc_id, page, kind, content) VALUES (?, 'notes', 1, 'key_fact', ?)",
                      (u, f"Home: {ADDRESS}"))
            c.execute("INSERT INTO send_log (user_id, recipient, text) VALUES (?, 'partner', ?)",
                      (u, f"I'm at 17 fakeham lane , testville"))
        fid = next(f["id"] for f in facts.get_active_facts(u, dm) if f["key"] == "home_address")
        self.fake.handlers["forget_resolve"] = json.dumps({"fact_ids": [fid], "values": [], "search_terms": ["live"]})

        status = memory.tools._forget(u, dm, subject="my address")
        self.assertNotIn("Fakeham", status)
        self.assertIn("verified clean", status)
        self.assertEqual(forget.verify_clean(ADDRESS), [])
        self.assertEqual(facts.get_fact_history(u, "address"), [])
        vocab = c.execute("SELECT COUNT(*) FROM temp.messages_fts_vocab WHERE term IN ('fakeham', 'testville')").fetchone()[0]
        self.assertEqual(vocab, 0)  # FTS index terms gone too
        self.assertEqual(Path(str(config.DB_PATH) + "-wal").stat().st_size, 0)
        self.assertNotIn("akeham", summary.get_summary(u, dm)["content"])  # re-rolled from redacted messages
        self.assertNotIn("Fakeham", memory.memory_text(u, dm))
        self.assertNotIn("akeham", retrieval.search_history(u, dm, "fakeham live"))
        db_row = c.execute("SELECT digest FROM forgotten WHERE user_id = ?", (u,)).fetchone()[0]
        self.assertNotIn("akeham", db_row)  # tombstone is a hash

        self.assertTrue(forget.contains_forgotten("Sure, it's 17 FAKEHAM lane,   Testville!"))
        self.assertTrue(forget.contains_forgotten("They live at 17 Fakeham Ln."))  # abbreviation variant
        self.assertFalse(forget.contains_forgotten("Ship it to 10 Downing Street, London"))
        self.assertFalse(forget.contains_forgotten("Fakeham is a nice name"))
        self.assertEqual(memory.filter_reply("You live at 17 Fakeham Lane, Testville."), "You live at [forgotten].")

        # Later extraction cannot reintroduce it.
        turn(u, dm, f"I live at {ADDRESS}")
        self.assertFalse(any(f["key"] == "home_address" for f in facts.get_active_facts(u, dm)))

    def test_forget_without_fact_finds_value_in_messages(self):
        u, t = uid(), "slack-dm"
        c = db.conn()
        with c:
            c.execute("INSERT INTO messages (user_id, thread_id, role, content, turn_no) VALUES (?, ?, 'user', ?, 1)",
                      (u, t, "my locker pin is 4455-9911 btw"))
        self.fake.handlers["forget_resolve"] = json.dumps({"fact_ids": [], "values": [], "search_terms": ["locker", "pin"]})
        self.fake.handlers["forget_find"] = json.dumps({"values": ["4455-9911", "made-up-value"]})
        status = forget.forget(u, "my locker pin", t)
        self.assertIn("1 value(s)", status)  # hallucinated value rejected
        self.assertEqual(forget.verify_clean("4455-9911"), [])
        self.assertTrue(forget.contains_forgotten("pin 4455 9911"))

    # S3
    def test_doc_notes_keep_launch_date(self):
        u = uid()
        self.fake.handlers["doc_note"] = lambda text: json.dumps({
            "summary": "Plan overview" if "PAGE 1" in text else "Timeline",
            "key_facts": ["Beta date: 2026-09-01"] if "PAGE 1" in text else ["Launch date (public launch): 2026-11-03",
                                                                             "Review date: 2026-10-15"]})
        out = doc_notes.summarize_doc(u, "slack-dm", "Q3-plan", read_page=lambda d, p: f"page {p} text",
                                      page_count=lambda d: 2)
        self.assertIn("2026-11-03", out)
        self.assertEqual(self.fake.count("doc_note"), 2)
        doc_notes.summarize_doc(u, "slack-dm", "Q3-plan", read_page=lambda d, p: "x", page_count=lambda d: 2)
        self.assertEqual(self.fake.count("doc_note"), 2)  # cached per page
        hit = doc_notes.search_doc_notes(u, "what was the launch date in that plan?")
        self.assertTrue(hit.startswith("[Q3-plan p2 key_fact] Launch date"), hit)
        self.assertIn("Q3-plan", memory.memory_text(u, "another-thread"))

    def test_summarize_doc_without_reader_is_a_clear_error(self):
        sys.modules["chatbot.capabilities.docs"] = None  # makes the import fail
        try:
            out = memory.tools._summarize_doc(uid(), "t", doc_id="Q3-plan")
        finally:
            del sys.modules["chatbot.capabilities.docs"]
        self.assertTrue(out.startswith("error: document reader unavailable"), out)

    def test_register_all(self):
        before = dict(core.TOOLS)
        try:
            memory.tools.register_all()
            self.assertTrue({"search_history", "search_doc_notes", "summarize_doc", "get_fact_history",
                             "forget"} <= set(core.TOOLS))
        finally:
            core.TOOLS.clear()
            core.TOOLS.update(before)


if __name__ == "__main__":
    unittest.main()
