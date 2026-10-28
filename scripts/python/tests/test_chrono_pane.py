"""Launch flags are independent of the coordinator's required identity record.

Regression for TASK-2026-09-28-0731-061f678a: the live pane existed, but its
coordinator record was missing after a direct resume. All process observations
here are explicit fixtures; these tests do not inspect the host process table.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.python.tests.test_chrono_pane_resolve import FAKE_TMUX


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "shared/chrono-pane.sh"
START = "Mon Sep 28 03:00:00 2026"
RESUME = "claude --dangerously-skip-permissions --resume fixture-session"


class CoordinatorFixture:
    """Shared controlled tmux/process observations for resolver and sender."""

    def __init__(self, root):
        self.root = root
        self.pidfile = root / "coordinator.pid"
        self.ps_calls = root / "ps-calls"
        self.tmux = root / "tmux"
        self.tmux.write_text(FAKE_TMUX)
        self.tmux.chmod(0o755)
        ps = root / "ps"
        ps.write_text(
            '#!/bin/bash\n'
            'printf "%s\\n" "$*" >> "$PS_CALLS"\n'
            'if [[ "$1" == -o ]]; then\n'
            '  printf "%s\\n" "$PROCESS_START"\n'
            'else\n'
            '  printf "%s\\n" "$PROCESS_TABLE"\n'
            'fi\n'
        )
        ps.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{root}:{os.environ['PATH']}",
            "TMUX_BIN": str(self.tmux),
            "CHRONO_COORDINATOR_PIDFILE": str(self.pidfile),
            "PS_CALLS": str(self.ps_calls),
            "PROCESS_START": START,
            "PROCESS_TABLE": f"12345 500 S+ {RESUME}\n600 1 S+ /bin/bash\n",
            "PANE_TABLE": "%0|500|squad|0|chrono|0\n%2|600|squad|0|chrono|1",
        }
        self.register()

    def register(self):
        self.pidfile.write_text(
            f"pid 12345\nshape pane\ntarget squad:chrono\nstart {START}\n"
        )

    def resolve(self, target="squad:chrono"):
        return subprocess.run(
            ["/bin/bash", "-c", 'source "$1"; chrono_pane_resolve_coordinator_id "$2"',
             "pane-test", str(HELPER), target],
            env=self.env, capture_output=True, text=True, timeout=10,
        )


class ChronoPaneResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chrono-resume-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.fixture = CoordinatorFixture(Path(self.temp.name))

    def test_launch_flags_and_native_version_path_preserve_registered_identity(self):
        for command in (
            "claude",
            RESUME,
            "claude --resume fixture-session --dangerously-skip-permissions",
            "/opt/fixture/.local/bin/claude --permission-mode acceptEdits --model opus",
            "/opt/fixture/.local/share/claude/versions/2.1.283 --resume fixture-session",
        ):
            for parent in ("500", "1"):
                with self.subTest(command=command, parent=parent):
                    self.fixture.env["PROCESS_TABLE"] = f"12345 {parent} S+ {command}\n"
                    self.fixture.env["PANE_TABLE"] = (
                        f"%0|{'500' if parent == '500' else '12345'}|squad|0|chrono|0"
                    )
                    result = self.fixture.resolve()
                    self.assertEqual((result.returncode, result.stdout.strip()), (0, "%0"), result.stderr)

    def test_missing_record_is_diagnosed_before_any_command_or_flag_check(self):
        self.fixture.env["CHRONO_COORDINATOR_PIDFILE"] = str(self.fixture.root / "absent.pid")
        result = self.fixture.resolve("%0")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("chrono pane identity unavailable: pane=%0", result.stderr)
        self.assertIn("launch/resume must register", result.stderr)
        self.assertFalse(self.fixture.ps_calls.exists())

        # Positive control changes only the missing identity record.
        self.fixture.env["CHRONO_COORDINATOR_PIDFILE"] = str(self.fixture.pidfile)
        result = self.fixture.resolve("%0")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "%0"), result.stderr)
        self.assertTrue(self.fixture.ps_calls.exists())

    def test_resumed_command_does_not_bypass_stale_or_foreign_identity(self):
        for record in (
            f"pid 12345\nshape pane\ntarget squad:chrono\nstart stale-start\n",
            f"pid 12346\nshape pane\ntarget squad:chrono\nstart {START}\n",
            f"pid 12345\nshape pane\ntarget %2\nstart {START}\n",
            "",
        ):
            with self.subTest(record=record):
                self.fixture.pidfile.write_text(record)
                result = self.fixture.resolve("%0")
                self.assertEqual((result.returncode, result.stdout), (1, ""))

    def test_background_stopped_and_unrelated_processes_stay_ineligible(self):
        for process in (
            f"12345 500 S {RESUME}",
            f"12345 500 T+ {RESUME}",
            f"12345 500 Z+ {RESUME}",
            f"12345 700 S+ {RESUME}",
            "12345 500 S+ /bin/zsh",
            "",
        ):
            with self.subTest(process=process):
                self.fixture.env["PROCESS_TABLE"] = process
                result = self.fixture.resolve("%0")
                self.assertEqual((result.returncode, result.stdout), (1, ""))


if __name__ == "__main__":
    unittest.main()
