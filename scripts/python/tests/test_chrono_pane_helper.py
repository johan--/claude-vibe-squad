"""shared/chrono-pane.sh must accept every target spelling its callers use.

Regression for 2026-09-19: the 2026-09-17 revision validated the target only
against `session:window-index.pane`. The focus gate passes the pane id it
inherits (`TMUX_PANE=%N`) and outbox-watcher.sh passes `squad:chrono.0` (window
NAME), so both got "no coordinator" and went silent for two days.

tmux and ps are controlled fixtures, including a coordinator startup record.
No host process-table observations are claimed by these tests.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER = REPO_ROOT / "shared" / "chrono-pane.sh"

# Two panes: %0 is the coordinator window "chrono", %7 sits in a window whose
# name contains a space, which is why the helper separates fields with tabs.
PANES = [
    ("%0", "squad:0.0", "squad:chrono.0"),
    ("%7", "squad:5.0", "squad:my win.0"),
]

FAKE_TMUX = r'''#!/usr/bin/env bash
# Minimal tmux stand-in: answers the two subcommands the helper uses.
# PANE_PIDS is "id=pid,id=pid" from the test.
case "$1" in
    list-panes)
        # $2=-a $3=-F $4=<format>; emit the format with fields substituted.
        fmt="$4"
        while IFS='|' read -r id idx name; do
            line="$fmt"
            line="${line//\#\{pane_id\}/$id}"
            line="${line//\#\{session_name\}:\#\{window_index\}.\#\{pane_index\}/$idx}"
            line="${line//\#\{session_name\}:\#\{window_name\}.\#\{pane_index\}/$name}"
            printf '%s\n' "$line"
        done <<< "$PANE_TABLE"
        ;;
    display-message)
        # $2=-p $3=-t $4=<target> $5=<format>. Only pane ids are honoured; any
        # other spelling mimics real tmux's silent fallback to the active pane,
        # which is exactly what the helper must never rely on.
        target="$4"
        case "$target" in
            %*) ;;
            *) target="%0" ;;
        esac
        pid=""
        for pair in ${PANE_PIDS//,/ }; do
            [[ "${pair%%=*}" == "$target" ]] && pid="${pair#*=}"
        done
        [[ -n "$pid" ]] || exit 1
        case "$5" in
            '#{pane_pid}') printf '%s\n' "$pid" ;;
            '#{pane_current_command}') printf 'fake-cmd\n' ;;
            '#{session_name}') printf 'squad\n' ;;
            *) exit 1 ;;
        esac
        ;;
    *) exit 1 ;;
esac
'''


class ChronoPaneHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="chrono-pane-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake_tmux = self.tmp / "tmux"
        self.fake_tmux.write_text(FAKE_TMUX)
        self.fake_tmux.chmod(self.fake_tmux.stat().st_mode | stat.S_IXUSR)
        # A live process whose executable path ends in "claude": the helper
        # matches on the command line, not on a name tmux reports.
        fake_claude = self.tmp / "claude"
        fake_claude.symlink_to(shutil.which("sleep") or "/bin/sleep")
        self.coordinator = subprocess.Popen([str(fake_claude), "30"])
        self.addCleanup(self.coordinator.wait)
        self.addCleanup(self.coordinator.kill)
        # %7 belongs to a process that is real but is not the coordinator.
        self.bystander = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(self.bystander.wait)
        self.addCleanup(self.bystander.kill)
        self.pidfile = self.tmp / "coordinator.pid"
        self.pidfile.write_text(f"pid {self.coordinator.pid}\nshape pane\ntarget %0\nstart fixture-start\n")
        ps = self.tmp / "ps"
        ps.write_text(f'#!/bin/bash\nif [[ "$1" == -o ]]; then echo fixture-start; else echo "{self.coordinator.pid} 1 S+ /fixture/claude"; fi\n')
        ps.chmod(0o755)

    def _run(self, func: str, target: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["TMUX_BIN"] = str(self.fake_tmux)
        env["PANE_TABLE"] = "\n".join("|".join(p) for p in PANES)
        env["PANE_PIDS"] = f"%0={self.coordinator.pid},%7={self.bystander.pid}"
        env.update(PATH=f"{self.tmp}:{env['PATH']}", CHRONO_COORDINATOR_PIDFILE=str(self.pidfile))
        result = subprocess.run(
            ["bash", "-c", f'source "$0"; {func} "$1"', str(HELPER), target],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode, result.stdout.strip()

    def test_resolves_every_caller_spelling_to_the_pane_id(self) -> None:
        for spelling in ("%0", "squad:0.0", "squad:chrono.0"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self._run("chrono_pane_resolve_id", spelling), (0, "%0"))
        self.assertEqual(self._run("chrono_pane_resolve_id", "squad:my win.0"), (0, "%7"))

    def test_rejects_targets_that_name_no_pane(self) -> None:
        for bogus in ("squad:99.9", "%99", "", "chrono", "squad:chrono"):
            with self.subTest(bogus=bogus):
                _rc, out = self._run("chrono_pane_resolve_id", bogus)
                self.assertEqual(out, "")

    def test_has_coordinator_accepts_all_spellings_for_the_live_pane(self) -> None:
        for spelling in ("%0", "squad:0.0", "squad:chrono.0"):
            with self.subTest(spelling=spelling):
                rc, _ = self._run("chrono_pane_has_coordinator", spelling)
                self.assertEqual(rc, 0)

    def test_has_coordinator_rejects_bogus_and_non_coordinator_panes(self) -> None:
        # Bogus targets must not fall through to the active pane's pid.
        for target in ("squad:99.9", "%99", "", "squad:my win.0", "squad:5.0", "%7"):
            with self.subTest(target=target):
                rc, _ = self._run("chrono_pane_has_coordinator", target)
                self.assertEqual(rc, 1)

    def test_observed_command_never_describes_the_wrong_pane(self) -> None:
        self.assertEqual(self._run("chrono_pane_observed_command", "%0"), (0, "fake-cmd"))
        self.assertEqual(self._run("chrono_pane_observed_command", "squad:chrono.0"), (0, "fake-cmd"))
        self.assertEqual(self._run("chrono_pane_observed_command", "squad:99.9"), (0, "unavailable"))


if __name__ == "__main__":
    unittest.main()
