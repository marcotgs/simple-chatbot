"""Offline budget checks with a fake model client. Run: python -m unittest discover tests"""
import json
import os
import sys
import tempfile
import unittest
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

from chatbot import config, core, db, llm  # noqa: E402


class FakeClient:
    """Mimics openai client: .chat.completions.create(**kw). Reports a configurable prompt_tokens."""

    def __init__(self, prompt_tokens=None, reply="ok", tool_calls=None):
        self.calls = []
        self.prompt_tokens = prompt_tokens
        self.replies = list(tool_calls or []) + [None]
        self.reply = reply
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        est = llm.estimate_tokens(kw["messages"], kw.get("tools"))
        pt = self.prompt_tokens(est) if callable(self.prompt_tokens) else self.prompt_tokens
        tcs = self.replies.pop(0) if self.replies else None
        msg = SimpleNamespace(content=None if tcs else self.reply, tool_calls=tcs)
        usage = None if pt is None else SimpleNamespace(prompt_tokens=pt, completion_tokens=5)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def big(n_chars):
    return "x" * n_chars


class BudgetTest(unittest.TestCase):
    def setUp(self):
        llm._client = FakeClient(prompt_tokens=lambda est: est)

    def test_trim_keeps_estimate_under_target(self):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(200):
            msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": big(2000)})
        trimmed = core.trim_to_budget(msgs)
        self.assertLessEqual(llm.estimate_tokens(trimmed), config.TARGET_INPUT_TOKENS)
        self.assertEqual(trimmed[0]["role"], "system")
        self.assertIs(trimmed[-1], msgs[-1])
        self.assertGreater(len(trimmed), 2)

    def test_trim_never_starts_with_orphan_tool_result(self):
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": big(20000)},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                {"role": "tool", "tool_call_id": "1", "content": big(3000)},
                {"role": "user", "content": "next"}]
        trimmed = core.trim_to_budget(msgs, target=2000)
        self.assertNotEqual(trimmed[1]["role"], "tool")

    def test_preflight_raises_and_does_not_call_model(self):
        with self.assertRaises(llm.BudgetExceeded):
            llm.chat([{"role": "user", "content": big(30000)}])
        self.assertEqual(llm._client.calls, [])

    def test_provider_reported_over_max_raises_and_is_logged(self):
        llm._client = FakeClient(prompt_tokens=9000)
        with self.assertRaises(llm.BudgetExceeded):
            llm.chat([{"role": "user", "content": "hi"}], purpose="probe-over")
        row = db.conn().execute(
            "SELECT prompt_tokens, over_limit FROM llm_calls WHERE purpose = 'probe-over'").fetchone()
        self.assertEqual((row["prompt_tokens"], row["over_limit"]), (9000, 1))

    def test_missing_usage_falls_back_to_estimate_and_flags(self):
        llm._client = FakeClient(prompt_tokens=None)
        llm.chat([{"role": "user", "content": "hello"}], purpose="probe-missing")
        line = [json.loads(x) for x in config.CALL_LOG_PATH.read_text().splitlines()
                if json.loads(x)["purpose"] == "probe-missing"][-1]
        self.assertTrue(line["usage_missing"])
        self.assertEqual(line["prompt_tokens"], line["estimated_tokens"])
        self.assertNotIn("hello", json.dumps(line))  # bodies never logged

    def test_long_thread_send_stays_under_target(self):
        for _ in range(30):
            self.assertEqual(core.send("u", "long-thread", big(3000)), "ok")
        # Every call counts (chat + memory extract/summary); chat calls are the ones carrying tools.
        sent = [llm.estimate_tokens(c["messages"], c.get("tools")) for c in llm._client.calls]
        self.assertEqual(sum(1 for c in llm._client.calls if c.get("tools")), 30)
        self.assertLessEqual(max(sent), config.TARGET_INPUT_TOKENS)

    def test_over_max_after_call_retries_once_tighter(self):
        # First call reported over MAX, the retry (smaller context) comes back fine.
        reports = iter([9000, 100])
        llm._client = FakeClient(prompt_tokens=lambda est: next(reports))
        for _ in range(3):
            core.ingest_turn("u", "retry", "user", big(6000))
            core.ingest_turn("u", "retry", "assistant", big(6000))
        self.assertEqual(core.send("u", "retry", "q"), "ok")
        first, second = (llm.estimate_tokens(c["messages"]) for c in llm._client.calls)
        self.assertLess(second, first)

    def test_tool_loop_runs_registered_tool(self):
        tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="echo", arguments='{"x": "hi"}'))
        llm._client = FakeClient(prompt_tokens=50, reply="done", tool_calls=[[tc]])
        seen = []
        tools = {"echo": {"schema": {"name": "echo", "parameters": {"type": "object"}},
                          "fn": lambda user_id, thread_id, x: seen.append(x) or f"echo:{x}"}}
        out = core.run_tool_loop([{"role": "user", "content": "go"}], "u", "t", tools)
        self.assertEqual((out, seen), ("done", ["hi"]))
        self.assertEqual(llm._client.calls[1]["messages"][-1]["content"], "echo:hi")


if __name__ == "__main__":
    unittest.main()
