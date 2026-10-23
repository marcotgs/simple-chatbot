"""Offline checks for chatbot/capabilities. Run: python3 -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["CALL_LOG_PATH"] = os.path.join(_tmp, "calls.jsonl")
os.environ["GATE_MODEL"] = ""  # offline: the salience gate (on by default) never hits the network
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatbot import capabilities, db, llm  # noqa: E402
from chatbot.capabilities import docs, linkedin, messaging  # noqa: E402

CLEAN = (
    "We shipped the new onboarding flow this week. Thanks to the whole team.\n\n#product #launch"
)


class LinkedInCheckerTest(unittest.TestCase):
    def test_rules_table(self):
        tags3 = " #a #b #c"
        cases = [
            ("clean post", CLEAN, True),
            ("exactly 1300 chars", "x" * 1300, True),
            ("1301 chars", "x" * 1301, False),
            ("3 hashtags at end", "Great quarter." + tags3, True),
            ("4 hashtags", "Great quarter." + tags3 + " #d", False),
            ("hashtag mid-text", "Great #quarter for the team.", False),
            ("text after hashtags", "Great quarter. #team thanks all", False),
            ("comma-separated tags at end", "Great quarter.\n\n#One, #Two, #Three", True),
            ("relative markdown link", "Read the [docs](/q3) today.", False),
            ("markdown link to bare path", "See [plan](q3-plan) for details.", False),
            ("http link", "Read more at http://example.org/x", False),
            ("https link", "Read more at https://example.org/x", False),
            ("www link", "Read more at www.example", False),
            ("bare .com", "Visit foo.com/ for details", False),
            ("bare .io", "Visit foo.io today", False),
            ("empty", "", False),
        ]
        for label, text, ok in cases:
            with self.subTest(label):
                self.assertEqual(
                    linkedin.check_linkedin_post(text) == [], ok, linkedin.check_linkedin_post(text)
                )

    def test_validate_tool(self):
        self.assertEqual(linkedin.validate_linkedin_post("u", "t", text=CLEAN), "valid")
        self.assertTrue(
            linkedin.validate_linkedin_post("u", "t", text="x" * 1301).startswith("invalid")
        )


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = SimpleNamespace(send=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=self.replies.pop(0), tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
        )


class LinkedInGenerateTest(unittest.TestCase):
    def tearDown(self):
        llm._client = None

    def test_regenerates_until_valid(self):
        llm._client = FakeClient(["see https://x.org #a", CLEAN])
        self.assertEqual(linkedin.generate_linkedin_post("launch", "date: 2026-09-17", "t1"), CLEAN)
        second = llm._client.calls[1]["messages"][-1]["content"]
        self.assertIn("http(s)", second)  # violations fed back

    def test_gives_up_after_retries(self):
        llm._client = FakeClient(["x" * 2000] * 4)
        out = linkedin.generate_linkedin_post("launch")
        self.assertTrue(out.startswith("error:"))
        self.assertEqual(len(llm._client.calls), 1 + linkedin.MAX_REGENERATIONS)

    def test_budget_exceeded_is_caught(self):
        llm._client = FakeClient([CLEAN])
        out = linkedin.generate_linkedin_post("y" * 40000)
        self.assertTrue(out.startswith("error:"))
        self.assertEqual(llm._client.calls, [])

    def test_source_doc_facts_are_only_the_callers(self):
        # Every page of a real doc is noted for the owner, so the drafter is the only LLM call.
        rows = [
            ("li-owner", p, "key_fact", f"owner fact p{p}: launch 2026-09-17")
            for p in range(1, docs.page_count("Q3-plan") + 1)
        ]
        c = db.conn()
        with c:
            c.executemany(
                "INSERT INTO doc_notes (user_id, doc_id, page, kind, content)"
                " VALUES (?, 'Q3-plan', ?, ?, ?)",
                [*rows, ("li-other", 1, "key_fact", "other user secret fact")],
            )
        llm._client = FakeClient([CLEAN])
        self.assertEqual(linkedin.write_linkedin_post("li-owner", "t1", "launch", "Q3-plan"), CLEAN)
        prompt = llm._client.calls[0]["messages"][-1]["content"]
        self.assertIn("owner fact", prompt)
        self.assertNotIn("other user", prompt)

    def test_source_doc_facts_from_another_thread(self):
        # Notes are cached per user (summarize_doc skips noted pages), so thread B must see A's.
        c = db.conn()
        with c:
            c.execute(
                "INSERT INTO doc_notes (user_id, thread_id, doc_id, page, kind, content) VALUES"
                " ('li-x', 'thread-a', 'li-doc2', 1, 'key_fact', 'noted in thread A')"
            )
        self.assertIn("noted in thread A", linkedin._doc_key_facts("li-x", "li-doc2"))


class LinkedInDocFactsTest(unittest.TestCase):
    """B9: the drafter gets facts from every page, the relevant ones first, and never drafts a
    doc post without facts."""

    def test_facts_cover_every_page_and_put_the_relevant_one_first(self):
        user = "li-b9-seeded"
        rows = [
            (user, "li-b9", page, "key_fact", f"p{page} metric {i}: {page * 100 + i} units")
            for page in range(1, 13)
            for i in range(15)
        ]
        rows[5 * 15 + 14] = (user, "li-b9", 6, "key_fact", "Public launch date: 2026-09-17")
        c = db.conn()
        with c:
            c.executemany(
                "INSERT INTO doc_notes (user_id, doc_id, page, kind, content)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        facts = linkedin._doc_key_facts(user, "li-b9", "post about the public launch")
        self.assertTrue(facts.startswith("- Public launch date: 2026-09-17"), facts[:200])
        for page in range(1, 13):
            self.assertIn(f"- p{page} metric 0:", facts)
        self.assertLessEqual(len(facts), linkedin.MAX_FACTS_CHARS)
        self.assertTrue(all(line.startswith("- ") for line in facts.split("\n")))  # whole lines

    def _fake_chat(self, note):
        calls = []

        def chat(messages, tools=None, purpose="chat", thread_id=None, **_):
            calls.append(purpose)
            if purpose == "doc_note":
                page = messages[-1]["content"].split("PAGE ", 1)[1].split(":", 1)[0]
                content = note(page)
            else:
                content = CLEAN
            msg = SimpleNamespace(content=content, tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

        chat.calls = calls
        return chat

    def test_unnoted_doc_is_noted_before_drafting(self):
        fake = self._fake_chat(
            lambda page: json.dumps({"summary": "s", "key_facts": [f"fact from page {page}"]})
        )
        with mock.patch.object(llm, "chat", fake):
            out = linkedin.write_linkedin_post("li-b9-fresh", "t1", "launch post", "Q3-plan")
        self.assertEqual(out, CLEAN)
        pages = docs.page_count("Q3-plan")
        self.assertEqual(fake.calls, ["doc_note"] * pages + ["linkedin"])
        self.assertEqual(
            len(
                db.conn()
                .execute(
                    "SELECT DISTINCT page FROM doc_notes WHERE user_id = 'li-b9-fresh'"
                    " AND doc_id = 'Q3-plan'"
                )
                .fetchall()
            ),
            pages,
        )

    def test_partly_noted_doc_gets_its_missing_pages_noted(self):
        """R7-S2: a doc with notes on some pages (an earlier summarize_doc failed mid-way) still
        gets the rest noted before drafting."""
        user = "li-b9-partial"
        c = db.conn()
        with c:
            c.execute(
                "INSERT INTO doc_notes (user_id, doc_id, page, kind, content)"
                " VALUES (?, 'Q3-plan', 1, 'key_fact', 'fact from page 1')",
                (user,),
            )
        fake = self._fake_chat(
            lambda page: json.dumps({"summary": "s", "key_facts": [f"fact from page {page}"]})
        )
        with mock.patch.object(llm, "chat", fake):
            out = linkedin.write_linkedin_post(user, "t1", "launch post", "Q3-plan")
        self.assertEqual(out, CLEAN)
        pages = docs.page_count("Q3-plan")
        self.assertEqual(fake.calls, ["doc_note"] * (pages - 1) + ["linkedin"])

    def test_no_facts_means_no_draft(self):
        fake = self._fake_chat(lambda page: json.dumps({"summary": "", "key_facts": []}))
        with mock.patch.object(llm, "chat", fake):
            out = linkedin.write_linkedin_post("li-b9-empty", "t1", "launch post", "Q3-plan")
        self.assertTrue(out.startswith("error:"), out)
        self.assertNotIn("linkedin", fake.calls)
        with mock.patch.object(llm, "chat", fake), self.assertRaises(ValueError):
            linkedin.write_linkedin_post("li-b9-empty", "t1", "launch post", "no-such-doc")


class FetchDocTest(unittest.TestCase):
    def test_pages_cover_doc_under_cap(self):
        manifest = json.loads((ROOT / "docs" / "_manifest.json").read_text())
        for doc_id in manifest:
            with self.subTest(doc_id):
                text = (ROOT / manifest[doc_id]["path"]).read_text()
                first = docs.fetch_doc("u", "t", doc_id=doc_id)
                n = int(first.split("\n", 1)[0].rstrip("]").split("/")[-1])
                self.assertGreater(n, 1)
                bodies = []
                for page in range(1, n + 1):
                    out = docs.fetch_doc("u", "t", doc_id=doc_id, page=page)
                    self.assertTrue(out.startswith(f"[{doc_id} page {page}/{n}]"))
                    self.assertLessEqual(llm.estimate_tokens([{"content": out}]), 3000)
                    bodies.append(out.split("\n", 1)[1])
                self.assertEqual("\n\n".join(bodies), text)

    def test_launch_date_in_q3_plan(self):
        manifest = json.loads((ROOT / "docs" / "_manifest.json").read_text())
        want = manifest["Q3-plan"]["facts"]["launch_date"]
        text = (ROOT / "docs" / "Q3-plan.md").read_text()
        self.assertEqual(text.count(f"Final launch date: {want}"), 1)
        self.assertGreaterEqual(manifest["Q3-plan"]["approx_tokens"], 20000)

    def test_rejects_traversal_and_unknown(self):
        for bad in [
            "../README",
            "../../etc/passwd",
            "/etc/passwd",
            "_manifest",
            "nope",
            "",
            "docs/Q3-plan",
        ]:
            with self.subTest(bad), self.assertRaises(ValueError):
                docs.fetch_doc("u", "t", doc_id=bad)
        with self.assertRaises(ValueError):
            docs.fetch_doc("u", "t", doc_id="Q3-plan", page=999)

    def test_csv_header_repeated_on_every_page(self):
        """B7: a hard-split CSV keeps its column names on every page, rows stay whole, and
        nothing is lost or duplicated besides the header copies."""
        header = "date,region,units,revenue,cost"
        rows = [
            f"2026-01-{r % 28 + 1:02d},R{r % 7},{r * 37 % 1000},{r * 7919}.13,{r}.50"
            for r in range(600)
        ]
        csv = "Sales export\n\n" + "\n".join([header, *rows])
        pages = docs.paginate(csv, page_chars=3000, page_tokens=900)
        self.assertGreater(len(pages), 3)
        table = [
            p.split("\n\n")[-1].rstrip("\n") for p in pages if "," in p
        ]  # page 1: "Sales export"
        for i, page in enumerate(table, 1):
            with self.subTest(page=i):
                self.assertEqual(page.split("\n", 1)[0], header)
                self.assertLessEqual(len(pages[i - 1]), 3000)
                self.assertLessEqual(docs._tokens(pages[i - 1]), 900)
        body = [line for page in table for line in page.split("\n")[1:]]
        self.assertEqual(body, rows)
        prose = "word " * 3000  # no table: hard split as before, no header added
        self.assertEqual("".join(docs.paginate(prose, page_chars=2000)), prose.rstrip("\n"))

    def test_fetch_doc_paginates_once(self):
        """R4-S1: fetch_doc indexes the pages it already has instead of paginating again."""
        with mock.patch.object(docs, "paginate", wraps=docs.paginate) as spy:
            docs.fetch_doc("u", "t", doc_id="Q3-plan", page=2)
        self.assertEqual(spy.call_count, 1)

    def test_list_docs(self):
        out = docs.list_docs("u", "t")
        self.assertIn("Q3-plan", out)
        self.assertNotIn("_manifest", out)


class SendMessageTest(unittest.TestCase):
    def _rows(self, recipient):
        return (
            db.conn().execute("SELECT * FROM send_log WHERE recipient = ?", (recipient,)).fetchall()
        )

    def test_logs_row(self):
        out = messaging.send_message("u1", "t1", to="alice-test", text="hello there")
        self.assertTrue(out.startswith("ok"), out)
        rows = self._rows("alice-test")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["user_id"], rows[0]["thread_id"], rows[0]["text"]), ("u1", "t1", "hello there")
        )

    def test_blocks_forgotten(self):
        fake = SimpleNamespace(contains_forgotten=lambda text: "secret-addr" in text)
        with mock.patch.dict(sys.modules, {"chatbot.memory.forget": fake}):
            out = messaging.send_message("u1", "t1", to="bob-test", text="my secret-addr is here")
        self.assertTrue(out.startswith("error"))
        self.assertEqual(self._rows("bob-test"), [])

    def test_blocks_forgotten_recipient(self):
        # W-SEC4/W9: real tombstone (no mock), forgotten value used as the recipient.
        from chatbot.memory import facts, forget

        u = "u-recipient-guard"
        fid = facts.upsert_fact(u, None, "global", "partner_handle", "jordan-test-0142")
        resolve = json.dumps({"fact_ids": [fid], "values": [], "search_terms": []})

        def fake_chat(messages, tools=None, purpose="chat", thread_id=None):
            content = resolve if purpose == "forget_resolve" else ""
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))]
            )

        with mock.patch.object(llm, "chat", fake_chat):
            self.assertTrue(forget.forget(u, "my partner's handle").startswith("Forgotten"))
        out = messaging.send_message(u, "t1", to="Jordan-Test 0142", text="hi there")
        self.assertTrue(out.startswith("error"), out)
        self.assertNotIn("0142", out)
        self.assertEqual(self._rows("Jordan-Test 0142"), [])

    def test_requires_fields(self):
        self.assertTrue(messaging.send_message("u1", "t1", to="", text="x").startswith("error"))


class RegistryTest(unittest.TestCase):
    def test_discovers_all(self):
        # Subset, not equality: adding a capability module must not break this test.
        self.assertLessEqual({"docs", "linkedin", "messaging"}, set(capabilities.discover()))

    def test_register_all_and_index(self):
        registered = {}
        names = capabilities.register_all(lambda n, s, f: registered.__setitem__(n, (s, f)))
        self.assertEqual(set(names), set(registered))
        for tool in [
            "fetch_doc",
            "list_docs",
            "send_message",
            "validate_linkedin_post",
            "write_linkedin_post",
            "load_capability",
        ]:
            self.assertIn(tool, registered)
            self.assertEqual(registered[tool][0]["name"], tool)
        index = capabilities.capability_index()
        self.assertLess(len(index) / llm.CHARS_PER_TOKEN, 300)
        self.assertIn("load_capability", index)

    def test_load_capability(self):
        out = capabilities.load_capability("u", "t", name="linkedin")
        self.assertIn("1300", out)
        self.assertTrue(capabilities.load_capability("u", "t", name="nope").startswith("error"))


if __name__ == "__main__":
    unittest.main()
