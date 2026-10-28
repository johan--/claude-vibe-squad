"""Checkout diagnostics and monitor regressions; all state is disposable.

Run this module explicitly. No suite discovery or live monitor is needed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import dispatch_checkout as checkout


HEAD = "a" * 40
DIRTY = b" M a.txt\0R  c.txt\0b.txt\0"


@contextlib.contextmanager
def captured_fd2():
    saved = os.dup(2)
    with tempfile.TemporaryFile(mode="w+b") as output:
        os.dup2(output.fileno(), 2)
        try:
            yield output
        finally:
            os.dup2(saved, 2)
            os.close(saved)


class CheckoutWarningTests(unittest.TestCase):
    def setUp(self):
        if hasattr(checkout, "_PENDING_DIAGNOSTICS"):
            self.enterContext(patch.dict(checkout._PENDING_DIAGNOSTICS, clear=True))

    def warn(self, *, status=DIRTY, status_rc=0, head=HEAD, head_rc=0,
             error=None, stream="normal", dead_fd=False):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if "status" in command:
                if error is not None:
                    raise error
                return subprocess.CompletedProcess(command, status_rc, status)
            return subprocess.CompletedProcess(command, head_rc, head)

        redirected = io.StringIO()
        stdout = io.StringIO()
        with captured_fd2() as output, os.fdopen(os.dup(2), "w") as stderr:
            original = stderr
            if stream == "none":
                original = None
            elif stream == "broken":
                original = unittest.mock.Mock()
                original.write.side_effect = OSError("broken stderr")
            with patch.object(checkout.subprocess, "run", side_effect=run), \
                    patch.object(sys, "__stderr__", original), \
                    contextlib.redirect_stderr(redirected), \
                    contextlib.redirect_stdout(stdout), contextlib.ExitStack() as stack:
                if dead_fd:
                    stack.enter_context(patch.object(checkout.os, "write", side_effect=OSError("closed fd")))
                checkout._warn_ignored_changes(Path("/fixture"))
            stderr.flush()
            output.seek(0)
            result = output.read().decode()
        self.assertEqual(redirected.getvalue(), "")
        self.assertEqual(stdout.getvalue(), "")
        return result, calls

    def test_dirty_baseline_counts_both_rename_paths(self):
        message, _ = self.warn()
        self.assertIn(f"TESTING COMMITTED HEAD {HEAD}", message)
        self.assertIn("3 tracked path(s)", message)

    def test_status_rc_is_not_clean(self):
        message, calls = self.warn(status=b"", status_rc=128)
        self.assertIn("git status: ValueError: 'exit 128'", message)
        self.assertIn("NOT a clean-tree measurement", message)
        self.assertEqual(len(calls), 1)

    def test_head_rc_is_not_clean(self):
        message, _ = self.warn(head_rc=128)
        self.assertIn("git rev-parse HEAD: ValueError: 'exit 128'", message)

    def test_git_exception_is_reported(self):
        message, _ = self.warn(error=FileNotFoundError("git"))
        self.assertIn("git status: FileNotFoundError", message)

    def test_malformed_record_is_not_success(self):
        message, _ = self.warn(status=b"XXgarbage-no-space\0")
        self.assertIn("malformed porcelain record", message)
        self.assertNotIn("TESTING COMMITTED HEAD", message)

    def test_unterminated_record_is_reported(self):
        message, _ = self.warn(status=b" M a.txt")
        self.assertIn("unterminated porcelain output", message)

    def test_missing_rename_source_is_reported(self):
        message, _ = self.warn(status=b"R  c.txt\0")
        self.assertIn("missing rename/copy source", message)

    def test_none_stderr_uses_fd2(self):
        message, _ = self.warn(stream="none")
        self.assertIn("stderr failed (ValueError); using fd 2", message)
        self.assertIn("3 tracked path(s)", message)

    def test_broken_stderr_uses_fd2(self):
        message, _ = self.warn(stream="broken")
        self.assertIn("stderr failed (OSError); using fd 2", message)
        self.assertIn("3 tracked path(s)", message)

    def test_status_timeout_is_bounded_and_reported(self):
        message, calls = self.warn(error=subprocess.TimeoutExpired("git status", 10))
        self.assertIn("TimeoutExpired", message)
        self.assertEqual(calls[0][1]["timeout"], 10)

    def test_both_stderr_channels_dead_does_not_raise(self):
        message, _ = self.warn(stream="broken", dead_fd=True)
        self.assertEqual(message, "")
        # Positive control: the same input is observable when fd 2 works.
        message, _ = self.warn(stream="broken")
        self.assertIn("TESTING COMMITTED HEAD", message)

    def test_clean_tree_is_silent_with_dirty_positive_control(self):
        message, calls = self.warn(status=b"")
        self.assertEqual(message, "")
        self.assertEqual(len(calls), 1)
        self.assertIn("TESTING COMMITTED HEAD", self.warn()[0])

    def test_copy_rename_modified_and_unusual_paths(self):
        message, calls = self.warn(status=(
            b"RM renamed\0original\0C  copy\0original\0"
            b" M tab\tname\0 M line\nname\0 M caf\xc3\xa9\0A  added\0D  deleted\0"
        ))
        self.assertIn("8 tracked path(s)", message)
        self.assertIn("-z", calls[0][0])
        self.assertIn("--untracked-files=no", calls[0][0])
        self.assertEqual(calls[1][1]["timeout"], 10)

    def test_invalid_head_is_reported(self):
        self.assertIn("invalid HEAD object id", self.warn(head="no-head")[0])
        self.assertIn("TESTING COMMITTED HEAD", self.warn(head="b" * 64)[0])

    def test_partial_fd_writes_finish_and_zero_write_reports_failure(self):
        real_write = os.write
        with captured_fd2() as output, patch.object(sys, "__stderr__", None):
            with patch.object(checkout.os, "write", side_effect=lambda fd, data: real_write(fd, data[:7])):
                self.assertTrue(checkout._checkout_diagnostic("complete message"))
            output.seek(0)
            self.assertTrue(output.read().endswith(b"complete message\n"))
            with patch.object(checkout.os, "write", return_value=0):
                self.assertFalse(checkout._checkout_diagnostic("undelivered"))

    def test_failed_warning_retried_once_after_stream_recovers(self):
        self.warn(stream="broken", dead_fd=True)
        root = Path("/fixture").resolve()
        pending = checkout._PENDING_DIAGNOSTICS[root]
        with patch.dict(checkout._CACHE, {root: Path("/cached")}, clear=True), \
                patch.object(checkout, "_checkout_diagnostic", return_value=True) as emit, \
                patch.object(checkout.subprocess, "run") as run:
            self.assertEqual(checkout.normal_checkout_root(root), Path("/cached"))
            checkout.normal_checkout_root(root)
            emit.assert_called_once_with(pending)
            run.assert_not_called()
        self.assertNotIn(root, checkout._PENDING_DIAGNOSTICS)

    def test_checkout_critical_path_commands_are_bounded(self):
        def run(command, **kwargs):
            # The fixture repo's git init/add/commit get 60 s with loose-object
            # compression off; every other critical-path command stays at 10 s.
            if command[:3] == ["git", "-c", "core.looseCompression=0"]:
                self.assertEqual(kwargs.get("timeout"), 60, command)
            else:
                self.assertEqual(kwargs.get("timeout"), 10, command)
            value = "/repo/.git/worktrees/task" if "--git-dir" in command else "/repo/.git"
            return subprocess.CompletedProcess(command, 0, value)

        with patch.object(checkout.subprocess, "run", side_effect=run) as invoked, \
                patch.object(checkout, "_warn_ignored_changes"), \
                patch.dict(checkout._CACHE, clear=True), \
                patch.object(checkout, "_TMPDIRS", []):
            checkout.normal_checkout_root(Path("/fixture"))
            self.assertEqual(len(invoked.call_args_list), 7)
            checkout._cleanup()

    def test_detection_timeout_propagates_instead_of_using_wrong_checkout(self):
        with patch.object(checkout.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 10)), \
                patch.dict(checkout._CACHE, clear=True):
            with self.assertRaises(subprocess.TimeoutExpired):
                checkout.normal_checkout_root(Path("/fixture"))
            self.assertEqual(checkout._CACHE, {})

    def test_real_linked_checkout_warns_and_seeds_only_committed_files(self):
        with tempfile.TemporaryDirectory(prefix="checkout-warning-git-") as directory, \
                patch.dict(checkout._CACHE, clear=True), patch.object(checkout, "_TMPDIRS", []):
            root = Path(directory) / "repo"
            root.mkdir()

            def git(*args):
                result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            git("init", "-q")
            (root / "a.txt").write_text("committed\n")
            (root / "b.txt").write_text("rename me\n")
            git("add", ".")
            git("-c", "user.email=fixture@example.invalid", "-c", "user.name=fixture", "commit", "-qm", "fixture")
            revision = git("rev-parse", "HEAD")
            worktree = Path(directory) / "worktree"
            git("worktree", "add", "--detach", str(worktree), "HEAD")
            (worktree / "a.txt").write_text("uncommitted\n")
            result = subprocess.run(["git", "-C", str(worktree), "mv", "b.txt", "c.txt"], capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            (worktree / "untracked.txt").write_text("not part of the warning\n")
            try:
                with captured_fd2() as output, os.fdopen(os.dup(2), "w") as stream, \
                        patch.object(sys, "__stderr__", stream):
                    self.assertEqual(checkout.normal_checkout_root(root), root.resolve())
                    cached = checkout.normal_checkout_root(worktree)
                    self.assertEqual(checkout.normal_checkout_root(worktree), cached)
                    stream.flush()
                    output.seek(0)
                    message = output.read().decode()
                self.assertEqual(message.count("TESTING COMMITTED HEAD"), 1)
                self.assertIn(revision, message)
                self.assertIn("3 tracked path(s)", message)
                self.assertEqual((cached / "a.txt").read_text(), "committed\n")
                self.assertTrue((cached / "b.txt").exists())
                self.assertFalse((cached / "c.txt").exists())
                self.assertFalse((cached / "untracked.txt").exists())
                self.assertFalse(checkout._is_linked_worktree(cached))
            finally:
                checkout._cleanup()


class MonitorThrashTests(unittest.TestCase):
    """Extract complete functions, with no monitor startup or live alert transport."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="monitor-warning-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "_state" / "monitor"
        self.state.mkdir(parents=True)
        self.registry = {}
        self.rows = []
        self.source = (Path(__file__).resolve().parents[3] / "bin/squad-monitor.sh").read_text()

    def put(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def task(self, number, body="same brief\n", *, durable=True, location="archive", namespace="security"):
        task_id = f"TASK-fixture-{number}"
        self.rows.append({"task_id": task_id, "ts": "2027-01-15T07:59:00Z", "source_namespace": namespace})
        packet = f"---\nid: {task_id}\n---\n{body}"
        if durable:
            attempt = f"d-{number}"
            self.registry[task_id] = {"delivery_attempt_id": attempt, "delivery_generation": 2, "status": "complete"}
            self.put(f"_state/board-dispatch/{task_id}.{attempt}.context.json", json.dumps({
                "authority": {"task_id": task_id, "attempt_id": attempt, "generation": 2},
                "task_prompt": f"attempt-specific wrapper {number}\n## Exact task packet\n\n{packet.rstrip()}\n",
            }))
        elif location:
            self.put(f"departments/coding/{location}/{task_id}.md", packet)
        return task_id

    def run_functions(self, names, command):
        functions = []
        for name in names:
            match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", self.source)
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        self.put("_state/active-tasks.json", self.registry if isinstance(self.registry, str) else json.dumps(self.registry))
        self.put("_state/dispatch-log.jsonl", "".join(json.dumps(row) + "\n" for row in self.rows))
        shell = (
            'set -uo pipefail\nVAULT_ROOT="$1"\n'
            'REGISTRY="$VAULT_ROOT/_state/active-tasks.json"\n'
            'STATE_DIR="$2"\n'
            'DISPATCH_LOG="$VAULT_ROOT/_state/dispatch-log.jsonl"\n'
            'now=1800000000\nTHRASH_WINDOW=1800\nCOMPLETED_ACTIVE_THRESHOLD=7200\n'
            'source "$3/shared/lead-windows.sh"\n'
            'send_alert() { printf "ALERT:%s\\n" "$1"; }\n'
            'tmux() { echo "unexpected tmux invocation" >&2; return 99; }\n'
            + "\n".join(functions) + "\n" + command
        )
        result = subprocess.run(["bash", "-c", shell, "--", str(self.root), str(self.state),
                                 str(Path(__file__).resolve().parents[3])], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("unexpected tmux invocation", result.stderr)
        return result.stdout + result.stderr

    def detect(self):
        return self.run_functions(["task_body_hash", "detect_thrash"], "detect_thrash")

    def test_settled_contexts_detect_same_brief_without_packets(self):
        self.task(1)
        self.task(2)
        output = self.detect()
        self.assertIn("security namespace received same task 2x", output)
        self.assertNotIn("NOT_MEASURED", output)
        self.assertNotIn("ALERT:", self.detect())  # same bucket deduplicates

    def test_distinct_bodies_do_not_alert_with_duplicate_positive_control(self):
        self.task(1, "first")
        self.task(2, "second")
        self.assertNotIn("ALERT:", self.detect())
        self.task(3, "first")
        self.assertIn("same task 2x", self.detect())

    def test_all_missing_packets_report_skipped_ids(self):
        self.task(1, durable=False, location=None)
        self.task(2, durable=False, location=None)
        output = self.detect()
        self.assertIn("skipped_task_ids=2 measured_task_ids=0", output)
        self.assertIn("ALERT:NOT_MEASURED thrash security:", output)
        self.assertNotIn("ALERT:", self.detect())
        for row in self.rows:
            row["ts"] = "2027-01-15T08:29:00Z"
        output = self.run_functions(["task_body_hash", "detect_thrash"], 'now=$((now + 1800))\ndetect_thrash')
        self.assertIn("ALERT:NOT_MEASURED thrash security:", output)
        self.rows = []
        output = self.detect()
        self.assertNotIn("ALERT:", output)  # an empty window does not mean blindness

    def test_partial_measurement_keeps_known_duplicate_alert(self):
        self.task(1)
        self.task(2)
        self.task(3, durable=False, location=None)
        output = self.detect()
        self.assertIn("skipped_task_ids=1 measured_task_ids=2", output)
        self.assertIn("same task 2x", output)

    def test_canonical_packet_and_context_bodies_hash_identically(self):
        self.task(1, "same brief\n---\nkeep this body divider\n")
        self.task(2, "same brief\n---\nkeep this body divider\n", durable=False)
        self.assertIn("same task 2x", self.detect())

    def test_each_fence_mismatch_is_unmeasured_even_with_packet_fallback(self):
        self.task(1)
        self.task(2)
        context_path = self.root / "_state/board-dispatch/TASK-fixture-2.d-2.context.json"
        original = json.loads(context_path.read_text())
        self.put("departments/coding/inbox/TASK-fixture-2.md", "---\nid: TASK-fixture-2\n---\nsame brief\n")
        for field, value in (("task_id", "TASK-wrong"), ("attempt_id", "d-wrong"), ("generation", 1), ("generation", True)):
            with self.subTest(field=field, value=value):
                changed = {**original, "authority": {**original["authority"], field: value}}
                context_path.write_text(json.dumps(changed))
                output = self.detect()
                self.assertIn("context identity does not match registry fence", output)
                self.assertIn("skipped_task_ids=1 measured_task_ids=1", output)
                self.assertNotIn("ALERT:", output)
        context_path.write_text(json.dumps(original))
        self.assertIn("same task 2x", self.detect())

    def test_stale_context_not_selected_by_glob(self):
        self.task(1)
        task_id = self.task(2)
        self.registry[task_id]["delivery_attempt_id"] = "d-new"
        self.assertIn("skipped_task_ids=1 measured_task_ids=1", self.detect())

    def test_repeated_id_alerts_without_comparing_historical_bodies(self):
        self.task(1)
        self.rows.append(dict(self.rows[0]))
        output = self.detect()
        self.assertIn("2 dispatches without attempt identity", output)
        self.assertIn("skipped_task_ids=1 measured_task_ids=0", output)
        self.assertIn("ALERT:security namespace received same task 2x in 30m", output)
        self.assertIn("repeated task id TASK-fixture-1", output)
        self.assertNotIn("duplicate body", output)
        self.assertEqual(output.count("ALERT:"), 1)
        self.assertNotIn("ALERT:", self.detect())

    def test_repeated_id_alert_needs_no_packet(self):
        self.task(1, durable=False, location=None)
        self.rows.extend([dict(self.rows[0]), dict(self.rows[0])])
        self.assertIn("ALERT:security namespace received same task 3x", self.detect())

    def test_repeat_and_body_alerts_share_namespace_bucket(self):
        self.task(1)
        self.rows.append(dict(self.rows[0]))
        self.task(2)
        self.task(3)
        output = self.detect()
        self.assertEqual(output.count("ALERT:security namespace received same task"), 1)
        self.assertNotIn("ALERT:", self.detect())

    def test_registry_identity_fallback_is_explicit(self):
        task_id = self.task(1, durable=False)
        for entry in ({}, {"delivery_attempt_id": "d-1", "delivery_generation": "1"},
                      {"delivery_attempt_id": None, "delivery_generation": None}):
            with self.subTest(entry=entry):
                self.registry = {task_id: entry} if entry else {}
                output = self.detect()
                self.assertIn(f"NOT_FENCED thrash {task_id}: registry attempt identity unusable", output)
                self.assertNotIn("NOT_MEASURED", output)
        self.registry = "{"
        output = self.detect()
        self.assertIn("NOT_FENCED", output)
        self.assertNotIn("NOT_MEASURED", output)
        self.registry = {}
        self.task(1)
        self.rows = self.rows[-1:]
        self.assertNotIn("NOT_FENCED", self.detect())

    def test_body_dividers_are_not_discarded(self):
        self.task(1, "same\n---\nend")
        self.task(2, "same\nend", durable=False)
        self.assertNotIn("ALERT:", self.detect())
        self.task(3, "same\n---\nend", durable=False)
        self.assertIn("same task 2x", self.detect())

    def test_empty_or_malformed_context_is_not_clean(self):
        task = self.task(1)
        path = self.root / f"_state/board-dispatch/{task}.d-1.context.json"
        original = path.read_text()
        for bad in ("{", "{}", "null", original.replace("same brief", "")):
            with self.subTest(bad=bad):
                path.write_text(bad)
                self.assertIn("skipped_task_ids=1 measured_task_ids=0", self.detect())

    def test_namespaces_and_time_window_exclude_unrelated_dispatches(self):
        self.task(1)
        self.task(2, namespace="coding")
        self.task(3)
        self.rows[-1]["ts"] = "2027-01-15T06:00:00Z"
        self.assertNotIn("ALERT:", self.detect())
        self.task(4)
        self.assertIn("security namespace received same task 2x", self.detect())

    def test_missing_log_reports_unmeasured(self):
        output = self.run_functions(["task_body_hash", "detect_thrash"], 'DISPATCH_LOG="$VAULT_ROOT/absent"\ndetect_thrash')
        self.assertIn("dispatch log unavailable; skipped_task_ids=unknown", output)

    def test_invalid_timestamp_is_counted_without_blinding_other_namespaces(self):
        self.rows.append({"ts": "invalid", "task_id": "TASK-invalid", "source_namespace": "security"})
        self.task(1, namespace="coding")
        self.task(2, namespace="coding")
        output = self.detect()
        self.assertIn("NOT_MEASURED thrash security: invalid dispatch timestamp; skipped_log_rows=1", output)
        self.assertNotIn("ALERT:NOT_MEASURED", output)
        self.assertIn("ALERT:coding namespace received same task 2x", output)
        self.assertNotIn("dispatch log unreadable", output)

    def test_malformed_relevant_rows_are_counted_and_valid_rows_still_alert(self):
        bad_rows = [{"ts": "2027-01-15T07:59:00Z"}] + [
            {"ts": "2027-01-15T07:59:00Z", "task_id": value}
            for value in (None, "", 12, {}, [], "TASK-bad\nrow", "TASK-bad\n", "TASK-bad\t")
        ] + [{"task_id": "TASK-no-time"}] + [
            {"task_id": "TASK-bad-time", "ts": value} for value in ("invalid", None, 12, {}, [])
        ]
        for index, row in enumerate(bad_rows):
            with self.subTest(row=row):
                self.state = self.root / f"state-{index}"
                self.state.mkdir()
                self.rows = [{**row, "source_namespace": "security"}]
                self.task(1)
                self.task(2)
                output = self.detect()
                self.assertIn("skipped_task_ids=0 measured_task_ids=2 skipped_log_rows=1", output)
                self.assertIn("skipped_log_rows=1", output)
                self.assertIn("ALERT:security namespace received same task 2x", output)
                self.assertNotIn("dispatch log unreadable", output)

    def test_unknown_timestamps_never_arm_recurring_alerts(self):
        for timestamp in (None, "invalid"):
            for elapsed in (0, 1800, 30 * 86400):
                with self.subTest(timestamp=timestamp, elapsed=elapsed):
                    self.state = self.root / f"state-{timestamp}-{elapsed}"
                    self.state.mkdir()
                    self.rows = [{"task_id": "TASK-no-time", "source_namespace": "content"}]
                    if timestamp is not None:
                        self.rows[0]["ts"] = timestamp
                    output = self.run_functions(["task_body_hash", "detect_thrash"],
                                                f"now=$((now + {elapsed}))\ndetect_thrash")
                    self.assertIn("skipped_log_rows=1", output)
                    self.assertNotIn("ALERT:", output)
                    # A valid-time, unresolved task must still signal blindness.
                    self.state = self.root / f"control-{timestamp}-{elapsed}"
                    self.state.mkdir()
                    self.task(1, durable=False, location=None, namespace="content")
                    self.rows[-1]["ts"] = datetime.fromtimestamp(
                        1800000000 + elapsed - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    output = self.run_functions(["task_body_hash", "detect_thrash"],
                                                f"now=$((now + {elapsed}))\ndetect_thrash")
                    self.assertIn("ALERT:NOT_MEASURED thrash content:", output)

    def test_invalid_identity_with_valid_timestamp_still_alerts(self):
        self.rows = [{"ts": "2027-01-15T07:59:00Z", "source_namespace": "content"}]
        output = self.detect()
        self.assertIn("ALERT:NOT_MEASURED thrash content:", output)
        self.assertIn("skipped_task_ids=0 measured_task_ids=0 skipped_log_rows=1", output)

    def test_source_namespaces_come_from_log_with_legacy_fallbacks(self):
        for field, namespace in (("source_namespace", "shared"),
                                 ("source_namespace", "fixture_namespace"),
                                 ("compatibility_namespace", "shared"),
                                 ("to_lead", "shared")):
            with self.subTest(field=field, namespace=namespace):
                self.state = self.root / f"state-{field}-{namespace}"
                self.state.mkdir()
                self.rows = []
                self.task(1, "first", namespace=namespace, durable=False)
                self.task(2, "second", namespace=namespace, durable=False)
                for row in self.rows:
                    row[field] = row.pop("source_namespace")
                    if field == "source_namespace":
                        row["compatibility_namespace"] = "coding"
                self.assertNotIn("ALERT:", self.detect())
                self.rows.append(dict(self.rows[0]))
                output = self.detect()
                self.assertIn(f"ALERT:{namespace} namespace received same task 2x", output)
                self.assertEqual(output.count("ALERT:"), 1)

    def test_shared_duplicate_bodies_use_canonical_mailbox(self):
        self.task(1, namespace="shared", durable=False)
        self.task(2, namespace="shared", durable=False)
        self.assertIn("ALERT:shared namespace received same task 2x", self.detect())

    def test_invalid_namespace_values_do_not_become_state_paths(self):
        for namespace in (None, "", 12, {}, [], "bad/name", "bad\nname", "bad\tname", "a" * 250):
            self.rows.append({"ts": "2027-01-15T07:59:00Z", "source_namespace": namespace})
        self.task(1)
        self.task(2)
        output = self.detect()
        self.assertIn("ALERT:security namespace received same task 2x", output)
        self.assertEqual(output.count("ALERT:"), 1)
        self.assertNotIn("dispatch log unreadable", output)

    def test_namespace_length_boundary_preserves_dedup(self):
        self.task(1, namespace="a" * 65)
        self.rows.append(dict(self.rows[-1]))
        self.assertNotIn("ALERT:", self.detect())
        self.assertEqual(list(self.state.iterdir()), [])
        namespace = "a" * 64
        self.task(2, namespace=namespace)
        self.rows.append(dict(self.rows[-1]))
        output = self.detect()
        self.assertEqual(output.count("ALERT:"), 1)
        self.assertIn(f"ALERT:{namespace} namespace received same task 2x", output)
        self.assertEqual(len(list(self.state.iterdir())), 1)
        self.assertNotIn("ALERT:", self.detect())

    def test_hyphenated_namespace_and_blindness_markers_are_independent(self):
        self.task(1, namespace="coding", durable=False, location=None)
        self.task(2, namespace="coding-thrash-unmeasured")
        self.rows.append(dict(self.rows[-1]))
        output = self.detect()
        self.assertIn("ALERT:NOT_MEASURED thrash coding:", output)
        self.assertIn("ALERT:coding-thrash-unmeasured namespace received same task 2x", output)
        self.assertEqual(output.count("ALERT:"), 2)
        self.assertEqual(len(list(self.state.iterdir())), 2)
        self.assertNotIn("ALERT:", self.detect())

    def test_legacy_markers_allow_one_realert_then_new_markers_dedup(self):
        bucket = 1800000000 // 1800
        legacy = {f"coding-thrash-unmeasured-{bucket}-alerted",
                  f"security-thrash-{bucket}-alerted"}
        for name in legacy:
            (self.state / name).touch()
        self.task(1, namespace="coding", durable=False, location=None)
        self.task(2)
        self.rows.append(dict(self.rows[-1]))
        output = self.detect()
        self.assertIn("ALERT:NOT_MEASURED thrash coding:", output)
        self.assertIn("ALERT:security namespace received same task 2x", output)
        self.assertEqual(output.count("ALERT:"), 2)
        self.assertNotIn("ALERT:", self.detect())
        self.assertEqual({path.name for path in self.state.iterdir()}, legacy | {
            f"coding.thrash-unmeasured-{bucket}-alerted",
            f"security.thrash-{bucket}-alerted",
        })

    def test_old_or_unattributed_malformed_rows_do_not_blind_detection(self):
        self.rows.extend([
            {"ts": "2026-01-01T00:00:00Z", "note": "old row without task id"},
            {"ts": "2026-01-01T00:00:00Z", "source_namespace": "security"},
            {"ts": "2027-01-15T07:59:00Z"},
            None, 7, "not an object", [],
        ])
        self.task(1)
        self.task(2)
        output = self.detect()
        self.assertIn("ALERT:security namespace received same task 2x", output)
        self.assertNotIn("NOT_MEASURED", output)

    def test_invalid_json_log_reports_unreadable(self):
        output = self.run_functions(["task_body_hash", "detect_thrash"],
                                    'printf "{" > "$DISPATCH_LOG"\ndetect_thrash')
        self.assertIn("dispatch log unreadable; skipped_task_ids=unknown", output)

    def test_legacy_inbox_and_active_find_canonical_outbox(self):
        for directory, function in (("inbox", "archive_completed_inbox"), ("active", "auto_archive_completed")):
            with self.subTest(directory=directory):
                task_id = f"TASK-legacy-{directory}"
                packet = self.put(f"departments/research/{directory}/{task_id}.md", "packet\n")
                os.utime(packet, (1_700_000_000, 1_700_000_000))
                self.run_functions([function], f"{function} research")
                self.assertTrue(packet.exists())  # absence of response prevents archive
                self.put(f"departments/coding/outbox/{task_id}-response.md", "response\n")
                self.assertIn("AUTO-ARCHIVED", self.run_functions([function], f"{function} research"))
                self.assertTrue((self.root / f"departments/research/archive/{task_id}.md").exists())
                self.assertFalse(packet.exists())


if __name__ == "__main__":
    unittest.main()
