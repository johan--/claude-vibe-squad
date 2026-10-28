#!/usr/bin/env python3
"""Receipts are stamped by the writer, validated by the reader, never typed.

Caller-supplied stamps can drift from the registry clock or misorder receipts.
``append_receipt`` owns the clock; the focus anchor reads the newest receipt's
``resume:`` clause so it follows the charter as work advances.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from chrono_state import decisions, thread_charters  # noqa: E402


STAMP = "2026-10-27T16:00:00Z"
RECEIPT = "operator asked for the tests — why: it completes rec 7; resume: run the suite"


def charter_text(loops: str, done: str = "- [ ] shipped", ask: str = "Ship it.") -> str:
    return f"## THE ASK\n{ask}\n\n## OPEN LOOPS\n{loops}\n\n## DONE-WHEN\n{done}\n"


class AppendReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="charter-append-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.active = self.root / thread_charters.CHARTERS_REL
        self.active.mkdir(parents=True)
        self.charter = self.active / "demo.md"
        clock = mock.patch.object(thread_charters, "_utcnow", return_value=STAMP)
        clock.start()
        self.addCleanup(clock.stop)

    def write(self, loops: str, **kwargs: str) -> None:
        self.charter.write_text(charter_text(loops, **kwargs), encoding="utf-8")

    def test_appends_a_stamped_line_and_touches_nothing_else(self) -> None:
        existing = "- 2026-10-26T10:00:00Z | FOLD | earlier — why: w; resume: earlier point"
        self.write(existing)
        os.chmod(self.charter, 0o600)
        before = self.charter.read_text(encoding="utf-8")

        receipt = thread_charters.append_receipt(self.charter, "FOLD", RECEIPT)

        expected_line = f"- {STAMP} | FOLD | {RECEIPT}"
        self.assertEqual(receipt.raw, expected_line)
        self.assertEqual(receipt.classification, "FOLD")
        self.assertEqual(receipt.stamp, STAMP)
        self.assertEqual(receipt.request, "operator asked for the tests")
        self.assertEqual(receipt.why, "it completes rec 7")
        self.assertEqual(receipt.resume, "run the suite")
        after = self.charter.read_text(encoding="utf-8")
        self.assertEqual(after, before.replace(existing, existing + "\n" + expected_line))
        self.assertEqual(self.charter.stat().st_mode & 0o777, 0o600)
        parsed = thread_charters.parse_charter(self.charter)
        self.assertEqual(parsed.issues, ())
        self.assertEqual(parsed.open_loops[-1], receipt)
        self.assertEqual(thread_charters.last_resume_point(parsed), receipt)

    def test_replaces_the_none_placeholder(self) -> None:
        self.write(thread_charters.EMPTY_LOOPS_PLACEHOLDER)
        thread_charters.append_receipt(self.charter, "DROP", RECEIPT)
        text = self.charter.read_text(encoding="utf-8")
        self.assertNotIn("(none)", text)
        self.assertEqual(text, charter_text(f"- {STAMP} | DROP | {RECEIPT}"))
        self.assertEqual(thread_charters.parse_charter(self.charter).issues, ())

    def test_queue_and_resolution_kinds_round_trip(self) -> None:
        self.write(thread_charters.EMPTY_LOOPS_PLACEHOLDER)
        queued = thread_charters.append_receipt(self.charter, "QUEUE Q-007", RECEIPT)
        self.assertEqual((queued.classification, queued.queue_id), ("QUEUE", "Q-007"))
        self.assertEqual(
            [q.queue_id for q in thread_charters.parse_charter(self.charter).unresolved_queues],
            ["Q-007"],
        )
        resolved = thread_charters.append_receipt(self.charter, "FOLD resolves Q-007", RECEIPT)
        self.assertEqual((resolved.classification, resolved.resolves), ("FOLD", "Q-007"))
        parsed = thread_charters.parse_charter(self.charter)
        self.assertEqual(parsed.unresolved_queues, ())
        self.assertEqual(parsed.issues, ())
        self.assertEqual(len(parsed.open_loops), 2)
        thread_charters.append_receipt(self.charter, "QUEUE  Q-008", RECEIPT)
        thread_charters.append_receipt(self.charter, "DROP resolves Q-008", RECEIPT)
        self.assertEqual(thread_charters.parse_charter(self.charter).issues, ())

    def test_refusals_leave_the_bytes_unchanged(self) -> None:
        self.write("- 2026-10-26T10:00:00Z | QUEUE Q-001 | q — why: w; resume: r")
        before = self.charter.read_bytes()
        cases = {
            "typed stamp": ("FOLD", f"{STAMP} | FOLD | x — why: y; resume: z"),
            "typed short stamp": ("FOLD", "2026-10-27T07:21Z | FOLD | x — why: y; resume: z"),
            "classification in text": ("FOLD", "FOLD | x — why: y; resume: z"),
            "decision in text": ("FOLD", "DECISION | x — why: y; resume: z"),
            "deprecated DECLINE": ("DECLINE", RECEIPT),
            "deprecated DECLINE resolves": ("DECLINE resolves Q-001", RECEIPT),
            "unknown kind": ("DROPPED", RECEIPT),
            "lowercase kind": ("fold", RECEIPT),
            "two lines": ("FOLD", "x — why: y; resume: z\nmore"),
            "missing why": ("FOLD", "x; resume: z"),
            "missing resume": ("FOLD", "x — why: y"),
            "queue id reuse": ("QUEUE Q-001", RECEIPT),
            "resolves unknown": ("FOLD resolves Q-999", RECEIPT),
        }
        for label, (kind, text) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(thread_charters.CharterAppendRefused):
                    thread_charters.append_receipt(self.charter, kind, text)
                self.assertEqual(self.charter.read_bytes(), before)

    def test_refuses_symlinks_and_charters_without_open_loops(self) -> None:
        self.write(thread_charters.EMPTY_LOOPS_PLACEHOLDER)
        link = self.active / "link.md"
        link.symlink_to(self.charter)
        with self.assertRaisesRegex(thread_charters.CharterAppendRefused, "regular file"):
            thread_charters.append_receipt(link, "FOLD", RECEIPT)
        headless = self.active / "headless.md"
        headless.write_text("## THE ASK\nx\n\n## DONE-WHEN\n- [ ] y\n", encoding="utf-8")
        before = headless.read_bytes()
        with self.assertRaisesRegex(thread_charters.CharterAppendRefused, "OPEN LOOPS"):
            thread_charters.append_receipt(headless, "FOLD", RECEIPT)
        self.assertEqual(headless.read_bytes(), before)

    def test_positive_control_the_refused_shapes_are_otherwise_appendable(self) -> None:
        """The same text without the typed stamp lands, so the refusal is the stamp."""
        self.write(thread_charters.EMPTY_LOOPS_PLACEHOLDER)
        thread_charters.append_receipt(self.charter, "FOLD", "x — why: y; resume: z")
        self.assertIn(f"- {STAMP} | FOLD | x — why: y; resume: z", self.charter.read_text(encoding="utf-8"))

    def test_last_resume_point_skips_unparseable_lines(self) -> None:
        self.write(
            "- 2026-10-26T10:00:00Z | FOLD | first — why: w; resume: first point\n"
            "- this line is malformed"
        )
        parsed = thread_charters.parse_charter(self.charter)
        self.assertEqual(len(parsed.issues), 1)
        last = thread_charters.last_resume_point(parsed)
        self.assertIsNotNone(last)
        assert last is not None
        self.assertEqual(last.resume, "first point")
        self.write(thread_charters.EMPTY_LOOPS_PLACEHOLDER)
        self.assertIsNone(thread_charters.last_resume_point(thread_charters.parse_charter(self.charter)))

    def test_resolve_charter_accepts_an_active_id_or_a_path(self) -> None:
        self.write(thread_charters.EMPTY_LOOPS_PLACEHOLDER)
        self.assertEqual(thread_charters.resolve_charter("demo", self.root), self.charter)
        self.assertEqual(thread_charters.resolve_charter("demo.md", self.root), self.charter)
        self.assertEqual(thread_charters.resolve_charter(str(self.charter), self.root), self.charter)
        with self.assertRaises(FileNotFoundError):
            thread_charters.resolve_charter("missing", self.root)
        parked = self.root / "_state/chrono/thread-charters/parked"
        parked.mkdir()
        (parked / "old.md").write_text(charter_text("- (none)"), encoding="utf-8")
        with self.assertRaisesRegex(FileNotFoundError, "active"):
            thread_charters.resolve_charter("old", self.root)
        with mock.patch.dict(os.environ, {"VAULT_ROOT": str(self.root)}):
            self.assertEqual(thread_charters.resolve_charter("demo"), self.charter)


class CharterCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="charter-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.active = self.root / thread_charters.CHARTERS_REL
        self.active.mkdir(parents=True)
        self.charter = self.active / "atlas.md"
        self.charter.write_text(charter_text("- (none)"), encoding="utf-8")
        self.journal = self.root / "_state" / "chrono" / "decisions.jsonl"
        journal = mock.patch.object(decisions, "DECISIONS_FILE", self.journal)
        journal.start()
        self.addCleanup(journal.stop)
        clock = mock.patch.object(thread_charters, "_utcnow", return_value=STAMP)
        clock.start()
        self.addCleanup(clock.stop)

    def run_cli(self, *argv: str) -> tuple[int, dict[str, object], str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = thread_charters.main(list(argv))
        payload = json.loads(out.getvalue().splitlines()[0]) if out.getvalue().strip() else {}
        return code, payload, err.getvalue()

    def test_fold_appends_without_a_decision(self) -> None:
        code, report, err = self.run_cli("append", "atlas", "FOLD", RECEIPT, "--root", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertEqual(report["kind"], "FOLD")
        self.assertEqual(report["stamp"], STAMP)
        self.assertNotIn("decision_id", report)
        self.assertFalse(self.journal.exists())

    def test_decision_appends_a_fold_receipt_and_one_journal_line(self) -> None:
        text = "Keep Atlas as the product name — why: operator said keep it (10-24); resume: plan rev 5c"
        code, report, err = self.run_cli("append", "atlas", "DECISION", text, "--root", str(self.root))
        self.assertEqual(code, 0, err)
        self.assertEqual(report["kind"], "FOLD")
        self.assertTrue(str(report["decision_id"]).startswith("DEC-"))
        lines = self.journal.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["decision_id"], report["decision_id"])
        self.assertEqual(event["status"], "confirmed")
        self.assertEqual(event["home"], "product")
        self.assertEqual(event["scope"], ["atlas"])
        self.assertEqual(event["statement"], "Keep Atlas as the product name")
        self.assertEqual(event["source_turn_ids"], [f"charter:atlas:{STAMP}"])
        active = decisions.active_decisions()
        self.assertEqual([e["statement"] for e in active], ["Keep Atlas as the product name"])
        self.assertEqual(active.diagnostics, ())
        self.assertIn(f"- {STAMP} | FOLD | {text}", self.charter.read_text(encoding="utf-8"))

    def test_decision_journal_failure_is_loud_and_keeps_the_receipt(self) -> None:
        with mock.patch.object(decisions, "record", side_effect=OSError("disk")):
            code, report, err = self.run_cli(
                "append", "atlas", "DECISION", RECEIPT, "--root", str(self.root)
            )
        self.assertEqual(code, 4)
        self.assertIn("receipt appended but decisions.jsonl was not", err)
        self.assertEqual(report["kind"], "FOLD")
        self.assertIn(RECEIPT, self.charter.read_text(encoding="utf-8"))

    def test_refusals_exit_three_without_writing(self) -> None:
        before = self.charter.read_bytes()
        for argv in (
            ("append", "atlas", "FOLD", f"{STAMP} | FOLD | x — why: y; resume: z"),
            ("append", "atlas", "DECLINE", RECEIPT),
            ("append", "missing", "FOLD", RECEIPT),
            ("append", "atlas", "FOLD", "no why or resume here"),
        ):
            with self.subTest(argv=argv):
                code, report, err = self.run_cli(*argv, "--root", str(self.root))
                self.assertEqual(code, 3)
                self.assertIn("charter append ERROR", err)
                self.assertEqual(report, {})
        self.assertEqual(self.charter.read_bytes(), before)
        self.assertFalse(self.journal.exists())


if __name__ == "__main__":
    unittest.main()
