"""P1/P2 legacy compatibility on a private server; ps is fixture data.

The lane may prohibit process-table access. The identity predicate is covered
separately with positive/negative ps fixtures; this rehearsal measures actual
tmux recipient routing, active-pane changes, queueing, and digest delivery.
This is not a native Claude asyncRewake or editor-draft acceptance canary.
The fixture session is deliberately never named squad.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import registry_reconciler as rr


class IsolatedTmuxRehearsal(unittest.TestCase):
    def test_live_recipient_and_digest_with_controlled_process_observations(self):
        tmux = shutil.which("tmux")
        if tmux is None:
            self.skipTest("tmux executable unavailable")
        with tempfile.TemporaryDirectory(prefix="notice-tmux-", dir="/tmp") as directory, ExitStack() as stack:
            root = Path(directory)
            env = {**os.environ, "TMUX_TMPDIR": str(root), "SQUAD_TEST_ISOLATION": "0"}
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            base = [tmux, "-L", "notice-rehearsal"]

            def call(*args, check=True):
                return subprocess.run(base + list(args), env=env, capture_output=True,
                                      text=True, timeout=10, check=check)

            receiver = root / "claude"
            receiver.symlink_to(shutil.which("cat") or "/bin/cat")
            start = call("new-session", "-d", "-s", "notice-fixture", "-n", "chrono",
                         f"exec {shlex.quote(str(receiver))}", check=False)
            if start.returncode:
                self.skipTest(f"isolated tmux unavailable (rc={start.returncode}): {start.stderr.strip()}")
            stack.callback(lambda: call("kill-server", check=False))
            pane = call("display-message", "-p", "-t", "notice-fixture:chrono.0", "#{pane_id}").stdout.strip()
            pid = call("display-message", "-p", "-t", pane, "#{pane_pid}").stdout.strip()
            other = call("split-window", "-h", "-P", "-F", "#{pane_id}", "-t", pane, "exec /bin/cat").stdout.strip()
            call("select-pane", "-t", other)

            wrapper = root / "tmux"
            wrapper.write_text(f'#!/bin/bash\nexec {shlex.quote(tmux)} -L notice-rehearsal "$@"\n')
            wrapper.chmod(0o755)
            ps = root / "ps"
            ps.write_text(f'#!/bin/bash\nif [[ "$1" == -o ]]; then echo fixture-start; else echo "{pid} 1 S+ {receiver}"; fi\n')
            ps.chmod(0o755)
            pidfile = root / "coordinator.pid"
            pidfile.write_text(f"pid {pid}\nshape pane\ntarget notice-fixture:chrono\nstart fixture-start\n")
            env.update(PATH=f"{root}:{env['PATH']}", CHRONO_COORDINATOR_PIDFILE=str(pidfile))
            stack.enter_context(mock.patch.dict(os.environ, env))
            for name, value in {
                "VAULT_ROOT": root, "TMUX_BIN": str(wrapper), "SQUAD_SESSION": "notice-fixture",
                "CHRONO_TMUX_TARGET": "notice-fixture:chrono", "CHRONO_QUEUE_PATH": root / "queue.md",
                "CHRONO_NOTIFY_LOCKDIR": root / "notify.lockdir",
                "CHRONO_NOTIFY_RECEIPTS_DIR": root / "receipts",
            }.items():
                stack.enter_context(mock.patch.object(rr, name, value))
            stack.enter_context(mock.patch.object(rr, "registered_in_canonical_registry", return_value=True))
            self.assertEqual(rr.chrono_pane_resolve_coordinator_id(), pane)
            sleep = time.sleep

            def switch_active(_delay):
                call("select-pane", "-t", other)
                sleep(0.02)

            stack.enter_context(mock.patch.object(rr.time, "sleep", side_effect=switch_active))
            self.assertTrue(rr.nudge_chrono("RECIPIENT-CONTROL", "recipient-control"))
            events = [(status, f"coding/TASK-live-{i}", "detail", "detail", None, False)
                      for i, status in enumerate(("complete", "blocked", "needs_human", "REVIEW-REQUIRED"))]
            self.assertEqual(rr.deliver_notification_sweep(events, {}, datetime.now(timezone.utc)), [True] * 4)
            sleep(0.1)
            received = call("capture-pane", "-p", "-t", pane).stdout
            bystander = call("capture-pane", "-p", "-t", other).stdout
            self.assertIn("RECIPIENT-CONTROL", received)
            self.assertIn("BOARD DIGEST", received)
            self.assertNotIn("RECIPIENT-CONTROL", bystander)
            self.assertNotIn("BOARD DIGEST", bystander)
            for receipt in (root / "receipts").glob("*.sent"):
                self.assertEqual(json.loads(receipt.read_text())["target"], pane)
            self.assertEqual((root / "queue.md").read_text().count(" | coding/TASK-"), 4)


if __name__ == "__main__":
    unittest.main()
