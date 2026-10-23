"""Forget guard accepts a subject that widens the user's words a little (B2) and search_doc_notes
resolves loosely written doc ids (B4). Offline.
Run: python3 -m unittest tests.test_forget_docs_fixes"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("CALL_LOG_PATH", os.path.join(_tmp, "calls.jsonl"))
os.environ["GATE_MODEL"] = ""  # offline: the salience gate (on by default) never hits the network
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-a-key")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatbot import core, db, memory  # noqa: E402
from chatbot.memory import doc_notes, facts, forget, tools  # noqa: E402


def uid(tag: str) -> str:
    return f"t10-{tag}-{uuid.uuid4().hex[:8]}"


def store_facts(u: str, t: str, **values: str) -> None:
    for key, value in values.items():
        facts.upsert_fact(u, t, "global", key, value)


class ForgetGuardTest(unittest.TestCase):
    def run_forget(self, typed: str, subject: str) -> str:
        u, t = uid("forget"), uid("thread")
        store_facts(
            u, t, home_address="1 Synthetic Way", phone_number="555-0100", partner_name="Sam Doe"
        )
        with mock.patch.object(memory, "after_turn"):
            core.ingest_turn(u, t, "user", typed)
        with mock.patch.object(forget, "forget", return_value="ran") as run:
            out = tools._forget(u, t, subject=subject)
        self.assertEqual(run.called, out == "ran")
        return out

    def test_accepts(self):
        for typed, subject in [
            ("Forget my address.", "home address"),
            ("Delete my address please", "home address"),
            ("Delete my address", "home address"),
            ("Please forget my home address.", "home address"),  # contiguous run, as before
            # S2: filler words around the ask do not count as the subject.
            ("Can you forget my address?", "home address"),
            ("Hey, forget my address.", "home address"),
            ("Please forget my address now.", "home address"),
            ("Could you delete my address, thanks", "home address"),
            # The user's own words, even broad ones, are theirs to ask for.
            ("Forget my address and everything else stored", "address and everything else stored"),
        ]:
            with self.subTest(typed=typed, subject=subject):
                self.assertEqual(self.run_forget(typed, subject), "ran")

    def test_refuses(self):
        for typed, subject in [
            ("Forget my address.", "home address work phone number"),  # 3 extra words
            ("Forget my address.", "phone number"),  # unrelated
            ("Forget it.", "home address"),  # no content words typed
            ("Forget my home page address.", "home address"),  # R5-SEC-S1 still refused
            ("Don't forget my address.", "home address"),  # negated ask
            # SEC-C1: model-added words must fit ONE stored key with the typed ones.
            ("Forget my address.", "address phone partner"),
            ("Forget my address.", "address phone number"),
            ("Forget my address.", "home address work"),
            ("Forget my address.", "address and everything else stored"),
            ("Forget my address.", "my address and all other facts"),
            ("Forget my address. My partner is Sam.", "address partner"),  # other sentence
            # SEC-C2: a sentence with a negated forget verb is refused as a whole.
            ("Don't forget my address, just remove the emoji", "address"),
            ("Never forget my phone number, but delete the draft", "phone number"),
            ("I don't want you to forget my address, delete nothing", "address"),
            ("Please don't forget my partner or remove them", "partner"),
            ("Forget my address, but don't delete my phone number", "phone number"),
            # SEC-W4: more negations, and a newline after the negation.
            ("I cannot forget my partner, delete it", "partner"),
            ("cannot forget my partner", "partner"),
            ("can not forget my partner", "partner"),
            ("You shouldn't delete my partner", "partner"),
            ("No need to forget my partner", "partner"),
            ("don't\nforget my partner", "partner"),
            ("do NOT, forget my partner", "partner"),
            # SEC-W5: markup between the negation and the verb does not end it.
            ("do *not* forget my partner", "partner"),
            ("don't *forget* my partner", "partner"),
            ("don't (ever) forget my partner", "partner"),
            ('don\'t "forget" my partner', "partner"),
            ("_don't_ forget my partner", "partner"),
            ("don't~forget my partner", "partner"),
            # W2: the documented fail-closed examples.
            ("No, forget my address", "address"),
            ("Don't keep it, forget my address", "address"),
        ]:
            with self.subTest(typed=typed, subject=subject):
                self.assertTrue(self.run_forget(typed, subject).startswith("error: not run"))


class ForgetFallbackTest(unittest.TestCase):
    """SEC-C1: with the resolver model down, the key fallback deletes one key or nothing."""

    def setUp(self):
        self.u, self.t = uid("fallback"), uid("thread")
        tag = uuid.uuid4().hex[:6]  # tombstones are global: values unique per test
        store_facts(
            self.u,
            self.t,
            home_address=f"12 Zzyx{tag} Lane, Testburg",
            phone_number=f"555-01{tag[:2]}-{tag[2:]}",
            partner_name=f"Quorra {tag} Synthetic",
        )
        self.down = mock.patch.object(forget, "ask_json", side_effect=RuntimeError("model down"))
        self.down.start()
        self.addCleanup(self.down.stop)

    def keys(self):
        return {f["key"] for f in facts.get_active_facts(self.u, self.t)}

    def test_guarded_tool_removes_only_home_address(self):
        with mock.patch.object(memory, "after_turn"):
            core.ingest_turn(self.u, self.t, "user", "Forget my address.")
        out = tools._forget(self.u, self.t, subject="address phone partner")
        self.assertTrue(out.startswith("error: not run"), out)
        self.assertEqual(self.keys(), {"home_address", "phone_number", "partner_name"})
        out = tools._forget(self.u, self.t, subject="home address")
        self.assertIn("Forgotten: home_address", out)
        self.assertEqual(self.keys(), {"phone_number", "partner_name"})

    def test_ambiguous_subject_deletes_nothing(self):
        facts.upsert_fact(self.u, self.t, "global", "work_address", "9 Office Synthetic Rd")
        for subject in ("address", "address phone partner", "address and everything else"):
            with self.subTest(subject):
                out = forget.forget(self.u, subject, self.t)
                self.assertTrue(out.startswith("Nothing stored matches"), out)
        self.assertEqual(
            self.keys(), {"home_address", "phone_number", "partner_name", "work_address"}
        )


class DocIdResolveTest(unittest.TestCase):
    def setUp(self):
        self.u, self.t = uid("docs"), uid("thread")
        c = db.conn()
        with c:
            c.executemany(
                "INSERT INTO doc_notes (user_id, thread_id, doc_id, page, kind, content)"
                " VALUES (?, ?, ?, 1, 'key_fact', ?)",
                [
                    (self.u, self.t, "Q3-plan", "Launch date: 2031-04-05"),
                    (self.u, self.t, "budget", "Launch budget: 900 units"),
                    (self.u, uid("other"), "Q3-plan", "Launch owner: Other Thread Person"),
                ],
            )

    def search(self, doc_id):
        return doc_notes.search_doc_notes(self.u, self.t, "launch", doc_id)

    def test_variants_resolve_to_noted_id(self):
        for doc_id in ["Q3-plan", "Q3-plan.pdf", "Q3 plan", "q3_plan.md", "upload-q3-plan.MD"]:
            with self.subTest(doc_id=doc_id):
                out = self.search(doc_id)
                self.assertIn("2031-04-05", out)
                self.assertNotIn("900 units", out)

    def test_unknown_id_searches_whole_thread(self):
        out = self.search("no-such-doc")
        self.assertIn("2031-04-05", out)
        self.assertIn("900 units", out)

    def test_other_thread_invisible(self):
        for doc_id in [None, "Q3-plan", "no-such-doc"]:
            with self.subTest(doc_id=doc_id):
                self.assertNotIn("Other Thread Person", self.search(doc_id))


if __name__ == "__main__":
    unittest.main()
