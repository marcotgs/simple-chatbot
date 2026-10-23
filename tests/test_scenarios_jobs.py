"""Offline checks for the scenario scheduler (--jobs), with fake children: no subprocess, no LLM.
Run: python3 -m unittest tests.test_scenarios_jobs -v"""

import io
import os
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GATE_MODEL", "")  # offline convention; the runner never imports chatbot here

from scenarios import run  # noqa: E402


class FakeChildren:
    """Stands in for run.run_one: records start/end times, the snapshot is the slowest child."""

    def __init__(self, fail=()):
        self.fail, self.times, self.snaps, self.tags = set(fail), {}, {}, set()
        self.lock = threading.Lock()

    def __call__(self, sid, keep, snap=None, fake=False, tag=False):
        t0 = time.monotonic()
        time.sleep(0.3 if sid == "snapshot" else 0.05)
        with self.lock:
            self.times[sid] = (t0, time.monotonic())
            self.snaps[sid] = snap
            self.tags.add(tag)
        return {
            "fails": ["boom"] if sid in self.fail else [],
            "notes": [],
            "calls": 2,
            "max_prompt": 10 if sid == 3 else 5,
            "limit": 8000,
            "dir": f"/nonexistent/snap-{sid}",
        }


def main(argv, children):
    out = io.StringIO()
    with (
        mock.patch.object(run, "run_one", children),
        mock.patch.object(run.gen_history, "write"),
        mock.patch.object(run.shutil, "rmtree") as rmtree,
        redirect_stdout(out),
    ):
        code = run.main(argv)
    return code, out.getvalue().splitlines(), rmtree


class JobsTest(unittest.TestCase):
    def test_sequential_order_unchanged(self):
        kids = FakeChildren()
        code, lines, rmtree = main([], kids)
        self.assertEqual(code, 0)
        order = [ln.split()[1] for ln in lines[:-1]]
        self.assertEqual(order, ["Ssnapshot", *(f"S{i}" for i in run.SCENARIOS)])
        self.assertEqual(kids.tags, {False})
        self.assertTrue(lines[-1].startswith("SUMMARY 8/8 passed; calls=16; max prompt_tokens=10"))
        rmtree.assert_called_once_with("/nonexistent/snap-snapshot", ignore_errors=True)

    def test_parallel_history_waits_for_snapshot(self):
        kids = FakeChildren()
        code, lines, rmtree = main(["--jobs", "4"], kids)
        self.assertEqual(code, 0)
        snap_end = kids.times["snapshot"][1]
        for sid in run.HISTORY_SCENARIOS - {"dry"}:
            self.assertGreaterEqual(kids.times[sid][0], snap_end, sid)
            self.assertEqual(kids.snaps[sid], "/nonexistent/snap-snapshot")
        # non-history scenarios ran while the snapshot was loading
        self.assertLess(kids.times[1][0], snap_end)
        self.assertEqual(kids.tags, {True})
        self.assertEqual(len(lines), 9)
        self.assertEqual(
            sorted(ln.split()[1] for ln in lines[:-1]),
            sorted(["Ssnapshot", *(f"S{i}" for i in run.SCENARIOS)]),
        )
        self.assertTrue(lines[-1].startswith("SUMMARY 8/8 passed; calls=16; max prompt_tokens=10"))
        rmtree.assert_called_once_with("/nonexistent/snap-snapshot", ignore_errors=True)

    def test_parallel_failure_exit_code_and_only(self):
        kids = FakeChildren(fail={4})
        code, lines, _ = main(["--jobs", "3", "--only", "1,4"], kids)
        self.assertEqual(code, 1)
        self.assertNotIn("snapshot", kids.times)  # no history scenario: no snapshot load
        self.assertIn("FAIL S4 Changed mind - boom [calls=2 max_prompt_tokens=5]", lines)
        self.assertTrue(lines[-1].startswith("SUMMARY 1/2 passed"))

    def test_parallel_dry_keep_db(self):
        kids = FakeChildren()
        code, lines, rmtree = main(["--dry", "--jobs", "3", "--keep-db"], kids)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(kids.times["dry"][0], kids.times["snapshot"][1])
        rmtree.assert_not_called()

    def test_parallel_interrupt_cancels_queued(self):
        kids = FakeChildren()
        # Ctrl-C lands in the main thread while it waits; snapshot and S1 hold both workers
        with (
            mock.patch.object(run, "wait", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            main(["--jobs", "2"], kids)
        self.assertLessEqual(set(kids.times), {"snapshot", 1})

    def test_jobs_must_be_positive(self):
        with mock.patch.object(run.gen_history, "write"), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
                run.main(["--jobs", "0"])


if __name__ == "__main__":
    unittest.main()
