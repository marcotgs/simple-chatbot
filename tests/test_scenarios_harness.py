"""Offline checks for the scenario harness (no network, no LLM).
Run: python3 -m unittest tests.test_scenarios_harness -v"""

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = tempfile.mkdtemp()
if (
    "chatbot.config" not in sys.modules
):  # another test module may already have pointed chatbot at its temp DB
    os.environ.update(
        {
            "DB_PATH": os.path.join(_tmp, "h.db"),
            "CALL_LOG_PATH": os.path.join(_tmp, "calls.jsonl"),
            "GATE_MODEL": "",  # offline: the salience gate (on by default) never hits the network
            "OPENROUTER_API_KEY": "test-not-a-key",
        }
    )

from scenarios import checks, run  # noqa: E402
from scenarios.linkedin_rules import check_post, extract_post  # noqa: E402
from scripts import gen_history as gh  # noqa: E402

FILLER_FORBIDDEN = [
    "mochi",
    "trattoria",
    "verde",
    "evergreen",
    "742",
    "springfield",
    "99999",
    "priya",
    "raman",
    "manager",
    "british",
    "american",
    "spelling",
    "playful",
    "launch",
    "q3",
    "cat",
    "partner",
    "jordan",
    "address",
    "january",
    "february",
    "march",
    "april",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


class GenHistoryTest(unittest.TestCase):
    def test_same_seed_same_history_and_other_seed_differs(self):
        self.assertEqual(gh.generate(), gh.generate())
        self.assertNotEqual(gh.generate(seed=1), gh.generate(seed=2))

    def test_has_500_numbered_turns(self):
        turns = gh.generate()
        self.assertEqual([t["turn"] for t in turns], list(range(1, 501)))

    def test_planted_details_at_expected_turns(self):
        turns, exp = gh.generate(), gh.expected()
        at = lambda k: turns[exp["turns"][k] - 1]["user"]  # noqa: E731
        self.assertIn(exp["address"], at("address"))
        self.assertTrue(5 <= exp["turns"]["address"] <= 15)
        self.assertIn(exp["partner"], at("partner"))
        self.assertIn(exp["cat_name"], at("cat"))
        self.assertTrue(18 <= exp["turns"]["cat"] <= 22)
        self.assertIn(exp["restaurant"], at("restaurant"))

    def test_address_only_in_its_user_turn_never_in_assistant(self):
        turns, exp = gh.generate(), gh.expected()
        for t in turns:
            self.assertEqual(
                checks.text_leaks(t["assistant"], [exp["address"], *exp["address_variants"]]), [], t
            )
        self.assertEqual(
            [t["turn"] for t in turns if "evergreen" in t["user"].lower()],
            [exp["turns"]["address"]],
        )

    def test_filler_carries_no_scenario_facts(self):
        for t in gh.generate(500, seed=99, plants=False):
            text = f"{t['user']} {t['assistant']}".lower()
            hits = [w for w in FILLER_FORBIDDEN if re.search(rf"\b{w}\b", text)]
            self.assertEqual(hits, [], t)

    def test_write_produces_history_and_expected_files(self):
        h, e = Path(_tmp) / "h.json", Path(_tmp) / "e.json"
        gh.write(h, e)
        import json

        self.assertEqual(json.loads(h.read_text())["turns"], gh.generate())
        self.assertEqual(json.loads(e.read_text())["cat_name"], "Mochi")

    def test_load_history_ingests_every_message_in_order(self):
        from chatbot import db, llm

        llm._client = SimpleNamespace(
            chat=SimpleNamespace(
                send=lambda **kw: SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
                    ],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1),
                )
            )
        )
        turns = gh.generate(30)
        self.assertEqual(gh.load_history("harness-u", "harness-t", turns=turns), 60)
        rows = (
            db.conn()
            .execute(
                "SELECT role, content FROM messages WHERE user_id = 'harness-u'"
                " AND thread_id = 'harness-t' ORDER BY id"
            )
            .fetchall()
        )
        expected = [(r, t[r]) for t in turns for r in ("user", "assistant")]
        self.assertEqual([(r["role"], r["content"]) for r in rows], expected)

    def test_load_history_uses_the_live_ingest_call(self):
        """D-2: the loader calls ingest_turn exactly like live chat (no live=/batch flag)."""
        from chatbot import core

        seen = []

        def ingest(
            user_id, thread_id, role, content
        ):  # TypeError if the loader passes any extra flag
            seen.append(role)

        with mock.patch.object(core, "ingest_turn", ingest):
            self.assertEqual(gh.load_history("u", "t", turns=gh.generate(3)), 6)
        self.assertEqual(seen, ["user", "assistant"] * 3)


class DateTest(unittest.TestCase):
    def test_formats_normalise_to_same_date(self):
        want = date(2026, 9, 17)
        for s in [
            "2026-09-17",
            "September 17, 2026",
            "Sep 17 2026",
            "17 Sep 2026",
            "17th of September, 2026",
            "Sept. 17th, 2026",
            "9/17/2026",
            "17/09/2026",
            "The launch is on Thursday, September 17, 2026.",
        ]:
            self.assertTrue(checks.mentions_date(s, want), s)

    def test_yearless_mention_counts(self):
        self.assertTrue(checks.mentions_date("Launch is September 17.", date(2026, 9, 17)))

    def test_distractor_dates_do_not_count(self):
        want = date(2026, 9, 17)
        for s in [
            "2026-09-03",
            "September 10, 2026",
            "the code freeze on 2026-09-01",
            "Sep 1",
            "September 17, 2025",
            "17 Sep 2027",
            "no date here",
        ]:
            self.assertFalse(checks.mentions_date(s, want), s)

    def test_parse_date_rejects_text_without_date(self):
        with self.assertRaises(ValueError):
            checks.parse_date("soon")


class SpellingTest(unittest.TestCase):
    def test_counts(self):
        cases = [
            ("The colour of the centre, organised by our favourite neighbours.", 5, 0),
            ("The color of the center, organized by our favorite neighbors.", 0, 5),
            ("We analysed the gray catalogue and realized it.", 2, 2),
            (
                "A realistic laboratory analysis by a specialist.",
                0,
                0,
            ),  # look-alikes are not variants
            ("", 0, 0),
        ]
        for text, brit, amer in cases:
            self.assertEqual(checks.spelling_counts(text)[:2], (brit, amer), text)


class PlayfulTest(unittest.TestCase):
    def test_signals(self):
        self.assertEqual(
            checks.playful_signals(
                "The redesign is on track. QA starts this week. Launch of the new nav follows "
                "review."
            ),
            [],
        )
        self.assertEqual(checks.playful_signals("Third-party fonts are in. Done!"), [])
        for s in [
            "Woohoo, the redesign is live",
            "Big progress 🎉",
            "Done! Shipped! Yes!",
            "Buckle up, team",
        ]:
            self.assertNotEqual(checks.playful_signals(s), [], s)


class LinkedInRulesTest(unittest.TestCase):
    def test_check_post(self):
        ok_post = (
            "We launched our new billing platform today.\n\n"
            "Thanks to the whole team.\n\n#Launch #Fintech #Q3"
        )
        cases = [
            (ok_post, []),
            ("Plain post, no tags.", []),
            ("Tags with commas.\n#One, #Two, #Three", []),
            ("", ["empty"]),
            ("x" * 1301, ["too long"]),
            ("x" * 1300, []),
            ("Hello\n#a #b #c #d", ["too many hashtags"]),
            ("We #launched today.\n#Q3", ["not at the end"]),
            ("#Launch first, then text.", ["not at the end"]),
            ("Read more at https://example.test/x", ["link"]),
            ("Visit www.example.test", ["link"]),
            ("See acme.com for details", ["link"]),
            ("[docs](/q3)", ["link"]),
            ("Built with C# and Node.js, e.g. fast.", []),
        ]
        for text, want in cases:
            got = check_post(text)
            self.assertEqual(len(got), len(want), (text[:40], got))
            for w, g in zip(want, got, strict=True):
                self.assertIn(w, g)

    def test_extract_post_strips_preamble_and_closer(self):
        reply = (
            "Here's your LinkedIn post:\n\nWe shipped it.\n\n#Launch\n\n"
            "Let me know if you want changes!"
        )
        self.assertEqual(extract_post(reply), "We shipped it.\n\n#Launch")

    def test_extract_post_prefers_fenced_block(self):
        self.assertEqual(
            extract_post("Draft:\n```\nPost body #A\n```\nNotes: see acme.com"), "Post body #A"
        )

    def test_extract_post_drops_the_draft_link_line_core_appends(self):
        """F2: core.send appends the LinkedIn draft line (a URL, or the too-long note) after the
        post; S7 judges the post only, so the no-link rule is not tripped by it."""
        from chatbot.capabilities import linkedin

        post = "We shipped it.\n\n#Launch"
        for line in (linkedin.draft_line(post), linkedin.draft_line("x" * 3000)):
            with self.subTest(line=line[:30]):
                reply = f"Here's your post:\n\n{post}\n\nLet me know if you want changes!"
                got = extract_post(f"{reply}\n\n{line}")
                self.assertEqual(got, post)
                self.assertEqual(check_post(got), [])

    def test_extract_post_keeps_trailing_chatter_that_is_not_a_closer(self):
        self.assertNotEqual(check_post(extract_post("Post body\n#A\n\nP.S. more text")), [])


class LeakScanTest(unittest.TestCase):
    NEEDLES = ["742 Evergreen Terrace", "Evergreen Terrace"]

    def _db(self):
        p = Path(tempfile.mkdtemp()) / "t.db"
        c = sqlite3.connect(p)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE m (id INTEGER PRIMARY KEY, content TEXT);
            CREATE VIRTUAL TABLE m_fts USING fts5 (content, content='m', content_rowid='id');
            INSERT INTO m VALUES (1, 'I live at 742  EVERGREEN terrace, ok');
            INSERT INTO m_fts (rowid, content) VALUES (1, 'I live at 742  EVERGREEN terrace, ok');
            INSERT INTO m VALUES (2, 'nothing here');
        """)
        c.commit()
        return p, c

    def test_scan_db_finds_value_in_rows_and_fts_vocab(self):
        p, c = self._db()
        hits = checks.scan_db(p, self.NEEDLES)
        self.assertIn("m.content[1]:742 evergreen terrace", hits)
        self.assertIn("m_fts(fts-vocab):evergreen", hits)
        c.close()

    def test_scan_db_clean_after_row_and_fts_purge(self):
        p, c = self._db()
        c.execute(
            "INSERT INTO m_fts (m_fts, rowid, content) VALUES ('delete', 1, 'I live at 742  "
            "EVERGREEN terrace, ok')"
        )
        c.execute("DELETE FROM m WHERE id = 1")
        c.commit()
        self.assertEqual(checks.scan_db(p, self.NEEDLES), [])
        c.close()

    def test_scan_db_catches_fts_left_behind_when_only_row_deleted(self):
        p, c = self._db()
        c.execute("DELETE FROM m WHERE id = 1")
        c.commit()
        self.assertEqual(
            checks.scan_db(p, self.NEEDLES),
            ["m_fts(fts-vocab):evergreen", "m_fts(fts-vocab):terrace"],
        )
        c.close()

    def test_scan_files_raw_bytes_case_insensitive(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.db-wal").write_bytes(b"\x00\x01junk 742 EVERGREEN TERRACE junk\xff")
        (d / "b.db").write_bytes(b"\x00clean\xff")
        self.assertEqual(
            checks.scan_files([d / "a.db-wal", d / "b.db", d / "missing"], self.NEEDLES[:1]),
            ["a.db-wal:742 Evergreen Terrace"],
        )

    def test_scan_files_sees_deleted_row_still_in_wal(self):
        p, c = self._db()
        c.execute("DELETE FROM m WHERE id = 1")
        c.commit()  # no VACUUM/checkpoint: old page bytes remain on disk
        self.assertNotEqual(checks.scan_files(checks.db_files(p), self.NEEDLES[1:]), [])
        c.close()

    def test_scan_files_finds_non_ascii_value_in_any_case(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.db").write_bytes("x RUE ÉMILE 12 x".encode())
        (d / "b.db").write_bytes("x rue émile 12 x".encode())
        (d / "c.db").write_bytes("x Hauptstraße 5 x".encode())
        self.assertEqual(
            checks.scan_files(
                [d / "a.db", d / "b.db", d / "c.db"], ["Rue Émile 12", "Hauptstraße 5"]
            ),
            ["a.db:Rue Émile 12", "b.db:Rue Émile 12", "c.db:Hauptstraße 5"],
        )

    def test_scan_db_fts_vocab_catches_accent_folded_term(self):
        p = Path(tempfile.mkdtemp()) / "t.db"
        c = sqlite3.connect(p)
        c.executescript("""CREATE TABLE m (id INTEGER PRIMARY KEY, content TEXT);
            CREATE VIRTUAL TABLE m_fts USING fts5 (content, content='m', content_rowid='id');
            INSERT INTO m_fts (rowid, content) VALUES (1, 'I live at Rue Émile 12');""")
        c.commit()
        self.assertIn("m_fts(fts-vocab):emile", checks.scan_db(p, ["Rue Émile 12"]))
        c.close()

    def test_text_leaks_ignores_punctuation(self):
        self.assertEqual(
            checks.text_leaks("at 742, Evergreen-Terrace!", self.NEEDLES[:1]), self.NEEDLES[:1]
        )

    def test_text_leaks_normalises_case_and_whitespace(self):
        self.assertEqual(
            checks.text_leaks("at 742\nevergreen   TERRACE.", self.NEEDLES[:1]), self.NEEDLES[:1]
        )
        self.assertEqual(checks.text_leaks("I've forgotten it.", self.NEEDLES), [])


class DryRunOfflineTest(unittest.TestCase):
    """B6: --dry fakes llm._client only, so its child process must also get the gate off, else
    the salience gate makes real HTTP calls (credit) and the 'extract calls == 500' check fails."""

    def test_dry_child_gets_gate_off_live_child_keeps_it(self):
        envs = []

        def fake_run(cmd, **kw):
            envs.append(kw["env"])
            return SimpleNamespace(stdout="", returncode=0)

        with (
            mock.patch.dict(os.environ, {"GATE_MODEL": "some/gate-model"}),
            mock.patch.object(run.subprocess, "run", fake_run),
        ):
            run.run_one("dry", False, None, fake=True)
            run.run_one(1, False, None, fake=False)
        self.assertEqual(envs[0]["GATE_MODEL"], "")
        self.assertEqual(envs[1]["GATE_MODEL"], "some/gate-model")


class S4CheckTest(unittest.TestCase):
    BRIT = "The colour of the walls, how we organise the storage room and analyse the survey."
    AMER = "The color of the walls, how we organize the storage room and analyze the survey."
    NEUTRAL = "The walls are blue, the storage room gets shelves, and we read the survey."

    def test_pre_switch_draft_must_be_british(self):
        self.assertEqual(run.check_spelling("w", self.BRIT, "british"), [])
        self.assertNotEqual(
            run.check_spelling("w", self.AMER, "british"), []
        )  # zero memory: model default
        self.assertNotEqual(
            run.check_spelling("w", self.NEUTRAL, "british"), []
        )  # nothing to judge

    def test_post_switch_draft_must_be_american(self):
        self.assertEqual(run.check_spelling("w", self.AMER, "american"), [])
        self.assertNotEqual(run.check_spelling("w", self.BRIT, "american"), [])
        self.assertNotEqual(
            run.check_spelling("w", self.AMER + " Our favourite one.", "american"), []
        )

    def test_history_answer_needs_both_preferences(self):
        self.assertEqual(
            run.check_spelling_history("You first asked for British, then switched to American."),
            [],
        )
        self.assertEqual(
            len(run.check_spelling_history("I don't have any earlier spelling instruction.")), 2
        )
        self.assertEqual(len(run.check_spelling_history("You asked for American spelling.")), 1)

    def test_filler_pushes_statement_out_of_recent_window(self):
        from chatbot import config, core, llm

        llm._client = SimpleNamespace(
            chat=SimpleNamespace(
                send=lambda **kw: SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
                    ],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1),
                )
            )
        )
        x = run.Ctx(core, config)
        core.ingest_turn(run.USER, run.DM, "user", "Always use British spelling.")
        core.ingest_turn(run.USER, run.DM, "assistant", "Noted.")
        self.assertNotEqual(run.statements_in_window(x, "spelling"), [])  # positive control
        gh.load_history(
            run.USER,
            run.DM,
            turns=gh.generate(run.window_filler(core.RECENT_TURNS), seed=1, plants=False),
        )
        self.assertEqual(run.statements_in_window(x, "spelling"), [])


class S7CheckTest(unittest.TestCase):
    LAUNCH = date(2026, 9, 17)
    GOOD = (
        "Big news from our team: our Q3 release goes live on September 17, 2026.\n\n"
        "Months of work across Payments, Mobile and Platform come together in one launch. "
        "Thanks to everyone who tested early builds and sent feedback along the way.\n\n#Launch #Q3"
    )

    def test_good_post_passes(self):
        self.assertEqual(
            run.check_linkedin_reply("Here's your LinkedIn post:\n\n" + self.GOOD, self.LAUNCH), []
        )

    def test_error_reply_fails(self):
        # long and carrying the date, so only the error guard can catch it
        reply = (
            "error: could not produce a valid LinkedIn post after 4 attempts. Last draft: "
            + self.GOOD
        )
        self.assertNotEqual(run.check_linkedin_reply(reply, self.LAUNCH), [])

    def test_refusal_and_short_post_fail(self):
        for reply in ("I can't do that.", "We launch on 2026-09-17! #Q3"):
            with self.subTest(reply=reply):
                self.assertNotEqual(run.check_linkedin_reply(reply, self.LAUNCH), [])

    def test_post_without_launch_date_fails(self):
        errors = run.check_linkedin_reply(
            self.GOOD.replace("on September 17, 2026", "soon"), self.LAUNCH
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("launch date", errors[0])

    def test_post_breaking_rules_fails(self):
        self.assertNotEqual(run.check_linkedin_reply(self.GOOD + " #More #Tags", self.LAUNCH), [])


class S3CheckTest(unittest.TestCase):
    LAUNCH = date(2026, 9, 17)

    def test_distractors_are_every_other_date_in_the_doc(self):
        d = run.q3_distractors(run.q3_launch())
        self.assertEqual(run.q3_launch(), self.LAUNCH)
        self.assertNotIn(self.LAUNCH, d)
        self.assertTrue({date(2026, 8, 20), date(2026, 9, 3), date(2026, 9, 24)} <= set(d), d)

    def test_true_date_alone_passes(self):
        d = run.q3_distractors(self.LAUNCH)
        for reply in ("The final launch date is 2026-09-17.", "It launches on September 17."):
            self.assertEqual(run.check_launch_answer(reply, self.LAUNCH, d), [], reply)

    def test_reply_listing_all_dates_fails(self):
        d = run.q3_distractors(self.LAUNCH)
        reply = "Dates in the plan: " + ", ".join(str(x) for x in sorted([*d, self.LAUNCH])) + "."
        errors = run.check_launch_answer(reply, self.LAUNCH, d)
        self.assertEqual(len(errors), 1)
        self.assertIn("distractor", errors[0])

    def test_distractor_as_the_answer_fails(self):
        d = run.q3_distractors(self.LAUNCH)
        for reply in ("The launch date is 2026-09-03.", "Launch is September 3."):
            self.assertEqual(len(run.check_launch_answer(reply, self.LAUNCH, d)), 2, reply)


if __name__ == "__main__":
    unittest.main()
