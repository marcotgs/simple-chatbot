"""F1b: summarize_doc notes uncached pages concurrently (DOC_NOTE_WORKERS), caps each doc_note
completion (DOC_NOTE_MAX_TOKENS) and stores notes under the canonical doc id (R5-SEC-W2).
Offline: llm._client is a fake whose calls sleep, so the real llm.chat (budget check, llm_calls
row, calls.jsonl line) runs from the worker threads.
Run: python3 -m unittest tests.test_doc_notes_parallel -v"""

import json
import os
import re
import sys
import tempfile
import threading
import time
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_integration_flows as flows  # noqa: E402  (module only: its TestCases are not re-run)

from chatbot import config, db, llm  # noqa: E402
from chatbot.memory import doc_notes, forget  # noqa: E402

SLEEP = 0.05
PAGES = 12


def uid(tag: str) -> str:
    return f"f1b-{tag}-{uuid.uuid4().hex[:8]}"


class SlowNoteClient:
    """openai-client stand-in for doc_note calls: sleeps SLEEP, answers with notes naming the
    page, raises for pages in `fail`."""

    def __init__(self, fail=()):
        self.fail, self.requests, self._lock = set(fail), [], threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        page = int(re.search(r"PAGE (\d+):", kw["messages"][1]["content"]).group(1))
        with self._lock:
            self.requests.append({"page": page, "max_tokens": kw.get("max_tokens")})
        time.sleep(SLEEP)
        if page in self.fail:
            raise RuntimeError(f"provider error on page {page}")
        content = json.dumps(
            {"summary": f"Summary of page {page}", "key_facts": [f"Page {page} total: {page * 7}"]}
        )
        msg = SimpleNamespace(content=content, tool_calls=None)
        usage = SimpleNamespace(prompt_tokens=50, completion_tokens=20)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    def pages(self):
        return sorted(r["page"] for r in self.requests)


class ParallelSummarizeTest(unittest.TestCase):
    def setUp(self):
        self._saved = llm._client
        self.addCleanup(setattr, llm, "_client", self._saved)

    def run_summary(self, workers, fail=(), doc="Report", n=PAGES):
        client = SlowNoteClient(fail)
        llm._client = client
        u = uid("p")
        with mock.patch.object(config, "DOC_NOTE_WORKERS", workers):
            start = time.perf_counter()
            try:
                out = doc_notes.summarize_doc(
                    u, "t", doc, read_page=lambda d, p: f"page {p} text", page_count=lambda d: n
                )
            except RuntimeError as e:
                out = e
            wall = time.perf_counter() - start
        return u, client, out, wall

    def test_parallel_is_faster_and_matches_sequential_output(self):
        _, seq_client, seq_out, seq_wall = self.run_summary(1)
        _, par_client, par_out, par_wall = self.run_summary(4)
        self.assertLess(par_wall, 0.6 * seq_wall, (par_wall, seq_wall))
        self.assertEqual(par_client.pages(), list(range(1, PAGES + 1)))  # every page, once
        self.assertEqual(par_out, seq_out)  # same text, page order
        found = [int(p) for p in re.findall(r"\[p(\d+)\] Summary", par_out)]
        self.assertEqual(found, list(range(1, PAGES + 1)))

    def test_workers_one_is_sequential(self):
        _, client, out, wall = self.run_summary(1, n=4)
        self.assertEqual([r["page"] for r in client.requests], [1, 2, 3, 4])  # strict page order
        self.assertGreaterEqual(wall, 4 * SLEEP)

    def test_failing_page_raises_after_other_pages_saved(self):
        u, client, out, _ = self.run_summary(4, fail={5})
        self.assertIsInstance(out, RuntimeError)
        self.assertEqual(str(out), "provider error on page 5")  # same error as the old loop
        self.assertEqual(doc_notes.noted_pages(u, "Report"), set(range(1, PAGES + 1)) - {5})
        # A retry only notes the failed page (the rest is cached).
        retry = SlowNoteClient()
        llm._client = retry
        out = doc_notes.summarize_doc(
            u, "t", "Report", read_page=lambda d, p: f"page {p} text", page_count=lambda d: PAGES
        )
        self.assertEqual(retry.pages(), [5])
        self.assertIn("[p5] Summary of page 5", out)

    def test_cached_pages_are_skipped(self):
        u, client, _, _ = self.run_summary(4)
        again = SlowNoteClient()
        llm._client = again
        doc_notes.summarize_doc(
            u, "t", "Report", read_page=lambda d, p: "x", page_count=lambda d: PAGES
        )
        self.assertEqual(again.requests, [])

    def test_completion_is_capped_and_every_call_logged(self):
        before = db.conn().execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        lines_before = config.CALL_LOG_PATH.read_text().count("\n")
        _, client, _, _ = self.run_summary(4)
        self.assertEqual({r["max_tokens"] for r in client.requests}, {config.DOC_NOTE_MAX_TOKENS})
        after = db.conn().execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        self.assertEqual(after - before, PAGES)  # one llm_calls row per threaded call
        new = config.CALL_LOG_PATH.read_text().splitlines()[lines_before:]
        self.assertEqual(len(new), PAGES)
        self.assertTrue(all(json.loads(line)["purpose"] == "doc_note" for line in new))

    def test_md_suffix_is_stripped_on_injected_reader(self):
        u, _, _, _ = self.run_summary(4, doc=" Report.md ", n=2)
        self.assertEqual(doc_notes.noted_docs(u), ["Report"])


class MdSuffixForgetTest(flows.FlowCase):
    """R5-SEC-W2: summarize_doc with a '.md' doc id must store notes under path.stem, so forget's
    rename of the upload also renames the notes and nothing keeps the value."""

    def test_md_suffix_notes_are_forgotten(self):
        u, t = uid("md"), "home"
        street = "Quill" + "".join(chr(97 + b % 26) for b in uuid.uuid4().bytes[:6])
        addr = f"42 {street} Road"
        self.fake.extract_rules = [
            ("I live at", [{"key": "home_address", "value": addr, "scope": "global"}])
        ]
        self.send(u, t, f"I live at {addr}.")
        doc = self.upload(u, f"Lease {addr}.txt", b"Lease A terms.")
        doc_notes.summarize_doc(u, t, doc + ".md")
        self.assertEqual(doc_notes.noted_docs(u), [doc])
        self.send(
            u, t, "Forget my home address.", [("forget", {"subject": "my home address"})], "Done."
        )
        self.assertEqual(forget.verify_clean(addr), [])


if __name__ == "__main__":
    unittest.main()
