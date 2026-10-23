"""Offline budget checks with a fake model client. Run: python -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_tmp = tempfile.mkdtemp()
os.environ.update(
    {
        "DB_PATH": os.path.join(_tmp, "test.db"),
        "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
        "GATE_MODEL": "",  # offline: the salience gate (on by default) never hits the network
        "MAX_INPUT_TOKENS": "8000",
        "TARGET_INPUT_TOKENS": "6500",
        "OPENROUTER_API_KEY": "test-not-a-key",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import config, core, db, llm  # noqa: E402


class FakeClient:
    """Mimics the OpenRouter SDK client: .chat.send(**kw). Reports a configurable
    prompt_tokens."""

    def __init__(self, prompt_tokens=None, reply="ok", tool_calls=None):
        self.calls = []
        self.prompt_tokens = prompt_tokens
        self.replies = list(tool_calls or []) + [None]
        self.reply = reply
        self.chat = SimpleNamespace(send=self._create)

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
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": big(20000)},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": big(3000)},
            {"role": "user", "content": "next"},
        ]
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
        row = (
            db.conn()
            .execute("SELECT prompt_tokens, over_limit FROM llm_calls WHERE purpose = 'probe-over'")
            .fetchone()
        )
        self.assertEqual((row["prompt_tokens"], row["over_limit"]), (9000, 1))

    def test_provider_reported_exactly_max_is_over(self):
        # The limit means input must stay UNDER MAX_INPUT_TOKENS (same rule as the scenario gate).
        llm._client = FakeClient(prompt_tokens=config.MAX_INPUT_TOKENS)
        with self.assertRaises(llm.BudgetExceeded):
            llm.chat([{"role": "user", "content": "hi"}], purpose="probe-equal")
        row = (
            db.conn()
            .execute("SELECT over_limit FROM llm_calls WHERE purpose = 'probe-equal'")
            .fetchone()
        )
        self.assertEqual(row["over_limit"], 1)
        llm._client = FakeClient(prompt_tokens=config.MAX_INPUT_TOKENS - 1)
        llm.chat([{"role": "user", "content": "hi"}], purpose="probe-under")

    def test_target_sized_context_stays_under_max_for_dense_text(self):
        # Numeric text runs down to 1.77 chars/token (measured, test_integration_flows). The
        # largest message the preflight lets through at TARGET must still be under MAX at that
        # density.
        n = 1
        while (
            llm.estimate_tokens([{"role": "user", "content": "7" * (n * 2)}])
            <= config.TARGET_INPUT_TOKENS
        ):
            n *= 2
        lo, hi = n, n * 2  # binary search the largest passing size
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            fits = (
                llm.estimate_tokens([{"role": "user", "content": "7" * mid}])
                <= config.TARGET_INPUT_TOKENS
            )
            lo, hi = (mid, hi) if fits else (lo, mid)
        self.assertLess(lo / 1.77, config.MAX_INPUT_TOKENS)

    def test_missing_usage_falls_back_to_estimate_and_flags(self):
        llm._client = FakeClient(prompt_tokens=None)
        llm.chat([{"role": "user", "content": "hello"}], purpose="probe-missing")
        line = [
            json.loads(x)
            for x in config.CALL_LOG_PATH.read_text().splitlines()
            if json.loads(x)["purpose"] == "probe-missing"
        ][-1]
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
        for _ in range(3):  # history ingest runs its extract calls on the setUp client
            core.ingest_turn("u", "retry", "user", big(6000))
            core.ingest_turn("u", "retry", "assistant", big(6000))
        reports = iter([9000, 100])
        llm._client = FakeClient(prompt_tokens=lambda est: next(reports, 100))
        self.assertEqual(core.send("u", "retry", "q"), "ok")
        first, second = (
            llm.estimate_tokens(c["messages"]) for c in llm._client.calls if c.get("tools")
        )
        self.assertLess(second, first)

    def test_zero_prompt_tokens_counts_as_missing_usage(self):
        # B3: a content_filter answer reports usage.prompt_tokens=0; the over-limit check must use
        # the estimate instead of a fake 0.
        llm._client = FakeClient(prompt_tokens=0)
        llm.chat([{"role": "user", "content": "7" * 9000}], purpose="probe-zero")
        row = (
            db.conn()
            .execute(
                "SELECT estimated_tokens, prompt_tokens, usage_missing FROM llm_calls"
                " WHERE purpose = 'probe-zero'"
            )
            .fetchone()
        )
        self.assertEqual(row["usage_missing"], 1)
        self.assertEqual(row["prompt_tokens"], row["estimated_tokens"])
        self.assertGreater(row["prompt_tokens"], 0)

    def test_oversized_current_message_is_cut_with_a_mark(self):
        # B2: the current user message alone over budget (a pasted table) is cut to fit, visibly.
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "4,2\n" * 10000}]
        out = core.trim_to_budget(msgs)
        self.assertTrue(out[-1]["content"].endswith(core.CUT_MARK))
        self.assertTrue(out[-1]["content"].startswith("4,2\n4,2"))
        self.assertLessEqual(llm.estimate_tokens(out), config.TARGET_INPUT_TOKENS)

    def test_oversized_tool_result_is_shortened_not_the_users_text(self):
        # R3-C1: when the newest tool result is what is over budget, the user's message stays
        # whole (no CUT_MARK blaming it) and the tool result gets TOOL_SHORTENED_MARK.
        user = "Please read this carefully. " * 300
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "4,2\n" * 5000},
        ]
        out = core.trim_to_budget(msgs)
        self.assertEqual(out[1]["content"], user)
        self.assertTrue(out[-1]["content"].endswith(core.TOOL_SHORTENED_MARK))
        self.assertLessEqual(llm.estimate_tokens(out), config.TARGET_INPUT_TOKENS)

    def test_over_max_retry_that_cannot_shrink_is_not_resent(self):
        # B2: when trimming cannot make the payload smaller, the identical request is not sent
        # twice; the turn fails cleanly.
        llm._client = FakeClient(prompt_tokens=9000)
        with self.assertRaises(llm.BudgetExceeded):
            core.send("u", "no-resend", "q")
        self.assertEqual(sum(1 for c in llm._client.calls if c.get("tools")), 1)

    def test_tool_loop_runs_registered_tool(self):
        tc = SimpleNamespace(
            id="c1", function=SimpleNamespace(name="echo", arguments='{"x": "hi"}')
        )
        llm._client = FakeClient(prompt_tokens=50, reply="done", tool_calls=[[tc]])
        seen = []
        tools = {
            "echo": {
                "schema": {"name": "echo", "parameters": {"type": "object"}},
                "fn": lambda user_id, thread_id, x: seen.append(x) or f"echo:{x}",
            }
        }
        out = core.run_tool_loop([{"role": "user", "content": "go"}], "u", "t", tools)
        self.assertEqual((out, seen), ("done", ["hi"]))
        self.assertEqual(llm._client.calls[1]["messages"][-1]["content"], "echo:hi")


if __name__ == "__main__":
    unittest.main()
