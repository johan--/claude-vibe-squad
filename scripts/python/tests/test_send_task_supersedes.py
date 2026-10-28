#!/usr/bin/env python3
"""`scripts/send-task.sh --supersedes`: settlement as a dispatch side effect.

A separate manual close can be missed, leaving a superseded needs_human return
open. Settlement is therefore part of the follow-up dispatch:
`--supersedes TASK-ID` (or env `SUPERSEDES=`) closes the
named task as `superseded` via `registry_reconciler.py`, but only AFTER the new
dispatch is admitted — never before, and never on a dry run.

Two layers of coverage:

* ``SupersedeWiringTests`` pin the shell seam by source text, the same way
  ``test_send_task_binding.py``'s ``DispatcherWiringTests`` do (the behavior that
  matters — the close itself — lives in the Python reconciler, exercised below).
* ``FixtureRegistryCloseTests`` run the real reconciler against a seeded
  throwaway registry and prove the close the wrapper invokes actually
  terminalizes the task, with a negative control that an unknown id closes
  nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[3]
SEND_TASK = REPO_ROOT / "scripts" / "send-task.sh"
RECONCILER = REPO_ROOT / "scripts" / "python" / "registry_reconciler.py"


class SupersedeWiringTests(unittest.TestCase):
    """Behavior lives in the reconciler; these assertions pin the shell to it."""

    @classmethod
    def setUpClass(cls):
        cls.source = SEND_TASK.read_text(encoding="utf-8")

    def test_env_form_defaults_the_id_and_the_flag_overrides_it(self):
        self.assertIn('SUPERSEDES_ID="${SUPERSEDES:-}"', self.source)
        self.assertIn("--supersedes)", self.source)

    def test_a_malformed_supersedes_id_is_rejected_before_dispatch(self):
        # The id is interpolated into the close command/reason, so a typo must
        # be caught at the canonical-id guard, not reach the reconciler.
        self.assertIn(
            "ERROR: invalid --supersedes/SUPERSEDES id", self.source
        )

    def test_the_close_uses_the_superseded_status_and_a_sourced_reason(self):
        self.assertIn("--close-status superseded", self.source)
        self.assertIn('--close-reason "superseded by ${TASK_ID}"', self.source)
        self.assertIn("registry_reconciler.py", self.source)

    def test_the_close_is_gated_on_an_id_and_skips_dry_runs(self):
        self.assertIn(
            'if [[ -n "${SUPERSEDES_ID}" && "${DRY_RUN}" != "true" ]]; then',
            self.source,
        )

    def test_the_close_runs_only_after_admission(self):
        dispatch = 'VAULT_ROOT="${VAULT_ROOT}" "${HARDENED_DISPATCH}" "${DISPATCH_ARGS[@]}"'
        close = "--close-status superseded"
        self.assertIn(dispatch, self.source)
        self.assertIn(close, self.source)
        self.assertLess(
            self.source.index(dispatch),
            self.source.index(close),
            "the supersede-close must appear after the hardened dispatch "
            "(admission-first: a failed admission closes nothing)",
        )


class FixtureRegistryCloseTests(unittest.TestCase):
    """Run the real reconciler close the wrapper invokes, against a fixture."""

    PARENT = "TASK-2026-10-27-0001-parent"
    CHILD = "TASK-2026-10-27-0002-child"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.registry = self.root / "_state" / "active-tasks.json"
        self.registry.parent.mkdir(parents=True)

    def _seed(self, payload):
        self.registry.write_text(json.dumps(payload))

    def _close(self, task_id):
        return subprocess.run(
            [
                sys.executable,
                str(RECONCILER),
                "--close-task",
                task_id,
                "--close-status",
                "superseded",
                "--close-reason",
                f"superseded by {self.CHILD}",
            ],
            # CHRONO_CANONICAL_VAULT_ROOT is pinned to the fixture too, so no
            # close can reach into or page the operator's real vault.
            env={
                **os.environ,
                "VAULT_ROOT": str(self.root),
                "CHRONO_CANONICAL_VAULT_ROOT": str(self.root),
            },
            capture_output=True,
            text=True,
        )

    def _entry(self):
        return json.loads(self.registry.read_text())[self.PARENT]

    def test_an_open_task_is_closed_as_superseded_with_the_child_reason(self):
        self._seed(
            {self.PARENT: {"status": "needs_human", "specialist": "planner"}}
        )
        result = self._close(self.PARENT)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self._entry()
        self.assertEqual(entry["status"], "superseded")
        self.assertEqual(
            entry["closure_reason"], f"superseded by {self.CHILD}"
        )
        self.assertEqual(entry["closed_from_status"], "needs_human")

    def test_closing_an_unknown_id_refuses_and_changes_nothing(self):
        # Positive control: the close must be able to FAIL, so a clean run on a
        # real id is meaningful. An unknown id is refused (exit != 0) and the
        # registry is left untouched.
        self._seed(
            {self.PARENT: {"status": "needs_human", "specialist": "planner"}}
        )
        result = self._close("TASK-2026-10-27-9999-ghost")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._entry()["status"], "needs_human")


if __name__ == "__main__":
    unittest.main()
