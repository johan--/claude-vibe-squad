#!/usr/bin/env python3
"""Cancel must reliably terminate a lane's CLI process tree.

`bin/vs-cancel-spawn.sh` is a thin wrapper: it verifies the base branch, then
execs `scripts/python/board_process_truth.py cancel`, whose
`terminate_attributable_tree()` is the code that actually signals the worker.

The reported failure: cancel said "cancelled" for TASK-2026-09-27-1351-a6666978
but the `codex exec` process (PID 46380) kept running as an orphan reparented to
init for ten minutes, and the FIRST cancel attempt refused with "process
identity changed before signal". Both symptoms have one cause: the worker CLI
changed its own PROCESS GROUP after launch (codex re-groups / detaches). The
identity fence recorded the launch-time group, so at cancel time
`_signal_identity()` sees a different group, treats the live worker as a changed
identity, and refuses to signal it -- leaving it to reparent to init.

The original expected-failure test from TASK-2026-09-27-1440-bb7348f9 alters a
recorded PGID while observing an owned process. It now passes. The tree test
also exercises a real os.setpgid transition between observation and SIGSTOP,
then checks TERM/KILL delivery, descendant death, and an unrelated survivor.
All live fixtures are owned by these tests and have bounded cleanup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


WORKTREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKTREE / "scripts" / "python"))
import board_process_truth as bpt  # noqa: E402


class VsCancelWrapperTest(unittest.TestCase):
    """The wrapper refuses ambiguous invocations rather than guessing a PID."""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/bash", str(WORKTREE / "bin" / "vs-cancel-spawn.sh"), *args],
            cwd=str(WORKTREE),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_no_arguments_prints_usage_and_exits_2(self):
        result = self._run()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("usage: vs-cancel-spawn.sh", result.stderr)

    def test_reap_without_a_log_path_is_refused(self):
        # `--reap` alone (no log path) must not be mistaken for a cancel target.
        result = self._run("--reap")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("usage: vs-cancel-spawn.sh", result.stderr)


class ProcessGroupDriftCancellationTest(unittest.TestCase):
    """A live worker that only changed its process GROUP must stay cancellable."""

    def setUp(self):
        self.pid = os.fork()
        if self.pid == 0:
            # Child: isolate into our own session so no group signal can ever
            # reach the test runner, arm a self-destruct in case teardown is
            # skipped, and wait to be killed. Never return into unittest.
            try:
                os.setsid()
                signal.alarm(30)
                while True:
                    signal.pause()
            except BaseException:
                pass
            finally:
                os._exit(0)
        self.addCleanup(self._reap_child)
        # Parent: wait until the child has SETTLED as its own session leader
        # (pgid == pid, i.e. os.setsid() has taken effect), then snapshot its
        # identity. Snapshotting earlier races the child's setsid() and captures
        # the transient pre-detach group.
        deadline = time.monotonic() + 5
        live = None
        while time.monotonic() < deadline:
            observed = bpt.observe_process(self.pid)
            if observed is not None and observed["pgid"] == self.pid:
                live = observed
                break
            time.sleep(0.02)
        self.assertIsNotNone(live, "forked child never became its own session leader")
        self.live = live

    def _reap_child(self):
        for reap in (lambda: os.kill(self.pid, signal.SIGKILL), lambda: os.waitpid(self.pid, 0)):
            try:
                reap()
            except OSError:
                pass

    def test_signal_succeeds_when_identity_matches(self):
        """Positive control: the exact recorded identity is signalled."""
        with mock.patch.object(bpt.os, "kill") as killed:
            result = bpt._signal_identity(
                self.live, signal.SIGCONT, target="tree_root", operation="freeze"
            )
        self.assertTrue(result)
        killed.assert_called_once_with(self.live["pid"], signal.SIGCONT)

    def test_signal_refuses_a_reused_pid_even_if_the_process_group_drifted(self):
        """A new start token still refuses every signal to the live PID slot."""
        recorded = dict(self.live)
        recorded["pgid"] = self.live["pgid"] + 1
        recorded["process_start_token"] = "not-this-process"
        with mock.patch.object(bpt.os, "kill") as killed:
            with self.assertRaises(bpt.ProcessTruthError) as caught:
                bpt._signal_identity(
                    recorded, signal.SIGCONT, target="tree_root", operation="freeze"
                )
            self.assertIn(
                "process identity changed before signal", str(caught.exception)
            )
            self.assertIn("process_start_token", str(caught.exception))
            killed.assert_not_called()

    def test_regrouped_worker_should_stay_cancellable(self):
        """Same pid, start token, and argv with a stale PGID stays signalable."""
        recorded = dict(self.live)
        recorded["pgid"] = self.live["pgid"] + 1
        with mock.patch.object(bpt.os, "kill") as killed:
            self.assertTrue(
                bpt._signal_identity(
                    recorded, signal.SIGCONT, target="tree_root", operation="freeze"
                )
            )
        killed.assert_called_once_with(self.pid, signal.SIGCONT)


class ProcessTreeCancellationTest(unittest.TestCase):
    def _grace_regression(self, mode):
        # Reproduce the review's TERM-handler fork/exec with owned, bounded
        # Python fixtures. The peer ignores TERM in both cases; the root exits.
        worker_program = textwrap.dedent("""
            import os, signal, sys
            from pathlib import Path
            signal.alarm(30)
            def finish(*_):
                if sys.argv[1] == 'exec':
                    os.execl('/bin/sleep', 'sleep', '30')
                child = os.fork()
                if child == 0:
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    signal.alarm(30)
                else:
                    Path(sys.argv[2]).write_text(str(child))
            signal.signal(signal.SIGTERM, finish)
            print('ready', flush=True)
            while True:
                signal.pause()
        """)
        peer_program = (
            "import signal; signal.alarm(30); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); signal.pause()"
        )
        root_program = textwrap.dedent("""
            import json, signal, subprocess, sys
            signal.alarm(30)
            children = [
                subprocess.Popen([sys.executable, '-c', sys.argv[1], *sys.argv[3:]],
                                 stdout=subprocess.PIPE, text=True),
                subprocess.Popen([sys.executable, '-c', sys.argv[2]],
                                 stdout=subprocess.PIPE, text=True),
            ]
            for child in children:
                assert child.stdout.readline().strip() == 'ready'
            print(json.dumps([child.pid for child in children]), flush=True)
            while True:
                signal.pause()
        """)
        with tempfile.TemporaryDirectory() as directory:
            late_pid_path = Path(directory) / "late-child.pid"
            root = subprocess.Popen(
                [sys.executable, "-c", root_program, worker_program, peer_program,
                 mode, str(late_pid_path)],
                start_new_session=True, stdout=subprocess.PIPE, text=True,
            )
            descendants = []
            sibling = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
            try:
                ready, _, _ = select.select([root.stdout], [], [], 5)
                self.assertTrue(ready, "grace fixture never reported readiness")
                descendants = json.loads(root.stdout.readline())
                worker, peer = descendants
                before = {pid: bpt.observe_process(pid)
                          for pid in (root.pid, worker, peer, sibling.pid)}
                self.assertTrue(all(before.values()), "positive control: all fixtures live")
                base = Path(directory) / "TASK-grace.d-grace"
                descriptor = {
                    "schema": bpt.DESCRIPTOR_V2,
                    "task_id": "TASK-grace", "attempt_id": "d-grace", "generation": 1,
                    "created_at": bpt.utc_now(), **before[root.pid],
                    **{key + "_path": str(base) + suffix for key, suffix in (
                        ("context", ".context.json"), ("log", ".log"),
                        ("receipt", ".receipt.json"),
                    )},
                }
                dispatch = Path(str(base) + ".dispatch.json")
                dispatch.write_text(json.dumps(descriptor))
                Path(descriptor["context_path"]).write_text(json.dumps({"authority": {
                    key: descriptor[key] for key in bpt.ATTEMPT_IDENTITY_FIELDS
                }}))
                real_signal = bpt._signal_identity
                delivered = []

                def record_signal(recorded, signum, **kwargs):
                    result = real_signal(recorded, signum, **kwargs)
                    if result:
                        delivered.append((recorded["pid"], signum))
                    return result

                with mock.patch.object(bpt, "_signal_identity", side_effect=record_signal):
                    if mode == "exec":
                        with self.assertRaisesRegex(bpt.ProcessTruthError, "identity changed"):
                            bpt.cancel_attempt(dispatch, grace=1)
                    else:
                        bpt.cancel_attempt(dispatch, grace=1)

                root.wait(timeout=3)
                self.assertIsNone(bpt.observe_process(peer), "TERM-ignoring peer survived")
                self.assertIn((peer, signal.SIGKILL), delivered)
                self.assertIsNone(sibling.poll(), "unrelated sibling was signalled")
                self.assertNotIn(sibling.pid, [pid for pid, _sig in delivered])
                if mode == "exec":
                    after = bpt.observe_process(worker)
                    self.assertIsNotNone(after, "argv fence must preserve the exec'd member")
                    self.assertEqual(before[worker]["process_start_token"],
                                     after["process_start_token"])
                    self.assertNotEqual(before[worker]["argv_sha256"], after["argv_sha256"])
                    self.assertNotIn((worker, signal.SIGKILL), delivered)
                    self.assertFalse(Path(descriptor["receipt_path"]).exists(),
                                     "survivor must prevent a cancelled receipt")
                else:
                    self.assertTrue(late_pid_path.exists(), "TERM handler must actually fork")
                    late = int(late_pid_path.read_text())
                    self.assertIn((late, signal.SIGKILL), delivered,
                                  "positive control: late child observed alive and signalled")
                    for pid in (worker, late):
                        self.assertIsNone(bpt.observe_process(pid), f"owned member survived: {pid}")
                    self.assertEqual(bpt.terminal_outcome(
                        bpt.load_json(descriptor["receipt_path"]), descriptor
                    ), "cancelled")
            finally:
                # Capture the late child's PID even when cancellation fails.
                if late_pid_path.exists():
                    descendants.append(int(late_pid_path.read_text()))
                for pid in descendants:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for child in (root, sibling):
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=3)
                root.stdout.close()

    def test_child_forked_during_term_grace_is_killed_before_receipt(self):
        self._grace_regression("fork")

    def test_exec_during_grace_does_not_abort_peer_kill_or_claim_success(self):
        self._grace_regression("exec")

    def test_regrouped_worker_and_children_die_but_sibling_survives(self):
        # The descriptor's supervisor remains a session leader. Its worker can
        # change groups; one grandchild also escapes into a separate session.
        sleeper = (
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "signal.alarm(30); print('ready', flush=True); signal.pause()"
        )
        worker_program = textwrap.dedent("""
            import json, os, signal, subprocess, sys
            os.setpgid(0, 0)
            signal.alarm(30)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            supervisor_group = os.getppid()
            signal.signal(signal.SIGUSR1, lambda *_: os.setpgid(0, supervisor_group))
            children = [subprocess.Popen([sys.executable, '-c', sys.argv[1]],
                        stdout=subprocess.PIPE, text=True, start_new_session=escaped)
                        for escaped in (False, True)]
            for child in children:
                assert child.stdout.readline().strip() == 'ready'
            print(json.dumps([os.getpid(), *(child.pid for child in children)]), flush=True)
            while True:
                signal.pause()
        """)
        root_program = textwrap.dedent("""
            import signal, subprocess, sys
            signal.alarm(30)
            worker = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],
                                      stdout=subprocess.PIPE, text=True)
            print(worker.stdout.readline().strip(), flush=True)
            worker.wait()
        """)
        root = subprocess.Popen(
            [sys.executable, "-c", root_program, worker_program, sleeper],
            start_new_session=True, stdout=subprocess.PIPE, text=True,
        )
        sibling = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        descendants = []
        try:
            ready, _, _ = select.select([root.stdout], [], [], 5)
            self.assertTrue(ready, "owned fixture did not report its children")
            descendants = json.loads(root.stdout.readline())
            worker = descendants[0]
            self.assertEqual(os.getpgid(worker), worker)
            self.assertEqual(os.getsid(descendants[2]), descendants[2])
            for pid in [root.pid, *descendants, sibling.pid]:
                self.assertIsNotNone(bpt.observe_process(pid), f"positive control: {pid}")
            identity = bpt.observe_process(root.pid)
            signals = []
            drifted = False
            real_signal = bpt._signal_identity

            def regroup_before_signal(expected, signum, **kwargs):
                nonlocal drifted
                if expected["pid"] == worker and signum == signal.SIGSTOP and not drifted:
                    self.assertEqual(expected["pgid"], worker)
                    os.kill(worker, signal.SIGUSR1)
                    deadline = time.monotonic() + 2
                    while os.getpgid(worker) != root.pid and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(os.getpgid(worker), root.pid)
                    drifted = True
                result = real_signal(expected, signum, **kwargs)
                if result:
                    signals.append((expected["pid"], signum, time.monotonic()))
                return result

            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory) / "TASK-regroup.d-regroup"
                descriptor = {
                    "schema": bpt.DESCRIPTOR_V2,
                    "task_id": "TASK-regroup", "attempt_id": "d-regroup", "generation": 1,
                    "created_at": bpt.utc_now(), **identity,
                    **{key + "_path": str(base) + suffix for key, suffix in (
                        ("context", ".context.json"), ("log", ".log"), ("receipt", ".receipt.json")
                    )},
                }
                dispatch = Path(str(base) + ".dispatch.json")
                dispatch.write_text(json.dumps(descriptor))
                Path(descriptor["context_path"]).write_text(json.dumps({"authority": {
                    key: descriptor[key] for key in bpt.ATTEMPT_IDENTITY_FIELDS
                }}))
                with mock.patch.object(bpt, "_signal_identity", side_effect=regroup_before_signal):
                    bpt.cancel_attempt(dispatch, grace=0.15)
                self.assertEqual(bpt.terminal_outcome(
                    bpt.load_json(descriptor["receipt_path"]), descriptor
                ), "cancelled")
            root.wait(timeout=3)
            self.assertTrue(drifted)
            for pid in descendants:
                self.assertIsNone(bpt.observe_process(pid), f"owned descendant survived: {pid}")
                delivered = {sig: at for target, sig, at in signals if target == pid}
                self.assertIn(signal.SIGTERM, delivered)
                self.assertIn(signal.SIGCONT, delivered)
                self.assertIn(signal.SIGKILL, delivered)
                self.assertGreaterEqual(delivered[signal.SIGKILL] - delivered[signal.SIGCONT], 0.15)
            self.assertIsNone(sibling.poll(), "unrelated sibling was signalled")
            self.assertNotIn(sibling.pid, [pid for pid, _sig, _at in signals])
        finally:
            # Only captured fixture PIDs; never sweep the shared host by name.
            for pid in descendants:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for child in (root, sibling):
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
            root.stdout.close()

    def test_term_handler_runs_before_kill_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "term-handled"
            program = textwrap.dedent("""
                from pathlib import Path
                import signal, sys
                def finish(*_):
                    Path(sys.argv[1]).write_text('TERM handled')
                    sys.exit(0)
                signal.signal(signal.SIGTERM, finish)
                signal.alarm(30)
                print('ready', flush=True)
                while True:
                    signal.pause()
            """)
            child = subprocess.Popen(
                [sys.executable, "-c", program, str(marker)],
                start_new_session=True, stdout=subprocess.PIPE, text=True,
            )
            try:
                ready, _, _ = select.select([child.stdout], [], [], 5)
                self.assertTrue(ready)
                self.assertEqual(child.stdout.readline().strip(), "ready")
                bpt.terminate_attributable_tree(bpt.observe_process(child.pid), grace=1)
                self.assertEqual(child.wait(timeout=3), 0)
                self.assertEqual(marker.read_text(), "TERM handled")
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
                child.stdout.close()


if __name__ == "__main__":
    unittest.main()
