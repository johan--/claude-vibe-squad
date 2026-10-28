#!/usr/bin/env python3
"""Entry-point tests for the resume capsule — the wrapper and the read instruction.

The 0210 review rejected library-only coverage twice over: `write_capsule()` preserved
the operator line while `bin/chrono-resume-capsule.sh` overwrote it from a stale
compaction snapshot, and "pull at read" was wired into one launcher branch while the
actual read instruction in `chrono/CLAUDE.md` regenerated nothing. Both defects lived
exactly in the gap between the tested library and the untested entry points, so these
tests go through the real wrapper (subprocess) and the real instruction file. (The
operator line and its whole turn plumbing have since been deleted — audit Rec 3 — so
the first defect can no longer recur; the wrapper tests now pin that absence.)
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "bin" / "chrono-resume-capsule.sh"


class WrapperRegeneratesWithoutAnOperatorTurn(unittest.TestCase):
    """The wrapper must regenerate a valid capsule through the real shell.

    Without a live producer, `## Latest operator instruction` could present
    stale instructions as current. The turn plumbing that fed it —
    `--latest-turn`, `--session-id` and the
    compaction-snapshot recovery behind them — is gone from the wrapper too,
    so those flags are rejected rather than silently accepted. What these
    tests pin is that `bin/chrono-resume-capsule.sh` still runs cleanly in
    both modes and that the dead section stays gone.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # The wrapper derives PYTHONPATH from VAULT_ROOT, so the throwaway root
        # must expose the real scripts tree under itself.
        (self.root / "scripts").symlink_to(ROOT / "scripts")
        self.capsule = self.root / "_state" / "chrono" / "resume.md"

    def run_wrapper(self, *args):
        return subprocess.run(
            ["bash", str(WRAPPER), *args],
            env={**os.environ, "VAULT_ROOT": str(self.root)},
            capture_output=True,
            text=True,
        )

    def test_wrapper_runs_and_writes_a_valid_capsule(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.capsule.read_text()
        self.assertIn("# Chrono resume capsule", text)
        self.assertNotIn("Latest operator instruction", text)

    def test_stdout_mode_renders_without_writing(self):
        # `.claude/commands/vs-queue.md` reads the capsule this way.
        result = self.run_wrapper("--stdout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# Chrono resume capsule", result.stdout)
        self.assertNotIn("Latest operator instruction", result.stdout)
        self.assertFalse(self.capsule.exists(), "--stdout must write nothing")

    def test_removed_turn_flags_are_rejected(self):
        # Positive control for the removal: a flag that is merely ignored would
        # exit 0 and write a capsule; a flag that is gone does neither.
        for flag in ("--latest-turn", "--session-id"):
            with self.subTest(flag=flag):
                result = self.run_wrapper(flag, "AN OPERATOR TURN")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Usage:", result.stderr)
                self.assertFalse(self.capsule.exists())


class SessionStartInstructionCoupling(unittest.TestCase):
    """Reading the capsule and regenerating it must be one inseparable step.

    Regeneration wired into one launcher branch is "refresh in one launcher",
    not "pull at read": a bare coordinator CLI never traverses the launcher. The
    only artifact every Chrono session start does traverse is the Start Of
    Session instruction in chrono/CLAUDE.md, so the read instruction there must
    itself regenerate — non-fatally, because a stale capsule must never block a
    session.
    """

    def step_one(self):
        text = (ROOT / "chrono" / "CLAUDE.md").read_text()
        section = text[text.index("## Start Of Session") :]
        match = re.search(r"\n1\.(.*?)\n2\.", section, re.S)
        self.assertIsNotNone(match, "Start Of Session has no numbered step 1")
        return match.group(1)

    def test_the_read_instruction_regenerates_before_reading(self):
        step = self.step_one()
        self.assertIn("chrono-resume-capsule.sh", step)
        self.assertIn("_state/chrono/resume.md", step)
        self.assertLess(
            step.index("chrono-resume-capsule.sh"),
            step.index("_state/chrono/resume.md"),
            "the regenerate command must come before the read",
        )

    def test_the_regeneration_is_instructed_as_non_fatal(self):
        step = self.step_one().lower()
        self.assertIn("stale", step)
        self.assertTrue(
            "non-fatal" in step or "never block" in step,
            "step 1 must say a failed regeneration cannot block the session",
        )


if __name__ == "__main__":
    unittest.main()
