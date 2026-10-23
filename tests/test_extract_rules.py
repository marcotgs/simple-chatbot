"""B1/B6: the extractor prompt states its update and new-key rules as general principles.
Behaviour is checked live (tests.test_extraction_quality HELD_OUT rows); offline this pins the
rules and keeps scenario wording out of the prompt.
Run: python3 -m unittest tests.test_extract_rules -v"""

import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("CALL_LOG_PATH", os.path.join(_tmp, "calls.jsonl"))
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot.memory import extract, facts  # noqa: E402


def _field(name: str) -> str:
    return re.search(rf"^- {name}: (.*)$", extract._PROMPT, re.M).group(1)


class ExtractRulesTest(unittest.TestCase):
    def test_own_fact_on_known_key_is_a_correction_without_change_words(self):
        rule = _field("correction")
        self.assertIn("user's own", rule)
        self.assertIn("known keys", rule)
        self.assertIn("even when the old value is not shown", rule)
        self.assertIn("anything about another person", rule)  # BUG-L2 stays guarded

    def test_known_keys_do_not_limit_new_topics(self):
        self.assertIn("not what may be stored", _field("key"))
        self.assertIn("gets its own new key", _field("key"))

    def test_changes_rule_names_no_trigger_words(self):
        rule = re.search(r"^4\. CHANGES\. (.*)$", extract._PROMPT, re.M).group(1)
        self.assertIn("the newest statement wins.", rule)
        self.assertNotIn('"actually"', rule)

    def test_prompt_has_no_scenario_wording(self):
        text = extract._PROMPT.casefold()
        for word in (
            "boss",
            "manager",
            "priya",
            "wilhelmina",
            "mochi",
            "restaurant",
            "address",
            " cat ",
        ):
            self.assertNotIn(word, text)


class KnownFactsTest(unittest.TestCase):
    """The known-keys line shows each key's current value, so a related topic gets its own key."""

    def test_keys_show_short_values_newest_first_and_stay_bounded(self):
        u = f"kf-{uuid.uuid4().hex[:8]}"
        for i in range(120):
            facts.upsert_fact(u, None, "global", f"topic_{i}", f"value number {i} " + "y" * 80)
        facts.upsert_fact(u, None, "global", "home_city", "Leeds")
        known = extract._known_facts(u, "t", "[turn 1] I grew up somewhere else")
        line = known.splitlines()[-1]
        self.assertIn("home_city (Leeds); topic_119 (value number 119 yyy", line)
        self.assertLess(line.index("topic_119"), line.index("topic_118"))  # newest first
        self.assertNotIn("y" * (extract.KEY_VALUE_CHARS + 1), line)  # values are cut
        self.assertIn("; topic_70; ", line)  # past the value cap keys go bare, still listed
        self.assertLessEqual(len(known), extract.KNOWN_FACTS_CHARS)

    def test_forgotten_values_are_redacted_before_the_cut(self):
        u = f"kf-{uuid.uuid4().hex[:8]}"
        secret = "Zebulon Quarry-Vantwest"
        facts.upsert_fact(u, None, "global", "contact", "x" * 30 + " " + secret)
        with mock.patch.object(
            extract, "redact_forgotten", lambda t: t.replace(secret, "[forgotten]")
        ):
            known = extract._known_facts(u, "t", "[turn 1] contact zebulon")
        self.assertNotIn("Zebulon", known)
        self.assertNotIn("Quarry", known)
        self.assertIn("[forgotten]", known)


if __name__ == "__main__":
    unittest.main()
