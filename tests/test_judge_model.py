"""JUDGE_MODEL routing: structured JSON decision calls (extract, forget resolver) may use their own
model; chat and summaries stay on LLM_MODEL. Offline, fake client. Run: python3 -m unittest
discover tests"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chatbot import config, core, llm  # noqa: E402
from chatbot.memory import extract, forget, summary  # noqa: E402

JUDGE = "test/judge-model"  # synthetic id


class FakeClient:
    """Records the model sent per call; every reply is valid JSON so no repair call happens."""

    def __init__(self, error=None):
        self.models = []
        self.error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.models.append(kw["model"])
        if self.error:
            raise self.error
        msg = SimpleNamespace(content='{"facts": [], "values": []}', tool_calls=None)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def logged_rows(since: int) -> list[dict]:
    return [json.loads(line) for line in config.CALL_LOG_PATH.read_text().splitlines()[since:]]


def by_purpose(rows: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["purpose"], set()).add(r["model"])
    return out


def log_len() -> int:
    p = config.CALL_LOG_PATH
    return len(p.read_text().splitlines()) if p.exists() else 0


class JudgeModelTest(unittest.TestCase):
    def setUp(self):
        self._client, llm._client = llm._client, FakeClient()

    def tearDown(self):
        llm._client = self._client

    def _run_all(self):
        """One call of each kind: extract, forget resolver + finder, summary, chat."""
        u, t = f"u-{uuid.uuid4().hex[:8]}", "t1"
        n, start = 0, log_len()
        for i in range(core.RECENT_TURNS + 2):
            n = core.ingest_turn(u, t, "user", f"My cat is called Mochi, note {i}")
            core.ingest_turn(u, t, "assistant", "ok")
        extract.maybe_extract(u, t, n)
        forget.forget(u, "my cat Mochi", t)
        summary.maybe_roll_summary(u, t, force=True)
        llm.chat([{"role": "user", "content": "hi"}], purpose="chat", thread_id=t)
        return logged_rows(start)

    def test_judge_model_set_routes_only_judge_calls(self):
        with mock.patch.object(config, "JUDGE_MODEL", JUDGE):
            rows = self._run_all()
        models = by_purpose(rows)
        for purpose in ("extract", "forget_resolve", "forget_find"):
            self.assertEqual(models.get(purpose), {JUDGE}, purpose)
        for purpose in ("summary", "chat"):
            self.assertEqual(models.get(purpose), {config.LLM_MODEL}, purpose)
        # Each log row records the model actually sent on that call.
        self.assertEqual([r["model"] for r in rows], llm._client.models)

    def test_judge_model_unset_uses_llm_model_everywhere(self):
        with mock.patch.object(config, "JUDGE_MODEL", config.LLM_MODEL):
            models = by_purpose(self._run_all())
        self.assertIn("extract", models)
        self.assertEqual(set().union(*models.values()), {config.LLM_MODEL})
        self.assertEqual(set(llm._client.models), {config.LLM_MODEL})

    def test_config_default_and_override(self):
        # Fresh interpreter: reloading config here would rebind DB_PATH for the rest of the suite.
        def judge_model(**env):
            out = subprocess.run(
                [sys.executable, "-c", "from chatbot import config; print(config.JUDGE_MODEL)"],
                env={**os.environ, "LLM_MODEL": "x/main", **env},
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            return out.stdout.strip()

        self.assertEqual(judge_model(JUDGE_MODEL=""), "x/main")  # "" also beats a .env value
        self.assertEqual(judge_model(JUDGE_MODEL=JUDGE), JUDGE)

    def test_provider_error_names_model_and_propagates(self):
        llm._client = FakeClient(error=RuntimeError("400 response_format not supported"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(RuntimeError):
            llm.chat([{"role": "user", "content": "x"}], purpose="extract", model=JUDGE)
        self.assertIn(JUDGE, err.getvalue())
        self.assertIn("extract", err.getvalue())


if __name__ == "__main__":
    unittest.main()
