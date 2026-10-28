"""Recipient, bounded pane text, and receipt-backed digest contracts."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import registry_reconciler as rr


class NotificationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.root / "_state"
        self.state.mkdir()
        for name, value in {
            "VAULT_ROOT": self.root, "STATE_DIR": self.state,
            "REGISTRY_PATH": self.state / "active-tasks.json",
            "CHRONO_QUEUE_PATH": self.state / "chrono-queue.md",
            "CHRONO_NOTIFY_LOCKDIR": self.state / "notify.lockdir",
            "CHRONO_NOTIFY_RECEIPTS_DIR": self.state / "receipts",
        }.items():
            self.stack.enter_context(mock.patch.object(rr, name, value))
        self.stack.enter_context(mock.patch.dict(os.environ, {rr.TEST_ISOLATION_ENV: "0"}))
        self.stack.enter_context(mock.patch.object(rr, "registered_in_canonical_registry", return_value=True))
        self.real_resolver = rr.chrono_pane_resolve_coordinator_id
        self.resolve = self.stack.enter_context(mock.patch.object(rr, "chrono_pane_resolve_coordinator_id", return_value="%0"))
        self.stack.enter_context(mock.patch.object(rr.time, "sleep"))
        self.sends = []
        self.run = self.stack.enter_context(mock.patch.object(rr.subprocess, "run", side_effect=self.tmux))
        self.now = datetime.now(timezone.utc)
        self.registry = {}

    def tmux(self, argv, **kwargs):
        if argv[1] == "send-keys":
            self.sends.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def events(self, statuses, *, age=timedelta(), detail="finished"):
        events = []
        for index, status in enumerate(statuses):
            task = f"TASK-fixture-{index}"
            key = f"{task}|{status}|1"
            event = (status, f"coding/{task}", detail, f"{status}: {task}; {detail}", key, False)
            self.registry[task] = {rr.NOTIFICATION_PENDING_EVENTS_FIELD: {key: {
                "created_at": (self.now - age).isoformat(), "state": status,
            }}}
            events.append(event)
        return events

    def literal_messages(self):
        return [argv[-1] for argv in self.sends if "-l" in argv]

    def test_status_notices_have_one_ascii_tag_and_keep_queue_detail(self):
        for status, token in (("ok", "ok"), ("complete", "complete"),
                              ("blocked", "blocked"), ("REVIEW-REQUIRED", "review-required"),
                              ("needs_human", "needs-human"), ("AUTO-CLOSED", "auto-closed")):
            with self.subTest(status=status):
                detail = f"full detail for {status}"
                expected = f"[vibesquad] {token}: TASK-fixture-0; details: _state/chrono-queue.md."
                self.assertEqual(rr.compact_notice(status, "coding/TASK-fixture-0", detail), expected)
                before = len(self.literal_messages())
                events = self.events([status], detail=detail)
                self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True])
                self.assertEqual(len(self.literal_messages()), before + 1)
                self.assertEqual(self.literal_messages()[-1], expected)
                self.assertTrue(expected.isascii())
                # The full detail remains in the queue, outside the tagged pane text.
                self.assertIn(detail, rr.CHRONO_QUEUE_PATH.read_text())
                self.assertNotIn(detail, self.literal_messages()[-1])

    def test_direct_nudges_tag_once_and_receipt_hash_matches_sent_text(self):
        for index, message in enumerate(("direct notice", "[vibesquad] direct notice")):
            with self.subTest(message=message):
                key = f"direct-{index}"
                self.assertTrue(rr.nudge_chrono(message, key))
                expected = "[vibesquad] direct notice"
                self.assertEqual(self.literal_messages()[-1], expected)
                receipt = json.loads(rr.notification_receipt_path(key).read_text())
                self.assertEqual(receipt["message_sha256"], hashlib.sha256(expected.encode()).hexdigest())
                # Positive send above controls the no-second-send replay assertion.
                before = len(self.sends)
                self.assertTrue(rr.nudge_chrono(message, key))
                self.assertEqual(len(self.sends), before)
        self.assertEqual(len(self.literal_messages()), 2)

    def test_enqueue_is_pending_until_recipient_ack_and_never_uses_transport(self):
        from chrono_state.notifications import NotificationStore
        events = self.events(["complete", "blocked", "needs_human", "REVIEW-REQUIRED"])
        changed, errors = rr.publish_durable_notices(self.registry, events, [], self.now)
        self.assertFalse(changed, "successful enqueue needs no registry retry-state rewrite")
        self.assertEqual(errors, [])
        self.run.assert_not_called()
        store = NotificationStore(self.root)
        notices = store.list_pending()
        self.assertEqual(len(notices), 4)
        self.assertEqual({notice["state"] for notice in notices}, {"pending"})
        store.acknowledge([notices[0]["id"]])
        self.assertEqual(len(store.list_pending()), 3)
        rr.publish_durable_notices(self.registry, events, [], self.now)
        self.assertEqual(len(store.list_pending()), 3)
        self.assertEqual(store.show(notices[0]["id"])["state"], "acknowledged")

    def test_bad_intent_does_not_starve_later_notice_on_repeated_passes(self):
        intent = {"generation": 1, "kind": "complete", "summary": "done",
                  "occurred_at": self.now.isoformat()}
        self.registry = {
            "TASK-bad": {"notice_pending_events": {"bad": {**intent, "occurred_at": "2026-09-27T00:00:00"}}},
            "TASK-good": {"notice_pending_events": {"good": intent}},
        }
        from chrono_state.notifications import NotificationStore
        for _ in range(2):
            _, errors = rr.publish_durable_notices(self.registry, [], [], self.now)
            self.assertEqual(len(errors), 1)
            self.assertNotIn("notice_pending_events", self.registry["TASK-good"])
            self.assertIn("TASK-bad", errors[0])
            self.assertIn("bad", self.registry["TASK-bad"]["notice_pending_events"])
            self.assertEqual(len(NotificationStore(self.root).list_pending()), 1)
        self.registry["TASK-bad"]["notice_pending_events"]["bad"] = intent
        _, errors = rr.publish_durable_notices(self.registry, [], [], self.now)
        self.assertEqual(errors, [])
        self.assertEqual(len(NotificationStore(self.root).list_pending()), 2)

    def test_malformed_store_cannot_crash_reconcile_or_drop_retry_intent(self):
        from chrono_state.notifications import NotificationStore
        store = NotificationStore(self.root)
        store.initialize()
        valid = store.path.read_text()
        outbox = self.root / "departments/coding/outbox"
        outbox.mkdir(parents=True)
        for index, payload in enumerate(([], None, "text", 7, True)):
            with self.subTest(payload=payload):
                task = f"TASK-malformed-{index}"
                store.path.write_text(json.dumps(payload))
                rr.REGISTRY_PATH.write_text(json.dumps({task: {
                    "status": "in-flight", "to_model": "gpt-codex",
                    "dispatched_at": "2020-01-01T00:00:00Z"}}))
                (outbox / f"{task}-response.md").write_text("---\nstatus: complete\n---\n\nDone.\n")
                with mock.patch.object(rr, "RESPONSE_MIN_AGE", timedelta(0)):
                    rr.reconcile(task, False)
                entry = json.loads(rr.REGISTRY_PATH.read_text())[task]
                self.assertEqual(entry["status"], "complete")
                self.assertTrue(entry["notice_pending_events"])
                store.path.write_text(valid)
                rr.reconcile(task, False)
                self.assertNotIn("notice_pending_events", json.loads(rr.REGISTRY_PATH.read_text())[task])
                self.assertEqual(len(store.list_pending()), 1)

    def test_unexpected_store_failure_is_deferred(self):
        from chrono_state.notifications import NotificationStore
        events = self.events(["complete"])
        with mock.patch.object(NotificationStore, "initialize", side_effect=AttributeError("fixture error")):
            changed, errors = rr.publish_durable_notices(self.registry, events, [], self.now)
        self.assertTrue(changed)
        self.assertIn("fixture error", errors[0])
        _, errors = rr.publish_durable_notices(self.registry, [], [], self.now)
        self.assertEqual(errors, [])
        self.assertEqual(len(NotificationStore(self.root).list_pending()), 1)

    def test_same_concrete_pane_for_text_enter_and_receipt(self):
        active = ["%1"]
        def switches_active(argv, **kwargs):
            result = self.tmux(argv, **kwargs)
            if "-l" in argv:
                active[0] = "%9"
            return result
        self.run.side_effect = switches_active
        self.assertTrue(rr.nudge_chrono("test", "recipient"))
        self.assertEqual(active, ["%9"])
        self.assertEqual([argv[argv.index("-t") + 1] for argv in self.sends], ["%0", "%0"])
        self.assertEqual(json.loads(rr.notification_receipt_path("recipient").read_text())["target"], "%0")
        self.resolve.assert_called_once()

    def test_resolver_rejects_successful_bare_window_output(self):
        self.run.side_effect = None
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout="squad:chrono\n")
        self.assertEqual(self.real_resolver(), "")
        self.run.return_value = subprocess.CompletedProcess([], 0, stdout="%12\n")
        self.assertEqual(self.real_resolver(), "%12")

    def test_three_fresh_notices_are_short_and_record_full_details(self):
        detail = "LONG-DETAIL " + " ".join(f"file-{n}.txt" for n in range(73))
        events = self.events(["complete"] * 3, detail=detail)
        self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True] * 3)
        self.assertEqual(len(self.literal_messages()), 3)
        for message in self.literal_messages():
            self.assertLess(len(message), 160)
            self.assertNotIn("file-", message)
            self.assertNotIn("\n", message)
        queue = rr.CHRONO_QUEUE_PATH.read_text()
        self.assertEqual(queue.count(" | complete | "), 3)
        self.assertIn("file-72.txt", queue)

    def test_burst_names_all_action_tasks_and_preserves_each_receipt(self):
        statuses = ["complete", "REVIEW-REQUIRED", "needs_human", "blocked", "complete"]
        events = self.events(statuses)
        self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True] * 5)
        messages = self.literal_messages()
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("[vibesquad] BOARD DIGEST:"))
        self.assertEqual(messages[0].count("[vibesquad]"), 1)
        self.assertEqual(rr.notification_digest(events), messages[0])
        self.assertIn("complete=2", messages[0])
        for index in (1, 2, 3):
            self.assertIn(f"TASK-fixture-{index}", messages[0])
        for index in (0, 4):
            self.assertNotIn(f"TASK-fixture-{index}", messages[0])
        for event in events:
            receipt = rr.notification_receipt_path(rr.notification_event_key(event[1], event[0]))
            self.assertEqual(json.loads(receipt.read_text())["target"], "%0")
        self.assertEqual(rr.CHRONO_QUEUE_PATH.read_text().count(" | coding/TASK-"), 5)

    def test_150_notice_backlog_is_one_digest_with_150_event_receipts(self):
        events = self.events(["complete"] * 147 + ["REVIEW-REQUIRED", "needs_human", "blocked"])
        self.assertTrue(all(rr.deliver_notification_sweep(events, self.registry, self.now)))
        self.assertEqual(len(self.literal_messages()), 1)
        self.assertIn("complete=147", self.literal_messages()[0])
        self.assertEqual(rr.CHRONO_QUEUE_PATH.read_text().count(" | coding/TASK-"), 150)
        self.assertEqual(len(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.sent"))), 151)

    def test_older_than_six_hours_is_digest_only(self):
        events = self.events(["blocked"], age=timedelta(hours=6, seconds=1))
        self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True])
        self.assertTrue(self.literal_messages()[0].startswith("[vibesquad] BOARD DIGEST:"))
        self.assertIn("TASK-fixture-0", self.literal_messages()[0])

    def test_exact_six_hour_boundary_is_individual(self):
        events = self.events(["blocked"], age=timedelta(hours=6))
        rr.deliver_notification_sweep(events, self.registry, self.now)
        self.assertTrue(self.literal_messages()[0].startswith("[vibesquad] blocked:"))

    def test_small_mixed_sweep_keeps_old_notices_only_in_its_digest(self):
        events = self.events(["blocked", "complete", "needs_human"])
        first = self.registry["TASK-fixture-0"][rr.NOTIFICATION_PENDING_EVENTS_FIELD][events[0][4]]
        first["created_at"] = (self.now - timedelta(hours=7)).isoformat()
        rr.deliver_notification_sweep(events, self.registry, self.now)
        messages = self.literal_messages()
        self.assertEqual(len(messages), 3)
        old = [message for message in messages if "TASK-fixture-0" in message]
        self.assertEqual(len(old), 1)
        self.assertTrue(old[0].startswith("[vibesquad] BOARD DIGEST:"))

    def test_coordination_is_visible_without_naming_completed_tasks(self):
        events = self.events(["complete", "coordination-requested", "complete", "COORDINATION_REQUESTED"])
        rr.deliver_notification_sweep(events, self.registry, self.now)
        message = self.literal_messages()[0]
        self.assertIn("coordination-requested: TASK-fixture-1, TASK-fixture-3", message)
        for index in (0, 2):
            self.assertNotIn(f"TASK-fixture-{index}", message)
        self.assertIn("Coordination requested in 2 notice(s)", message)

    def test_coalesced_coordination_names_tasks_but_plain_completions_stay_unnamed(self):
        events = []
        statuses = ["complete", "complete", "blocked", "complete"]
        for index, status in enumerate(statuses):
            task = f"TASK-fixture-{index}"
            entry = self.registry[task] = {}
            if index in (1, 2):
                entry.update(coordination_requested=True, coordination_request_summary="route follow-up")
            self.assertTrue(rr.append_terminal_event(
                events, entry, task, "coding", self.now, status, "finished", f"{status}: {task}"
            ))
        self.assertEqual([event[0] for event in events], statuses)
        deliveries, _changed = rr._prepare_notification_deliveries(self.registry, events, None)
        self.assertEqual(rr.deliver_notification_sweep(deliveries, self.registry, self.now), [True] * 4)
        self.assertEqual(len(self.literal_messages()), 1)
        message = self.literal_messages()[0]
        self.assertIn("blocked=1, complete=3", message)
        self.assertIn("blocked: TASK-fixture-2", message)
        self.assertIn("coordination-requested: TASK-fixture-1, TASK-fixture-2", message)
        for index in (0, 3):
            self.assertNotIn(f"TASK-fixture-{index}", message)
        self.assertIn("Coordination requested in 2 notice(s)", message)
        self.assertIn("route follow-up", rr.CHRONO_QUEUE_PATH.read_text())

    def test_successful_digest_prunes_markers_but_keeps_all_receipts(self):
        events = self.events(["blocked"] * 4)
        self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True] * 4)
        self.assertEqual(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest")), [])
        self.assertEqual(len(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.sent"))), 5)

    def test_failed_digest_keeps_all_events_retryable(self):
        events = self.events(["blocked"] * 4)
        self.resolve.return_value = ""
        self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [False] * 4)
        self.assertEqual(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.sent")), [])
        self.assertEqual(len(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest"))), 4)
        self.assertEqual(rr.CHRONO_QUEUE_PATH.read_text().count(" | coding/TASK-"), 4)
        self.resolve.return_value = "%0"
        retry = [(*event[:5], True) for event in events]
        self.assertEqual(rr.deliver_notification_sweep(retry, self.registry, self.now), [True] * 4)
        self.assertEqual(len(self.literal_messages()), 1)
        self.assertEqual(rr.CHRONO_QUEUE_PATH.read_text().count(" | coding/TASK-"), 4)
        self.assertEqual(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest")), [])

    def test_overlapping_concurrent_digests_deliver_each_member_once(self):
        events = self.events(["blocked"] * 6)
        # Both callers have already selected their overlapping membership; the
        # notifier must recheck inside the lock, not trust that earlier census.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(rr.nudge_chrono, "", digest_events=tuple(group))
                       for group in (events[:4], events[2:])]
            self.assertTrue(all(future.result(timeout=10) for future in futures))
        messages = " ".join(self.literal_messages())
        for index in range(6):
            self.assertEqual(messages.count(f"TASK-fixture-{index}"), 1, messages)

    def test_isolation_flag_queues_burst_without_touching_delivery_receipts(self):
        events = self.events(["blocked"] * 4)
        with mock.patch.dict(os.environ, {rr.TEST_ISOLATION_ENV: "1"}):
            self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [False] * 4)
        self.assertFalse(rr.CHRONO_NOTIFY_RECEIPTS_DIR.exists())
        self.run.assert_not_called()

    def test_digest_commit_recovers_missing_member_receipt_without_resending(self):
        events = self.events(["blocked"] * 4)
        missing = rr.notification_receipt_path(rr.notification_event_key(events[1][1], events[1][0]))
        write = rr.atomic_write
        def fail_member(path, text):
            if path == missing:
                raise OSError("simulated member receipt failure")
            return write(path, text)
        with mock.patch.object(rr, "atomic_write", side_effect=fail_member):
            self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [False] * 4)
        self.assertEqual(len(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest"))), 4)
        # Simulate other tasks already acknowledged: only one pending event is
        # left, below the burst threshold and with no individual receipt.
        retry = [(*events[1][:5], True)]
        self.assertEqual(rr.deliver_notification_sweep(retry, self.registry, self.now), [True])
        self.assertEqual(len(self.literal_messages()), 1)
        self.assertTrue(missing.is_file())
        # Keep the whole digest's recovery map until its last member is repaired.
        self.assertEqual(len(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest"))), 4)
        for index in (2, 3):
            retry = [(*events[index][:5], True)]
            self.assertEqual(rr.deliver_notification_sweep(retry, self.registry, self.now), [True])
        self.assertEqual(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest")), [])
        self.assertEqual(len(self.literal_messages()), 1)

    def test_old_completed_markers_are_pruned_without_touching_incomplete_digest(self):
        receipts = rr.CHRONO_NOTIFY_RECEIPTS_DIR
        receipts.mkdir()
        completed, pending = "digest:" + "a" * 64, "digest:" + "b" * 64
        for key, digest, sent in (("old-1", completed, True), ("old-2", completed, True),
                                  ("pending-1", pending, True), ("pending-2", pending, False)):
            receipt = rr.notification_receipt_path(key)
            receipt.with_suffix(".digest").write_text(digest + "\n")
            if sent:
                receipt.write_text("{}\n")
        self.assertTrue(rr.nudge_chrono("unrelated", "unrelated"))
        self.assertEqual(set(receipts.glob("*.digest")), {
            rr.notification_receipt_path(key).with_suffix(".digest")
            for key in ("pending-1", "pending-2")
        })

    def test_marker_cleanup_failure_does_not_turn_delivery_into_failure(self):
        events = self.events(["blocked"] * 4)
        unlink = Path.unlink
        def fail_marker(path, *args, **kwargs):
            if path.suffix == ".digest":
                raise OSError("simulated cleanup failure")
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, "unlink", fail_marker):
            self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True] * 4)
        self.assertEqual(len(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest"))), 4)
        self.assertTrue(rr.nudge_chrono("unrelated", "unrelated"))
        self.assertEqual(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest")), [])

    def test_malformed_markers_retire_without_touching_a_pending_digest(self):
        receipts = rr.CHRONO_NOTIFY_RECEIPTS_DIR
        receipts.mkdir()
        completed, pending = "digest:" + "a" * 64, "digest:" + "b" * 64
        for key, digest, sent in (("old", completed, True), ("pending-1", pending, True),
                                  ("pending-2", pending, False)):
            receipt = rr.notification_receipt_path(key)
            receipt.with_suffix(".digest").write_text(digest + "\n")
            if sent:
                receipt.write_text("{}\n")
        (receipts / "zz.digest").write_text("digest:" + "c" * 64 + "\n")
        bad_pending_name = receipts / "pending-invalid.digest"
        bad_pending_name.write_text(pending + "\n")
        keep = self.state / "keep"
        keep.write_text("not a marker\n")
        rr.notification_receipt_path("bad-content").with_suffix(".digest").write_text("../keep\n")
        rr.notification_receipt_path("bad-encoding").with_suffix(".digest").write_bytes(b"\xff")
        protected = {
            rr.notification_receipt_path(key).with_suffix(".digest")
            for key in ("pending-1", "pending-2")
        } | {bad_pending_name}
        before = {path: path.read_bytes() for path in protected}

        self.assertTrue(rr.nudge_chrono("unrelated", "unrelated"))
        self.assertEqual(set(receipts.glob("*.digest")), protected)
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        self.assertEqual(keep.read_text(), "not a marker\n")
        rr.notification_receipt_path("pending-2").write_text("{}\n")
        self.assertTrue(rr.nudge_chrono("unrelated", "unrelated"))
        self.assertEqual(list(receipts.glob("*.digest")), [])
        self.assertEqual(keep.read_text(), "not a marker\n")

    def test_receipted_replay_prunes_markers_without_another_nudge(self):
        events = self.events(["blocked"] * 4)
        self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True] * 4)
        # Recreate the state after receipt fanout but before marker cleanup.
        for event in events:
            receipt = rr.notification_receipt_path(rr.notification_event_key(event[1], event[0]))
            receipt.with_suffix(".digest").write_text(json.loads(receipt.read_text())["digest_event_key"] + "\n")
        self.assertEqual(len(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest"))), 4)
        self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True] * 4)
        self.assertEqual(list(rr.CHRONO_NOTIFY_RECEIPTS_DIR.glob("*.digest")), [])
        self.assertEqual(len(self.literal_messages()), 1)

    def test_unregistered_events_do_not_trigger_or_enter_digest(self):
        events = self.events(["complete"] * 4)
        with mock.patch.object(rr, "registered_in_canonical_registry", side_effect=lambda task: task != "TASK-fixture-3"):
            self.assertEqual(rr.deliver_notification_sweep(events, self.registry, self.now), [True, True, True, False])
        self.assertEqual(len(self.literal_messages()), 3)
        self.assertFalse(any("BOARD DIGEST" in message for message in self.literal_messages()))
        self.assertEqual(rr.CHRONO_QUEUE_PATH.read_text().count(" | coding/TASK-"), 4)

    def test_path_lists_are_counted_capped_and_point_to_receipt(self):
        paths = [f"path-{index}" for index in range(73)]
        statement = rr.preserved_work_statement("TASK-X", {}, {
            "evidence_status": "preserved_partial", "evidence_out_of_scope_paths": paths,
            "evidence_worktree_location": "/fixture/attempt",
            "work_recovery_status": "integrated", "work_recovery_commit": "abc123",
            "work_recovery_paths": paths,
        })
        self.assertIn("73 path(s)", statement)
        self.assertIn("path-2", statement)
        self.assertNotIn("path-3", statement)
        self.assertIn("full list: terminal receipt", statement)

    def test_promoted_artifact_notice_drops_repeated_boilerplate(self):
        with mock.patch.object(rr, "promoted_artifact_statement", return_value="PROMOTED ARTIFACT: report.md"):
            statement = rr.preserved_work_statement("TASK-X", {"delivery_attempt_id": "d-1"}, {})
        self.assertEqual(statement, "PROMOTED ARTIFACT: report.md")

    def test_shell_watcher_receipt_remains_a_successful_delivery(self):
        receipt = rr.notification_receipt_path("shell-event")
        receipt.parent.mkdir()
        receipt.write_text("event_key=shell-event\ntarget=%0\n")
        self.assertTrue(rr.nudge_chrono("already delivered", "shell-event"))
        self.run.assert_not_called()

    def test_queue_keeps_task_ids_inside_recovery_refs_and_artifact_paths(self):
        detail = "blocked: TASK-X; read refs/heads/worktree/TASK-X/d-1 and outbox/TASK-X.md"
        queued = rr.notification_queue_detail("coding/TASK-X", "summary", detail)
        self.assertIn("refs/heads/worktree/TASK-X/d-1", queued)
        self.assertIn("outbox/TASK-X.md", queued)

    def test_reconcile_burst_enqueues_every_event_without_recipient_ack(self):
        outbox = self.root / "departments/coding/outbox"
        outbox.mkdir(parents=True)
        entries = {}
        for index in range(4):
            task = f"TASK-burst-{index}"
            entries[task] = {
                "status": "in-flight", "specialist": "systems-engineer",
                "to_model": "gpt-codex", "compatibility_namespace": "coding",
                "delivery_generation": 1, "dispatched_at": "2020-01-01T00:00:00Z",
            }
            (outbox / f"{task}-response.md").write_text("---\nstatus: complete\n---\n\nFixture complete.\n")
        rr.REGISTRY_PATH.write_text(json.dumps(entries))
        with mock.patch.object(rr, "RESPONSE_MIN_AGE", timedelta(0)):
            rr.reconcile(None, dry_run=False)
            rr.reconcile(None, dry_run=False)
        self.assertEqual(len(self.literal_messages()), 1)
        self.assertIn("BOARD DIGEST: complete=4", self.literal_messages()[0])
        for task, entry in json.loads(rr.REGISTRY_PATH.read_text()).items():
            self.assertEqual(entry["notification_key"], f"{task}|complete|1")
            self.assertNotIn(rr.NOTIFICATION_PENDING_EVENTS_FIELD, entry)
        self.assertEqual(rr.CHRONO_QUEUE_PATH.read_text().count(" | coding/TASK-"), 4)
        from chrono_state.notifications import NotificationStore
        store = NotificationStore(self.root)
        pending = store.list_pending()
        self.assertEqual(len(pending), 4)
        self.assertEqual({notice["state"] for notice in pending}, {"pending"})
        store.acknowledge([notice["id"] for notice in pending])
        self.assertEqual(store.list_pending(), [])


class ShellNoticeProducerTests(unittest.TestCase):
    def test_monitor_retries_store_without_repaging_and_new_episode_pages(self):
        import shlex
        from test_board_spawn_stall_alert import _shell_function
        from chrono_state.notifications import NotificationStore
        source_root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = NotificationStore(root)
            store.initialize()
            backup = root / "valid-store.json"
            backup.write_bytes(store.path.read_bytes())
            store.path.write_text("[]")
            shell = "\n".join([
                "set -uo pipefail",
                "now=1800000000; alerts=0; sent_alerts=0; CHRONO_PANE=fixture:chrono",
                "VAULT_ROOT=" + shlex.quote(str(root)),
                "NOTIFICATION_MODULE=" + shlex.quote(str(source_root / "scripts/python/chrono_state/notifications.py")),
                "chrono_pane_resolve_coordinator_id() { echo %7; }",
                "tmux() { echo SEND; }",
                _shell_function((source_root / "bin/squad-monitor.sh").read_text(), "send_alert"),
                'for cycle in 1 2 3; do send_alert "degraded" episode-one || true; done',
                'echo "DEGRADED:$alerts:$sent_alerts"',
                'cp "$VAULT_ROOT/valid-store.json" "$VAULT_ROOT/_state/notifications/store.json"',
                'send_alert "recovered" episode-one',
                'echo "RECOVERED:$alerts:$sent_alerts"',
                'send_alert "new episode" episode-two',
                'echo "NEW:$alerts:$sent_alerts"',
            ])
            run = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("DEGRADED:0:1", run.stdout)
            self.assertIn("RECOVERED:1:1", run.stdout)
            self.assertIn("NEW:2:2", run.stdout)
            self.assertEqual(run.stdout.splitlines().count("SEND"), 4)
            self.assertEqual(len(store.list_pending()), 2)

    def test_watcher_failed_enqueue_retries_without_duplicate_fallback_rows(self):
        import shlex
        from test_board_spawn_stall_alert import _shell_function
        from chrono_state.notifications import NotificationStore
        source_root = Path(__file__).resolve().parents[3]
        source = (source_root / "bin/outbox-watcher.sh").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = NotificationStore(root)
            store.initialize()
            backup = root / "valid-store.json"
            backup.write_bytes(store.path.read_bytes())
            store.path.write_text("[]")
            outbox = root / "departments/coding/outbox"
            outbox.mkdir(parents=True)
            response = outbox / "TASK-fallback-response.md"
            response.write_text("---\nstatus: complete\n---\n\nFallback done.\n")
            binary = root / "bin/registry-reconciler.sh"
            binary.parent.mkdir()
            binary.write_text("#!/bin/bash\necho 'unhandled fixture'\n")
            binary.chmod(0o755)
            functions = ("release_chrono_queue_lock", "response_summary", "append_chrono_queue", "handle_response_path")
            shell = "\n".join([
                "set -uo pipefail",
                "VAULT_ROOT=" + shlex.quote(str(root)),
                'STATE_DIR="$VAULT_ROOT/_state"; NAMESPACE=coding; ALL_NAMESPACES=0',
                'PROCESSED_PATHS="|"; PENDING_PATHS="|"; QUEUED_PATHS="|"',
                "SESSION=fixture; TMUX_BIN=false",
                "NOTIFICATION_MODULE=" + shlex.quote(str(source_root / "scripts/python/chrono_state/notifications.py")),
                "frontmatter_field() { echo complete; }; response_context() { echo fixture; }",
                "legacy_response_ready_for_status() { return 0; }; response_status() { echo complete; }",
                "status_nudge_prefix() { echo DONE; }; autocapture_dispatch() { :; }",
                "task_already_handled() { return 1; }; sync() { :; }",
                *(_shell_function(source, name) for name in functions),
                "response=" + shlex.quote(str(response)),
                'handle_response_path "$response"',
                'handle_response_path "$response"',
                'cp "$VAULT_ROOT/valid-store.json" "$VAULT_ROOT/_state/notifications/store.json"',
                'handle_response_path "$response"',
                'handle_response_path "$response"',
            ])
            run = subprocess.run(["bash", "-c", shell], capture_output=True, text=True, timeout=15)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = (root / "_state/chrono-queue.md").read_text()
            self.assertEqual(rows.count(" | coding/TASK-fallback | "), 1, rows)
            self.assertEqual(len(store.list_pending()), 1)

    def test_monitor_marks_only_durable_episodes_and_counts_no_skipped_sends(self):
        import shlex
        from test_board_spawn_stall_alert import _shell_function
        from chrono_state.notifications import NotificationStore
        root_source = Path(__file__).resolve().parents[3]
        source = (root_source / 'bin/squad-monitor.sh').read_text()
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = root / '_state'
                state.mkdir()
                if unavailable:
                    (state / 'notifications').write_text('not a directory')
                active = root / 'departments/coding/active'
                active.mkdir(parents=True)
                task = active / 'TASK-monitor.md'
                task.write_text('fixture')
                os.utime(task, (1, 1))
                monitor = state / 'monitor'
                monitor.mkdir()
                shell = '\n'.join([
                    'set -uo pipefail',
                    'VAULT_ROOT=' + shlex.quote(str(root)),
                    'STATE_DIR=' + shlex.quote(str(monitor)),
                    'NOTIFICATION_MODULE=' + shlex.quote(str(root_source / 'scripts/python/chrono_state/notifications.py')),
                    'now=1800000000; alerts=0; sent_alerts=0; STALE_THRESHOLD=1800; PARKED_THRESHOLD=300; CHRONO_PANE=isolated:chrono',
                    'chrono_pane_resolve_coordinator_id() { return 1; }',
                    'chrono_pane_observed_command() { echo unavailable; }',
                    'tmux() { echo UNEXPECTED_TRANSPORT; return 99; }',
                    'task_registry_status() { echo blocked; }',
                    'task_parked_cause() { echo blocked; }',
                    'board_spawn_live() { return 1; }',
                    _shell_function(source, 'send_alert'),
                    _shell_function(source, 'detect_stale_active'),
                    'detect_stale_active coding',
                    'detect_stale_active coding',
                    'echo "COUNTS:$alerts:${sent_alerts:-0}"',
                ])
                run = subprocess.run(['bash', '-c', shell], text=True, capture_output=True, timeout=15)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertNotIn('UNEXPECTED_TRANSPORT', run.stdout)
                marker = monitor / 'coding-stale-TASK-monitor.md-alerted'
                self.assertEqual(marker.exists(), not unavailable)
                self.assertIn('COUNTS:0:0' if unavailable else 'COUNTS:1:0', run.stdout)
                if not unavailable:
                    notices = NotificationStore(root).list_pending()
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0]['state'], 'pending')

    def test_disk_warning_enqueues_without_running_cleanup(self):
        import ast
        import types
        from chrono_state.notifications import NotificationStore
        root_source = Path(__file__).resolve().parents[3]
        embedded = (root_source / 'bin/disk-hygiene.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        tree = ast.parse(embedded)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'check_disk')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / '_state'
            state.mkdir()
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
            namespace = dict(Path=Path, os=os, root=root, state=state,
                             disk_report=lambda root: "synthetic disk inventory",
                             shutil=types.SimpleNamespace(disk_usage=lambda path: types.SimpleNamespace(free=1)),
                             warning_floor=15 * 2**30, interval=1800,
                             tmp_root=lambda root: root / 'tmp', chrome_root=lambda root: root / 'chrome',
                             args=types.SimpleNamespace(periodic=True),
                             subprocess=types.SimpleNamespace(run=runner, TimeoutExpired=subprocess.TimeoutExpired),
                             time=types.SimpleNamespace(time=lambda: 1800000000), NotificationStore=NotificationStore)
            exec(compile(ast.Module(body=[function], type_ignores=[]), '<disk-check-only>', 'exec'), namespace)
            store = NotificationStore(root)
            store.initialize()
            valid = store.path.read_text()
            for payload in ([], None, "text", 7, True,
                            {"schema": "chrono-notifications/v1", "notices": {}},
                            {"schema": "chrono-notifications/v1", "cutoff": [], "notices": {}}):
                with self.subTest(payload=payload):
                    store.path.write_text(json.dumps(payload))
                    runner.reset_mock()
                    with mock.patch.dict(os.environ, {'SQUAD_TEST_ISOLATION': '1'}):
                        self.assertEqual(namespace['check_disk'](), 3)
                    self.assertEqual(sum("--notify-once" in call.args[0] for call in runner.call_args_list), 1)
            store.path.write_text(valid)
            runner.reset_mock()
            with mock.patch.dict(os.environ, {'SQUAD_TEST_ISOLATION': '1'}):
                self.assertEqual(namespace['check_disk'](), 3)
            notices = NotificationStore(root).list_pending()
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]['identity'], ['resource', 'disk', 'disk-low:1000000'])
            self.assertEqual(notices[0]['state'], 'pending')
            self.assertEqual(sum("--notify-once" in call.args[0] for call in runner.call_args_list), 1)
            self.assertIn('--notify-once', runner.call_args.args[0])
            self.assertNotIn('--apply', runner.call_args.args[0])

    def test_watcher_without_recipient_enqueues_once_and_does_not_ack(self):
        from test_outbox_watcher_pane_target import WatcherPaneTargetTests
        from chrono_state.notifications import NotificationStore
        fixture = WatcherPaneTargetTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.env['RESOLVE_STATUS'] = '1'
        for _ in range(2):
            result = fixture.notify('missing-recipient', 'pending notice')
            self.assertEqual(result.returncode, 2, result.stderr)
        store = NotificationStore(fixture.root)
        pending = store.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['state'], 'pending')
        self.assertFalse(fixture.receipt('missing-recipient').exists())
        self.assertFalse(fixture.log.exists())
        store.acknowledge([pending[0]['id']])
        self.assertEqual(store.list_pending(), [])


if __name__ == "__main__":
    unittest.main()
