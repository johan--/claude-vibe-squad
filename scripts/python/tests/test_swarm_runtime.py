"""Hermetic tests for the live mailbox reconciliation sweep."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[3]
RUNTIME_PATH = REPO / "scripts/python/swarm_runtime.py"
spec = importlib.util.spec_from_file_location("swarm_runtime_tested", RUNTIME_PATH)
assert spec and spec.loader
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class SwarmRuntimeTest(unittest.TestCase):
    def test_reconcile_sweep_times_out_hung_process_and_keeps_control(self):
        hung = unittest.mock.MagicMock(pid=999999)
        hung.wait.side_effect = subprocess.TimeoutExpired([], .01)
        with patch.dict(os.environ, {"SQUAD_RECONCILE_SWEEP_TIMEOUT_SECONDS": "0.01"}), \
             patch.object(runtime.subprocess, "Popen", return_value=hung), \
             patch.object(runtime.os, "killpg", side_effect=ProcessLookupError):
            result = runtime.reconcile_once()
        self.assertEqual(result, {"ok": False, "timeout": True})

    def test_reconcile_sweep_timeout_kills_wrapped_grandchild(self):
        # bin/registry-reconciler.sh execs `uv run`, which spawns the real
        # Python reconciler as a grandchild. Killing only the direct child
        # orphaned that grandchild, which then queued on the registry lock;
        # one leaked per sweep cycle (measured 2026-10-04: 47 orphans, board
        # dispatch stalled for an hour).
        with tempfile.TemporaryDirectory(prefix="reconcile-orphan-") as raw:
            root = Path(raw)
            (root / "bin").mkdir()
            pid_file = root / "grandchild.pid"
            script = root / "bin/registry-reconciler.sh"
            script.write_text(
                "#!/bin/bash\n"
                f"sleep 30 & echo $! > {pid_file}\n"
                "wait\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            with patch.object(runtime, "VAULT_ROOT", root), \
                 patch.dict(os.environ, {"SQUAD_RECONCILE_SWEEP_TIMEOUT_SECONDS": "1"}):
                result = runtime.reconcile_once()
            self.assertEqual(result, {"ok": False, "timeout": True})
            grandchild = int(pid_file.read_text(encoding="utf-8"))
            for _ in range(50):
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                import time
                time.sleep(0.1)
            else:
                os.kill(grandchild, 9)
                self.fail("sweep timeout left the wrapped reconciler running")

    def test_reconcile_sweep_settles_preexisting_response_without_watcher_event(self):
        with tempfile.TemporaryDirectory(prefix="reconcile-sweep-") as raw:
            root = Path(raw)
            (root / "_state").mkdir()
            (root / "shared").mkdir()
            (root / "shared/specialist-runtime-map.tsv").write_text(
                "specialist\tc2\tc3\tc4\tc5\tc6\tprimary_lane\n"
                "systems-engineer\tx\tx\tx\tx\tx\tcodex\n", encoding="utf-8"
            )
            task_id = "TASK-sweep-landed"
            entry = {
                "compatibility_namespace": "coding", "source_namespace": "coding",
                "specialist": "systems-engineer", "to_model": "gpt-codex",
                "mandatory_review": "false", "review_model": "none",
                "status": "in-flight",
            }
            (root / "_state/active-tasks.json").write_text(
                json.dumps({task_id: entry}), encoding="utf-8"
            )
            response = root / f"departments/coding/outbox/{task_id}-response.md"
            response.parent.mkdir(parents=True)
            response.write_text(
                "---\n" f"id: {task_id}-response\n" f"in_response_to: {task_id}\n"
                "from: gpt-codex\nto: chrono\ntype: RESULT\nstatus: complete\n---\n\ndone\n",
                encoding="utf-8",
            )
            wrapper = root / "bin/registry-reconciler.sh"
            wrapper.parent.mkdir()
            wrapper.write_text(
                f"#!/bin/bash\nexec '{sys.executable}' '{REPO / 'scripts/python/registry_reconciler.py'}' \"$@\"\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            with patch.object(runtime, "VAULT_ROOT", root), patch.dict(os.environ, {
                "VAULT_ROOT": str(root), "RESPONSE_MIN_AGE_SECONDS": "0",
                "TMUX_BIN": "/nonexistent/tmux", "SQUAD_SESSION": "none",
            }, clear=False):
                result = runtime.reconcile_once()
            final = json.loads((root / "_state/active-tasks.json").read_text())[task_id]
            self.assertTrue(result["ok"], result)
            self.assertEqual(final["status"], "complete")


if __name__ == "__main__":
    unittest.main()
