#!/usr/bin/env python3
"""The shell wrappers are the only writers: drive them as processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from chrono_state import thread_charters, workboard  # noqa: E402


CHARTER_APPEND = ROOT / "bin" / "charter-append.sh"
WORKBOARD_APPEND = ROOT / "bin" / "workboard-append.sh"
RECEIPT = "operator approved recs 1-9 — why: the audit landed; resume: dispatch tracking fixes A"


class WrapperFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="wrapper-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.active = self.root / thread_charters.CHARTERS_REL
        self.active.mkdir(parents=True)
        self.charter = self.active / "atlas.md"
        self.charter.write_text(
            "## THE ASK\nShip Atlas.\n\n## OPEN LOOPS\n- (none)\n\n## DONE-WHEN\n- [ ] shipped\n",
            encoding="utf-8",
        )
        self.ledger = self.root / workboard.WORKBOARD_REL
        self.journal = self.root / "_state" / "chrono" / "decisions.jsonl"

    def run_wrapper(self, script: Path, *argv: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "VAULT_ROOT": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        environment.pop("PYTHONPATH", None)
        return subprocess.run(
            ["/bin/bash", str(script), *argv],
            capture_output=True,
            text=True,
            cwd=self.root,
            env=environment,
            timeout=30,
        )

    @staticmethod
    def payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        return json.loads(result.stdout.splitlines()[0])


class CharterAppendWrapperTests(WrapperFixture):
    def test_fold_lands_with_a_wrapper_stamp_and_no_journal(self) -> None:
        result = self.run_wrapper(CHARTER_APPEND, "atlas", "FOLD", RECEIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.payload(result)
        self.assertEqual(report["kind"], "FOLD")
        self.assertRegex(str(report["stamp"]), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(report["charter"], str(self.charter))
        parsed = thread_charters.parse_charter(self.charter)
        self.assertEqual(parsed.issues, ())
        self.assertEqual(parsed.open_loops[-1].raw, report["line"])
        self.assertEqual(parsed.open_loops[-1].resume, "dispatch tracking fixes A")
        self.assertFalse(self.journal.exists())

    def test_decision_writes_the_receipt_and_one_journal_line(self) -> None:
        text = "Keep Atlas — why: operator said so on 10-24; resume: plan rev 5c"
        result = self.run_wrapper(CHARTER_APPEND, "atlas", "DECISION", text)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.payload(result)
        self.assertEqual(report["kind"], "FOLD")
        self.assertEqual(report["decisions_file"], str(self.journal))
        lines = self.journal.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["decision_id"], report["decision_id"])
        self.assertEqual((event["status"], event["home"], event["scope"]), ("confirmed", "product", ["atlas"]))
        self.assertEqual(event["statement"], "Keep Atlas")
        self.assertIn(f"| FOLD | {text}", self.charter.read_text(encoding="utf-8"))

    def test_queue_kind_is_one_quoted_argument(self) -> None:
        result = self.run_wrapper(CHARTER_APPEND, "atlas", "QUEUE Q-003", RECEIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [q.queue_id for q in thread_charters.parse_charter(self.charter).unresolved_queues],
            ["Q-003"],
        )
        result = self.run_wrapper(CHARTER_APPEND, str(self.charter), "DROP resolves Q-003", RECEIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(thread_charters.parse_charter(self.charter).unresolved_queues, ())

    def test_refusals_change_nothing(self) -> None:
        before = self.charter.read_bytes()
        cases = {
            "typed stamp": ("atlas", "FOLD", "2026-10-27T07:21Z | FOLD | x — why: y; resume: z"),
            "deprecated kind": ("atlas", "DECLINE", RECEIPT),
            "unknown thread": ("missing", "FOLD", RECEIPT),
            "no resume": ("atlas", "FOLD", "x — why: y"),
        }
        for label, argv in cases.items():
            with self.subTest(case=label):
                result = self.run_wrapper(CHARTER_APPEND, *argv)
                self.assertEqual(result.returncode, 3, result.stdout)
                self.assertIn("charter append ERROR", result.stderr)
        self.assertEqual(self.charter.read_bytes(), before)
        self.assertFalse(self.journal.exists())
        usage = self.run_wrapper(CHARTER_APPEND, "atlas", "FOLD")
        self.assertEqual(usage.returncode, 2)
        self.assertIn("charter-append.sh", usage.stdout)


class WorkboardAppendWrapperTests(WrapperFixture):
    def test_start_then_queue_then_switch_through_the_wrapper(self) -> None:
        start = self.run_wrapper(
            WORKBOARD_APPEND, "start", "--alias", "ATLAS", "--summary", "ship atlas",
            "--why", "approved", "--next", "see charter atlas",
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        report = self.payload(start)
        self.assertEqual(report["kind"], "start")
        self.assertRegex(str(report["work_id"]), r"^W-[0-9a-f]{32}$")
        self.assertRegex(str(report["event_id"]), r"^EV-[0-9a-f]{12}$")
        self.assertTrue(str(report["line"]).startswith("- workboard-event/v1 "))

        queue = self.run_wrapper(
            WORKBOARD_APPEND, "queue", "--alias", "Q-SQUAD", "--summary", "refresh",
            "--why", "operator asked", "--resume", "after atlas",
        )
        self.assertEqual(queue.returncode, 0, queue.stderr)
        switch = self.run_wrapper(
            WORKBOARD_APPEND, "switch", "--alias", "Q-SQUAD", "--next", "start the refresh"
        )
        self.assertEqual(switch.returncode, 0, switch.stderr)
        self.assertEqual(self.payload(switch)["work_id"], self.payload(queue)["work_id"])
        projection = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(projection.active_item_id, "Q-SQUAD")
        self.assertEqual(projection.next_action, "start the refresh")
        self.assertEqual(len(projection.items), 2)

    def test_refused_appends_exit_three_and_write_nothing(self) -> None:
        missing = self.run_wrapper(WORKBOARD_APPEND, "queue", "--alias", "X", "--summary", "s")
        self.assertEqual(missing.returncode, 3)
        self.assertIn("queue requires --alias --summary --why --resume; missing --why --resume", missing.stderr)
        self.assertFalse(self.ledger.exists())
        # Positive control for the library's postcondition: a queue cannot be the
        # first event because nothing would be active; the wrapper surfaces it.
        rejected = self.run_wrapper(
            WORKBOARD_APPEND, "queue", "--alias", "X", "--summary", "s", "--why", "w", "--resume", "r"
        )
        self.assertEqual(rejected.returncode, 3)
        self.assertIn("append rejected", rejected.stderr)
        self.assertFalse(self.ledger.exists())
        usage = self.run_wrapper(WORKBOARD_APPEND)
        self.assertEqual(usage.returncode, 2)
        self.assertIn("workboard-append.sh", usage.stdout)
        unknown = self.run_wrapper(WORKBOARD_APPEND, "adopt", "--alias", "X")
        self.assertEqual(unknown.returncode, 2)


if __name__ == "__main__":
    unittest.main()
