#!/usr/bin/env python3
"""Explicit Chrono settlement outranks later terminal receipt projection."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
import tempfile
import unittest

import test_registry_reconciler_board_receipt as fixtures


reconciler = fixtures.reconciler

TASK_ID = "TASK-2026-09-22-1200-settlement-regression"

# The differential table both reviews of this fix converged on. Each row pairs a
# registry status and a review_settled_by value against a quarantined terminal
# receipt, with the status reconcile must leave behind.
#
#   A, F, G  the three markers settle_review() actually writes -- stay complete
#   B, D     the value-blindness a "chrono-" prefix predicate let through
#   C, E     negative controls: an unsettled complete row is still held
#   H        a reopened row that never got back to complete
#
# C and E are what make the table evidence rather than decoration: if the guard
# were widened to accept anything, they would flip too.
DIFFERENTIAL_ROWS = (
    ("A", "complete", "chrono-explicit", "complete"),
    ("B", "complete", "chrono-reopened", reconciler.REVIEW_REQUIRED),
    ("C", "complete", "worker", reconciler.REVIEW_REQUIRED),
    ("D", "complete", "chrono-bogus", reconciler.REVIEW_REQUIRED),
    ("E", "complete", "", reconciler.REVIEW_REQUIRED),
    ("F", "complete", "chrono-explicit-independent", "complete"),
    ("G", "complete", "chrono-factual-attestation", "complete"),
    ("H", "needs_review", "chrono-reopened", reconciler.REVIEW_REQUIRED),
)


class SettledStaysSettledTests(unittest.TestCase):
    @staticmethod
    def _reconcile_receipt(
        *,
        initial_status: str = "complete",
        settled_by: str = "chrono-explicit",
        failure_class: str = "cli_timeout",
        receipt_status: str = "needs_review",
        targeted: bool = False,
        passes: int = 2,
    ) -> tuple[dict, dict, list[str], list[int]]:
        task_id = TASK_ID
        attempt_id = "d-" + "9" * 32
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "_state"
            registry_path = state / "active-tasks.json"
            entry = fixtures.BoardReceiptSettlementTests._v2_entry(
                task_id,
                attempt_id,
                status=initial_status,
                delivery_state="terminal",
                mandatory_review="true",
                review_triggers=["blast_radius"],
                review_model="claude",
            )
            if settled_by:
                entry.update(
                    review_settled_at="2026-09-22T16:43:23Z",
                    review_settled_by=settled_by,
                    completed_at="2026-09-22T16:43:23Z",
                    reconciled_at="2026-09-22T16:43:23Z",
                    cross_family_review_ref="departments/coding/outbox/review.md",
                    review_ref="departments/coding/outbox/review.md",
                    verdict="APPROVE",
                    review_force_override=False,
                )
            descriptor = fixtures.BoardReceiptSettlementTests._write_v2_descriptor(state, task_id, attempt_id)
            receipt_fields = {}
            if failure_class:
                receipt_fields = {
                    "status": "blocked",
                    "failure_class": failure_class,
                    "cli_exec_succeeded": False,
                    "controller_quarantine": reconciler.CONTROLLER_QUARANTINE_MARKER,
                    "response_status": "needs_review",
                }
            receipt = fixtures.BoardReceiptSettlementTests._write_v2_receipt(
                descriptor,
                completed_at="2026-09-22T15:27:35Z",
                terminal_outcome=receipt_status,
                **receipt_fields,
            )
            entry["terminal_receipt_path"] = str(receipt.relative_to(root))
            entry["terminal_receipt_status"] = receipt_status
            reconciler.apply_receipt_diagnostics(
                entry, reconciler.receipt_failure_diagnostics(receipt)
            )
            registry_path.write_text(json.dumps({task_id: entry}), encoding="utf-8")
            messages: list[str] = []
            change_counts: list[int] = []
            with fixtures.BoardReceiptSettlementTests._patch_runtime(root, state, registry_path):
                for _ in range(passes):
                    pass_changes, pass_messages = reconciler.reconcile(
                        task_id if targeted else None, dry_run=False
                    )
                    change_counts.append(pass_changes)
                    messages.extend(pass_messages)
            result = json.loads(registry_path.read_text(encoding="utf-8"))[task_id]
        return entry, result, messages, change_counts

    def test_settlement_markers_match_settle_review_literals(self) -> None:
        """Pin the guard's vocabulary to its only producer, in both directions."""

        source = ast.parse(inspect.getsource(reconciler.settle_review))
        markers = {
            node.value.value
            for node in ast.walk(source)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "settled_by"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        }
        # settle_review() gains a fourth settlement class -> this fails.
        self.assertEqual(markers, {
            "chrono-explicit",
            "chrono-explicit-independent",
            "chrono-factual-attestation",
        })
        # The constant gains a value settle_review() never writes (the
        # security-relevant direction: chrono-reopened) -> this fails.
        self.assertEqual(reconciler.CONTROLLER_SETTLEMENT_MARKERS, markers)

    def test_differential_table_holds_targeted_and_sweep(self) -> None:
        """Rows A-H, both reconcile shapes, against a quarantined receipt."""

        for row, initial_status, settled_by, expected in DIFFERENTIAL_ROWS:
            for targeted in (False, True):
                with self.subTest(row=row, settled_by=settled_by, targeted=targeted):
                    _, after, _, _ = self._reconcile_receipt(
                        initial_status=initial_status,
                        settled_by=settled_by,
                        targeted=targeted,
                    )
                    self.assertEqual(after["status"], expected)

    def test_settled_row_reconciles_idempotently(self) -> None:
        """A settled row is not rewritten, and reports no change, on any pass.

        The reverted hardening moved settled rows into the terminal-receipt
        branch, where `changed += 1` ran unconditionally and `reconciled_at` was
        restamped every pass. That put a permanent nonzero floor under
        `changes=N` and made "did anything happen?" unanswerable from the count.
        """

        for settled_by in sorted(reconciler.CONTROLLER_SETTLEMENT_MARKERS):
            for targeted in (False, True):
                with self.subTest(settled_by=settled_by, targeted=targeted):
                    before, after, _, change_counts = self._reconcile_receipt(
                        settled_by=settled_by, targeted=targeted, passes=3
                    )
                    self.assertEqual(change_counts, [0, 0, 0])
                    self.assertEqual(after["status"], "complete")
                    self.assertEqual(after, before)
                    self.assertEqual(after["completed_at"], "2026-09-22T16:43:23Z")
                    self.assertEqual(after["reconciled_at"], "2026-09-22T16:43:23Z")

    def test_targeted_settled_reconcile_emits_already_settled(self) -> None:
        """bin/outbox-watcher.sh keys reconciler_handled on this exact line.

        Silence here re-queues settled work to Chrono and leaves the task packet
        in the live mailbox. test_reconciler_watcher_contract.py reads the
        accepted forms out of the watcher itself; this asserts the line is
        emitted at all, for every marker and every receipt shape.
        """

        for settled_by in sorted(reconciler.CONTROLLER_SETTLEMENT_MARKERS):
            for failure_class in ("cli_timeout", "cli_nonzero", ""):
                with self.subTest(settled_by=settled_by, failure_class=failure_class):
                    _, _, messages, _ = self._reconcile_receipt(
                        settled_by=settled_by,
                        failure_class=failure_class,
                        targeted=True,
                    )
                    self.assertIn(f"already-settled {TASK_ID} -> complete", messages)

    def test_sweep_settled_reconcile_stays_quiet(self) -> None:
        """Negative control: the message is targeted-only, as it was before."""

        _, _, messages, _ = self._reconcile_receipt(targeted=False)
        self.assertEqual([m for m in messages if TASK_ID in m], [])

    def test_explicit_settlement_survives_quarantined_receipt(self) -> None:
        for settled_by in (
            "chrono-explicit",
            "chrono-explicit-independent",
            "chrono-factual-attestation",
        ):
            for failure_class in ("cli_timeout", "cli_nonzero"):
                for targeted in (False, True):
                    with self.subTest(
                        settled_by=settled_by,
                        failure_class=failure_class,
                        targeted=targeted,
                    ):
                        before, after, messages, _ = self._reconcile_receipt(
                            settled_by=settled_by,
                            failure_class=failure_class,
                            targeted=targeted,
                        )
                        self.assertEqual(after["status"], "complete")
                        self.assertEqual(after, before)
                        self.assertFalse(any("review-required" in m for m in messages))

    def test_explicit_settlement_survives_ordinary_receipts(self) -> None:
        for receipt_status in ("needs_review", "complete", "blocked"):
            with self.subTest(receipt_status=receipt_status):
                before, after, _, _ = self._reconcile_receipt(
                    failure_class="", receipt_status=receipt_status
                )
                self.assertEqual(after, before)

    def test_unsettled_needs_review_receipt_still_requires_review(self) -> None:
        for failure_class in ("cli_timeout", "cli_nonzero", ""):
            with self.subTest(failure_class=failure_class):
                _, after, messages, _ = self._reconcile_receipt(
                    initial_status="in-flight", settled_by="",
                    failure_class=failure_class,
                )
                self.assertEqual(after["status"], reconciler.REVIEW_REQUIRED)
                self.assertEqual(after["review_required_by"], "claude")
                self.assertTrue(any("review-required" in m for m in messages))

    def test_complete_without_controller_settlement_still_requires_review(self) -> None:
        for settled_by in ("", "worker", "chrono-reopened", "chrono-bogus"):
            with self.subTest(settled_by=settled_by):
                _, after, _, _ = self._reconcile_receipt(settled_by=settled_by)
                self.assertEqual(after["status"], reconciler.REVIEW_REQUIRED)

    def test_reopened_task_is_not_protected_by_historical_settlement(self) -> None:
        # initial_status is deliberately "complete": with "needs_review" the
        # guard's first conjunct is False and the predicate is never evaluated,
        # so the test passed no matter how wide the predicate was.
        _, after, _, _ = self._reconcile_receipt(
            initial_status="complete", settled_by="chrono-reopened"
        )
        self.assertEqual(after["status"], reconciler.REVIEW_REQUIRED)


if __name__ == "__main__":
    unittest.main()
