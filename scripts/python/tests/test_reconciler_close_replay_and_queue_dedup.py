"""Q8/Q9 controls from the September 23 dispatch audit, using isolated state."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from threading import Barrier
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import registry_reconciler as rr


AUTO_TASK = "TASK-2026-09-23-0207-afc64f57"
REVIEW_TASK = "TASK-2026-09-23-0242-f13e35dc"
CLOSED_AT = "2026-09-23T09:12:43.959875+00:00"
CLOSED_REASON = "terminal board receipt=complete rc=0"
# Exact payload shared by queue lines 1378 and 1379 in the audit snapshot.
REVIEW_SUMMARY = (
    "claude specialist 'test-engineer' is held; controller-quarantined output "
    "requires a distinct-family review lane"
)
REVIEW_NUDGE = (
    f"REVIEW-REQUIRED: {REVIEW_TASK} (test-engineer, lane claude) cannot settle: "
    "controller-quarantined output requires a distinct-family review lane. "
    "Correct the packet contract and redispatch; this invalid entry cannot be review-settled."
)


def auto_closed_entry():
    return {
        "status": "closed",
        "delivery_generation": 1,
        "delivery_state": "terminal",
        "to_model": "gpt-codex",
        "mandatory_review": "false",
        "review_triggers": [],
        "review_model": "none",
        "review_disposition": "not-required",
        "terminal_receipt_status": "complete",
        "terminal_receipt_returncode": 0,
        "lifecycle_closed_by": "registry-reconciler-auto",
        "lifecycle_closed_at": CLOSED_AT,
        "closed_from_status": "complete",
        "closure_reason": CLOSED_REASON,
        "closure_history": [{
            "at": CLOSED_AT, "from_status": "complete", "to_status": "closed",
            "reason": CLOSED_REASON, "by": "registry-reconciler-auto",
        }],
    }


class CloseReplayAndQueueDedupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.root / "_state"
        self.state.mkdir()
        self.registry_path = self.state / "active-tasks.json"
        self.queue_path = self.state / "chrono-queue.md"
        for name, value in {
            "VAULT_ROOT": self.root,
            "STATE_DIR": self.state,
            "REGISTRY_PATH": self.registry_path,
            "CHRONO_QUEUE_PATH": self.queue_path,
            "CHRONO_NOTIFY_LOCKDIR": self.state / "notify.lockdir",
            "CHRONO_NOTIFY_RECEIPTS_DIR": self.state / "receipts",
        }.items():
            self.stack.enter_context(mock.patch.object(rr, name, value))
        self.stack.enter_context(mock.patch.dict(rr.os.environ, {rr.TEST_ISOLATION_ENV: "1"}))
        self.stack.enter_context(mock.patch.object(rr, "registered_in_canonical_registry", return_value=True))
        self.nudge = self.stack.enter_context(mock.patch.object(rr, "nudge_chrono", return_value=True))
        self.now = datetime.now(timezone.utc)

    def write_registry(self, entries):
        self.registry_path.write_text(json.dumps(entries) + "\n")

    def event(self, task=REVIEW_TASK, status="REVIEW-REQUIRED", generation=1, entry=None):
        entry = deepcopy(entry) if entry is not None else {"status": "review-required"}
        entry["delivery_generation"] = generation
        registry = {task: entry}
        events = []
        rr.append_terminal_event(
            events, entry, task, "coding", self.now, status,
            REVIEW_SUMMARY if task == REVIEW_TASK else "terminal board status=complete; disposition=closed",
            REVIEW_NUDGE if task == REVIEW_TASK else "AUTO-CLOSED: successful completion",
        )
        deliveries, _changed = rr._prepare_notification_deliveries(registry, events, None)
        return registry, deliveries

    def rows(self):
        return [line for line in self.queue_path.read_text().splitlines() if " | " in line and not line.startswith("#")]

    def test_terminal_quarantine_emits_watcher_handled_status_line(self):
        task = "TASK-quarantine-status-line"
        for initial in ("blocked", "complete"):
            with self.subTest(initial=initial):
                self.write_registry({task: {"status": initial, "to_model": "gpt-codex",
                    "mandatory_review": False, "review_triggers": []}})
                receipt = self.state / "terminal.receipt.json"
                receipt.write_text("{}")
                with mock.patch.object(rr, "terminal_board_receipt", return_value=(
                        receipt, "blocked", "needs_review", self.now.isoformat())), mock.patch.object(
                        rr, "receipt_failure_diagnostics", return_value={
                            "controller_quarantine": rr.CONTROLLER_QUARANTINE_MARKER,
                            "response_status": "needs_review"}):
                    _, messages = rr.reconcile(task, False)
                self.assertEqual(json.loads(self.registry_path.read_text())[task]["status"], "complete")
                self.assertIn(f"reconciled {task} -> complete via terminal board receipt (transport quarantine)", messages)

    def test_quarantine_respects_triggers_for_response_and_receipt(self):
        for via_receipt in (False, True):
            for triggered in (False, True):
                with self.subTest(via_receipt=via_receipt, triggered=triggered):
                    task = f"TASK-quarantine-{via_receipt}-{triggered}"
                    entry = {"status": "review-required", "to_model": "gpt-codex",
                             "review_model": "claude" if triggered else "none",
                             "mandatory_review": triggered,
                             "review_triggers": ["blast_radius"] if triggered else [],
                             "delivery_generation": 1,
                             "dispatched_at": "2000-01-01T00:00:00+00:00"}
                    self.write_registry({task: entry})
                    response = self.root / "departments/coding/outbox" / f"{task}-response.md"
                    response.parent.mkdir(parents=True, exist_ok=True)
                    response.write_text("---\nstatus: needs_review\n---\n\ncontroller_quarantine: " + rr.CONTROLLER_QUARANTINE_MARKER + "\n\nValidated recovered result.\n")
                    receipt = self.state / f"{task}.receipt.json"
                    receipt.write_text("{}")
                    with ExitStack() as patches:
                        patches.enter_context(mock.patch.object(rr, "RESPONSE_MIN_AGE", rr.timedelta(0)))
                        if via_receipt:
                            patches.enter_context(mock.patch.object(rr, "landed_response", return_value=(None, "")))
                            patches.enter_context(mock.patch.object(rr, "response_candidates", return_value=[]))
                            patches.enter_context(mock.patch.object(rr, "terminal_board_receipt", return_value=(receipt, "blocked", "needs_review", self.now.isoformat())))
                            patches.enter_context(mock.patch.object(rr, "receipt_failure_diagnostics", return_value={"controller_quarantine": rr.CONTROLLER_QUARANTINE_MARKER, "response_status": "needs_review"}))
                        rr.reconcile(task, False)
                        result = json.loads(self.registry_path.read_text())[task]
                        if triggered:
                            self.assertEqual(result["status"], "review-required")
                            self.assertEqual(result["review_required_by"], "claude")
                        else:
                            self.assertEqual(result["status"], "complete")
                            self.assertEqual(result["review_disposition"], "transport-quarantine-not-required" if via_receipt else "not-required")
                            self.assertTrue(result["coordination_requested"])
                            self.assertEqual(result["coordination_request_source"], "controller-transport-quarantine" if via_receipt else "legacy-needs_review-status")
                            self.assertNotIn("review_required_by", result)
                            from chrono_state.notifications import NotificationStore
                            notices = NotificationStore(self.root).list_pending()
                            self.assertTrue(any(n['identity'] == ['task', task, 1, 'coordination-requested'] for n in notices))
                        before = self.registry_path.read_bytes()
                        rr.reconcile(task, False)
                        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_authenticated_v2_transport_failure_resolves_by_review_trigger(self):
        from test_registry_reconciler_board_receipt import BoardReceiptSettlementTests
        case = BoardReceiptSettlementTests()
        original_entry = case._v2_entry
        for failure in ("cli_nonzero", "cli_timeout"):
            for initial in ("in-flight", "complete"):
                for triggered in (False, True):
                    with self.subTest(failure=failure, initial=initial, triggered=triggered):
                        def entry(*args, **kwargs):
                            kwargs.update(mandatory_review=triggered,
                                          review_triggers=["blast_radius"] if triggered else [])
                            return original_entry(*args, **kwargs)
                        with mock.patch.object(case, "_v2_entry", side_effect=entry):
                            result = case._run_transport_receipt(
                                failure_class=failure, quarantined=True, initial_status=initial)
                        self.assertEqual(result['status'], 'review-required' if triggered else 'complete')
                        self.assertEqual(result['terminal_receipt_controller_quarantine'], rr.CONTROLLER_QUARANTINE_MARKER)
                        self.assertEqual(result['terminal_receipt_failure_class'], failure)
                        if triggered:
                            self.assertEqual(result['review_required_by'], 'claude')
                        else:
                            self.assertEqual(result['review_disposition'], 'transport-quarantine-not-required')
                            self.assertTrue(result['coordination_requested'])

    def test_0207_automatic_close_replay_preserves_original_record(self):
        self.write_registry({AUTO_TASK: auto_closed_entry()})
        before = self.registry_path.read_bytes()
        for reason in (CLOSED_REASON, "operator confirms completion"):
            with self.subTest(reason=reason):
                report = rr.close_task(AUTO_TASK, reason, "closed")
                self.assertFalse(report)
                self.assertEqual(report.outcomes[0].disposition, rr.CLOSE_ALREADY)
                self.assertIn(CLOSED_AT, report.outcomes[0].detail)
                self.assertIn(CLOSED_REASON, report.outcomes[0].detail)
                self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertFalse(self.queue_path.exists())

    def test_explicit_close_remains_exact_reason_only(self):
        entry = auto_closed_entry()
        entry["lifecycle_closed_by"] = "chrono-explicit"
        self.write_registry({AUTO_TASK: entry})
        self.assertFalse(rr.close_task(AUTO_TASK, CLOSED_REASON, "closed"))
        before = self.registry_path.read_bytes()
        with self.assertRaises(rr.BatchCloseRefused):
            rr.close_task(AUTO_TASK, "conflicting reason", "closed")
        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_conflicting_status_refuses_whole_batch_before_writes(self):
        self.write_registry({REVIEW_TASK: {"status": "review-required"}, AUTO_TASK: auto_closed_entry()})
        before = self.registry_path.read_bytes()
        with self.assertRaises(rr.BatchCloseRefused) as caught:
            rr.close_task([REVIEW_TASK, AUTO_TASK], "supersede both", "superseded")
        self.assertEqual([o.disposition for o in caught.exception.report.outcomes], [rr.CLOSE_ELIGIBLE, rr.CLOSE_REFUSED])
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertFalse(self.queue_path.exists())

    def test_unknown_task_ref_refuses_batch_including_automatic_replay(self):
        self.write_registry({REVIEW_TASK: {"status": "review-required"}, AUTO_TASK: auto_closed_entry()})
        before = self.registry_path.read_bytes()
        with self.assertRaisesRegex(rr.BatchCloseRefused, "unknown registry task"):
            rr.close_task([REVIEW_TASK, AUTO_TASK, "coding/" + AUTO_TASK], "close", "closed")
        self.assertEqual(self.registry_path.read_bytes(), before)
        self.assertFalse(self.queue_path.exists())

    def test_valid_mixed_batch_closes_open_member_only(self):
        original = auto_closed_entry()
        self.write_registry({REVIEW_TASK: {"status": "review-required"}, AUTO_TASK: original})
        report = rr.close_task([REVIEW_TASK, AUTO_TASK], "close remaining work", "closed")
        self.assertTrue(report)
        self.assertEqual([o.disposition for o in report.outcomes], [rr.CLOSE_CLOSED, rr.CLOSE_ALREADY])
        registry = json.loads(self.registry_path.read_text())
        self.assertEqual(registry[AUTO_TASK], original)
        self.assertEqual(len(registry[REVIEW_TASK]["closure_history"]), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_0242_concurrent_delivery_writes_one_durable_row(self):
        registry, deliveries = self.event()
        self.write_registry(registry)
        barrier = Barrier(2)

        def deliver():
            barrier.wait(timeout=5)
            return rr.deliver_notification_sweep(deliveries, registry, self.now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(deliver) for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [[True], [True]])
        self.assertEqual(len(self.rows()), 1)
        self.assertIn(REVIEW_SUMMARY + " / " + REVIEW_NUDGE, self.rows()[0])
        self.nudge.assert_called()

    def test_nine_recurring_audit_appends_preserve_all_nine_rows(self):
        payloads = (
            ("long-running:active", "pane snippet one"),
            ("long-running:active", "pane snippet two"),
            ("long-running:active", "pane snippet three"),
            ("REVIEW-REOPENED", "first reopen"),
            ("REVIEW-SETTLED", "first settlement"),
            ("REVIEW-REOPENED", "second reopen"),
            ("REVIEW-SETTLED", "second settlement"),
            ("ENVELOPE-REPAIRED", "prior status blocked"),
            ("ENVELOPE-REPAIRED", "prior status needs_review"),
        )
        self.write_registry({REVIEW_TASK: {"delivery_generation": 1}})
        for status, summary in payloads:
            rr.append_chrono_queue(status, f"coding/{REVIEW_TASK}", summary)
        rows = self.rows()
        self.assertEqual(len(rows), 9)
        self.assertEqual(
            [tuple(row.split(" | ")[1::2]) for row in rows], list(payloads)
        )

    def test_concurrent_digest_delivery_preserves_one_row_per_event(self):
        registry, deliveries = {}, []
        for index in range(4):
            entries, events = self.event(f"{REVIEW_TASK}-{index}")
            registry.update(entries)
            deliveries.extend(events)
        barrier = Barrier(2)

        def deliver():
            barrier.wait(timeout=5)
            return rr.deliver_notification_sweep(deliveries, registry, self.now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(deliver) for _ in range(2)]
            self.assertEqual([f.result(timeout=10) for f in futures], [[True] * 4] * 2)
        self.assertEqual(len(self.rows()), 4)
        self.assertTrue(all(len(row.split(" | ")) == 4 for row in self.rows()))
        self.assertTrue(all("digest_events" in call.kwargs for call in self.nudge.call_args_list))

    def test_unmanaged_append_never_reads_registry(self):
        with mock.patch.object(rr, "load_registry", side_effect=AssertionError("registry read")) as load:
            rr.append_chrono_queue("long-running:active", f"coding/{REVIEW_TASK}", "heartbeat")
        load.assert_not_called()
        self.assertEqual(len(self.rows()), 1)

    def test_unmanaged_audit_row_survives_matching_notification_identity(self):
        registry, deliveries = self.event()
        self.write_registry(registry)
        rr.deliver_notification_sweep(deliveries, registry, self.now)
        rr.append_chrono_queue("REVIEW-REQUIRED", f"coding/{REVIEW_TASK}", "independent audit")
        rr.deliver_notification_sweep(deliveries, registry, self.now)
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(self.rows()[1].endswith(" | independent audit"))

    def test_distinct_task_generation_and_disposition_each_keep_a_row(self):
        for task, status, generation in (
            (REVIEW_TASK, "REVIEW-REQUIRED", 1),
            (AUTO_TASK, "REVIEW-REQUIRED", 1),
            (REVIEW_TASK, "REVIEW-REQUIRED", 2),
            (REVIEW_TASK, "COORDINATION-REQUESTED", 1),
        ):
            registry, deliveries = self.event(task, status, generation)
            self.write_registry(registry)
            rr.deliver_notification_sweep(deliveries, registry, self.now)
        self.assertEqual(len(self.rows()), 4)

    def test_stale_pending_generation_does_not_use_current_generation(self):
        registry, deliveries = self.event(generation=1)
        registry[REVIEW_TASK]["delivery_generation"] = 2
        self.write_registry(registry)
        rr.deliver_notification_sweep(deliveries, registry, self.now)
        newer_registry, newer = self.event(generation=2)
        rr.deliver_notification_sweep(newer, newer_registry, self.now)
        rr.deliver_notification_sweep(deliveries, registry, self.now)
        self.assertEqual(len(self.rows()), 2)

    def test_crash_before_ack_replays_queue_without_another_row(self):
        registry, _deliveries = self.event()
        self.write_registry(registry)
        with mock.patch.object(rr, "acknowledge_notification_delivery", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                rr.reconcile(REVIEW_TASK, dry_run=False)
        rr.reconcile(REVIEW_TASK, dry_run=False)
        self.assertEqual(len(self.rows()), 1)
        self.assertNotIn(rr.NOTIFICATION_PENDING_EVENTS_FIELD, json.loads(self.registry_path.read_text())[REVIEW_TASK])

    def test_clean_auto_close_delivery_outcome_tracks_nudge(self):
        for sent in (True, False):
            with self.subTest(sent=sent):
                registry, deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=auto_closed_entry())
                self.nudge.reset_mock()
                self.nudge.return_value = sent
                self.assertEqual(rr.deliver_notification_sweep(deliveries, registry, self.now), [sent])
                self.nudge.assert_called_once_with(
                    rr.compact_notice("AUTO-CLOSED", f"coding/{AUTO_TASK}", deliveries[0][3]),
                    rr.notification_event_key(f"coding/{AUTO_TASK}", "AUTO-CLOSED"),
                )

    def test_failed_clean_auto_close_nudge_stays_pending_until_retry_succeeds(self):
        registry, _deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=auto_closed_entry())
        self.write_registry(registry)
        self.nudge.return_value = False
        _, messages = rr.reconcile(AUTO_TASK, dry_run=False)
        entry = json.loads(self.registry_path.read_text())[AUTO_TASK]
        key = f"{AUTO_TASK}|AUTO-CLOSED|1"
        self.assertNotIn("notification_key", entry)
        self.assertNotIn("notification_last_emitted_at", entry)
        pending = entry[rr.NOTIFICATION_PENDING_EVENTS_FIELD][key]
        self.assertEqual(pending["outcome"], "failed")
        self.assertEqual(pending["failure_count"], 1)
        self.assertTrue(pending["queue_recorded"])
        self.assertIn(f"chrono-nudge queued-only coding/{AUTO_TASK}", messages)
        self.nudge.assert_called_once()

        self.nudge.return_value = True
        rr.reconcile(AUTO_TASK, dry_run=False)
        entry = json.loads(self.registry_path.read_text())[AUTO_TASK]
        self.assertEqual(entry["notification_key"], key)
        self.assertEqual(entry["notification_failure_count"], 1)
        self.assertIn("notification_last_emitted_at", entry)
        self.assertNotIn(rr.NOTIFICATION_PENDING_EVENTS_FIELD, entry)
        self.assertEqual(self.nudge.call_count, 2)
        self.assertEqual(len(self.rows()), 1)

    def test_clean_auto_close_notice_keeps_status_task_and_queue_pointer(self):
        registry, deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=auto_closed_entry())
        rr.deliver_notification_sweep(deliveries, registry, self.now)
        self.nudge.assert_called_once()
        notice = self.nudge.call_args.args[0]
        self.assertIn("auto-closed", notice)
        self.assertIn(AUTO_TASK, notice)
        self.assertIn("_state/chrono-queue.md", notice)
        self.assertLess(len(notice), 300)

    def test_successful_auto_close_is_queued_and_acknowledged_after_nudge(self):
        registry, _deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=auto_closed_entry())
        self.write_registry(registry)
        _changed, messages = rr.reconcile(AUTO_TASK, dry_run=False)
        before = self.registry_path.read_bytes(), self.queue_path.read_bytes()
        rr.reconcile(AUTO_TASK, dry_run=False)
        self.assertEqual((self.registry_path.read_bytes(), self.queue_path.read_bytes()), before)
        entry = json.loads(self.registry_path.read_text())[AUTO_TASK]
        self.assertEqual(entry["notification_key"], f"{AUTO_TASK}|AUTO-CLOSED|1")
        self.assertNotIn(rr.NOTIFICATION_PENDING_EVENTS_FIELD, entry)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn(" | AUTO-CLOSED | ", self.rows()[0])
        self.assertIn(f"chrono-nudge sent coding/{AUTO_TASK}", messages)
        self.nudge.assert_called_once()

    def test_auto_close_crash_before_ack_is_durable_and_replayable(self):
        registry, _deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=auto_closed_entry())
        self.write_registry(registry)
        with mock.patch.object(rr, "acknowledge_notification_delivery", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                rr.reconcile(AUTO_TASK, dry_run=False)
        rr.reconcile(AUTO_TASK, dry_run=False)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.nudge.call_count, 2)

    def test_older_auto_close_cannot_be_silenced_by_a_new_successful_generation(self):
        registry, deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=auto_closed_entry())
        registry[AUTO_TASK]["delivery_generation"] = 2
        self.assertEqual(rr.deliver_notification_sweep(deliveries, registry, self.now), [True])
        self.nudge.assert_called_once()

    def test_clean_auto_closes_enter_and_trigger_a_digest(self):
        registry, deliveries = {}, []
        for index in range(4):
            entries, events = self.event(f"{AUTO_TASK}-{index}", "AUTO-CLOSED", entry=auto_closed_entry())
            registry.update(entries)
            deliveries.extend(events)
        entries, events = self.event()
        registry.update(entries)
        deliveries.extend(events)
        for sent in (True, False):
            with self.subTest(sent=sent):
                self.nudge.reset_mock()
                self.nudge.return_value = sent
                self.assertEqual(rr.deliver_notification_sweep(deliveries, registry, self.now), [sent] * 5)
                self.assertEqual(len(self.rows()), 5)
                self.nudge.assert_called_once_with("", digest_events=tuple(deliveries))
                self.assertIn("BOARD DIGEST: auto-closed=4, review-required=1", rr.notification_digest(deliveries))

    def test_failure_review_and_coordination_controls_still_nudge(self):
        controls = (
            {"closed_from_status": "blocked", "terminal_receipt_status": "failed"},
            {"terminal_receipt_failure_class": "cli_timeout"},
            {"terminal_receipt_returncode": 1},
            {"terminal_receipt_termination_kind": "timeout"},
            {"terminal_receipt_controller_quarantine": rr.CONTROLLER_QUARANTINE_MARKER},
            {"review_required_by": "claude"},
            {"mandatory_review": "true", "review_model": "claude"},
            {"review_triggers": ["blast_radius"], "review_model": "claude"},
            {"coordination_requested": True, "coordination_request_summary": "Route follow-up"},
        )
        for fields in controls:
            with self.subTest(fields=fields):
                entry = auto_closed_entry()
                entry.update(fields)
                registry, deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=entry)
                self.nudge.reset_mock()
                self.assertEqual(rr.deliver_notification_sweep(deliveries, registry, self.now), [True])
                self.nudge.assert_called_once()

    def test_all_statuses_nudge_with_clean_auto_closed_entry(self):
        statuses = (
            "AUTO-CLOSED", "auto_closed", "blocked", "BLOCKED", "needs_human",
            "NEEDS-HUMAN", "REVIEW-REQUIRED", "needs_review", "complete",
            "work-done-no-envelope", "COORDINATION-REQUESTED", "failed", "",
        )
        for index, status in enumerate(statuses):
            with self.subTest(status=status):
                registry, deliveries = self.event(
                    f"{AUTO_TASK}-{index}", status, entry=auto_closed_entry()
                )
                self.nudge.reset_mock()
                self.assertEqual(rr.deliver_notification_sweep(deliveries, registry, self.now), [True])
                self.nudge.assert_called_once()
        self.assertEqual(len(self.rows()), len(statuses))

    def test_auto_close_nudges_regardless_of_returncode(self):
        for index, code in enumerate(("absent", None, 0)):
            with self.subTest(returncode=code):
                entry = auto_closed_entry()
                if code == "absent":
                    entry.pop("terminal_receipt_returncode")
                else:
                    entry["terminal_receipt_returncode"] = code
                registry, deliveries = self.event(f"{AUTO_TASK}-{index}", "AUTO-CLOSED", entry=entry)
                self.nudge.reset_mock()
                self.assertEqual(rr.deliver_notification_sweep(deliveries, registry, self.now), [True])
                self.nudge.assert_called_once()
        self.assertEqual(len(self.rows()), 3)

    def test_unregistered_auto_close_stays_pending_without_page(self):
        registry, deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=auto_closed_entry())
        with mock.patch.object(rr, "registered_in_canonical_registry", return_value=False):
            self.assertEqual(rr.deliver_notification_sweep(deliveries, registry, self.now), [False])
        self.assertEqual(len(self.rows()), 1)
        self.assertIn(rr.NOTIFICATION_PENDING_EVENTS_FIELD, registry[AUTO_TASK])
        self.nudge.assert_not_called()

    def test_queue_failure_never_acknowledges_clean_completion(self):
        registry, _deliveries = self.event(AUTO_TASK, "AUTO-CLOSED", entry=auto_closed_entry())
        self.write_registry(registry)
        with mock.patch.object(rr, "append_chrono_queue", side_effect=OSError("queue unavailable")):
            with self.assertRaisesRegex(OSError, "queue unavailable"):
                rr.reconcile(AUTO_TASK, dry_run=False)
        entry = json.loads(self.registry_path.read_text())[AUTO_TASK]
        self.assertIn(rr.NOTIFICATION_PENDING_EVENTS_FIELD, entry)
        self.assertNotIn("notification_key", entry)
        self.nudge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
