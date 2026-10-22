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
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("CALL_LOG_PATH", os.path.join(_tmp, "calls.jsonl"))
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatbot import capabilities, db, llm  # noqa: E402
from chatbot.capabilities import docs, linkedin, messaging  # noqa: E402

CLEAN = "We shipped the new onboarding flow this week. Thanks to the whole team.\n\n#product #launch"


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
                self.assertEqual(linkedin.check_linkedin_post(text) == [], ok, linkedin.check_linkedin_post(text))

    def test_validate_tool(self):
        self.assertEqual(linkedin.validate_linkedin_post("u", "t", text=CLEAN), "valid")
        self.assertTrue(linkedin.validate_linkedin_post("u", "t", text="x" * 1301).startswith("invalid"))


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=self.replies.pop(0), tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10))


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
                    self.assertLessEqual(len(out) / llm.CHARS_PER_TOKEN, 3500)
                    bodies.append(out.split("\n", 1)[1])
                self.assertEqual("\n\n".join(bodies), text)

    def test_launch_date_in_q3_plan(self):
        manifest = json.loads((ROOT / "docs" / "_manifest.json").read_text())
        want = manifest["Q3-plan"]["facts"]["launch_date"]
        text = (ROOT / "docs" / "Q3-plan.md").read_text()
        self.assertEqual(text.count(f"Final launch date: {want}"), 1)
        self.assertGreaterEqual(manifest["Q3-plan"]["approx_tokens"], 20000)

    def test_rejects_traversal_and_unknown(self):
        for bad in ["../README", "../../etc/passwd", "/etc/passwd", "_manifest", "nope", "", "docs/Q3-plan"]:
            with self.subTest(bad), self.assertRaises(ValueError):
                docs.fetch_doc("u", "t", doc_id=bad)
        with self.assertRaises(ValueError):
            docs.fetch_doc("u", "t", doc_id="Q3-plan", page=999)

    def test_list_docs(self):
        out = docs.list_docs("u", "t")
        self.assertIn("Q3-plan", out)
        self.assertNotIn("_manifest", out)


class SendMessageTest(unittest.TestCase):
    def _rows(self, recipient):
        return db.conn().execute("SELECT * FROM send_log WHERE recipient = ?", (recipient,)).fetchall()

    def test_logs_row(self):
        out = messaging.send_message("u1", "t1", to="alice-test", text="hello there")
        self.assertTrue(out.startswith("ok"), out)
        rows = self._rows("alice-test")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["user_id"], rows[0]["thread_id"], rows[0]["text"]), ("u1", "t1", "hello there"))

    def test_blocks_forgotten(self):
        fake = SimpleNamespace(contains_forgotten=lambda text: "secret-addr" in text)
        with mock.patch.dict(sys.modules, {"chatbot.memory.forget": fake}):
            out = messaging.send_message("u1", "t1", to="bob-test", text="my secret-addr is here")
        self.assertTrue(out.startswith("error"))
        self.assertEqual(self._rows("bob-test"), [])

    def test_requires_fields(self):
        self.assertTrue(messaging.send_message("u1", "t1", to="", text="x").startswith("error"))


class RegistryTest(unittest.TestCase):
    def test_discovers_all(self):
        self.assertEqual(set(capabilities.discover()), {"docs", "linkedin", "messaging"})

    def test_register_all_and_index(self):
        registered = {}
        names = capabilities.register_all(lambda n, s, f: registered.__setitem__(n, (s, f)))
        self.assertEqual(set(names), set(registered))
        for tool in ["fetch_doc", "list_docs", "send_message", "validate_linkedin_post",
                     "write_linkedin_post", "load_capability"]:
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
