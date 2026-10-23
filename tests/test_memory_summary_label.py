"""B-S4: the stored chat summary is labelled as history that the facts above it override.
Run: python3 -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update(
    {
        "DB_PATH": os.path.join(_tmp, "test.db"),
        "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
        "GATE_MODEL": "",  # offline: the salience gate (on by default) never hits the network
        "OPENROUTER_API_KEY": "test-not-a-key",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import db, memory  # noqa: E402
from chatbot.memory import facts, summary  # noqa: E402


class SummaryLabelTest(unittest.TestCase):
    def test_summary_follows_facts_as_history(self):
        u, t = f"u-{uuid.uuid4().hex[:8]}", "t1"
        facts.upsert_fact(u, None, "global", "reply_style", "short answers")
        with db.conn() as c:
            c.execute(
                "INSERT INTO summaries (user_id, thread_id, upto_turn, content)"
                " VALUES (?, ?, ?, ?)",
                (u, t, 30, "User asked for long answers (turn 1), changed to short (turn 17)."),
            )
        text = memory.memory_text(u, t)
        fact_at = text.index("- reply_style: short answers")
        head_at = text.index("Summary of earlier turns of this chat, up to turn 30 (history, not")
        self.assertLess(text.index(memory._FILL), fact_at)
        self.assertLess(fact_at, head_at)
        self.assertIn("the facts win", text[head_at:])
        self.assertLess(head_at, text.index("changed to short (turn 17)"))
        self.assertIn("every reply and draft", memory._FILL)

    def test_prompt_keeps_preferences_out_of_the_summary(self):
        self.assertIn("do not restate them as current", summary._PROMPT)
        self.assertIn("changed from X to Y", summary._PROMPT)


if __name__ == "__main__":
    unittest.main()
