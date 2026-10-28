"""A bare `session:window` target must find the pane the COORDINATOR is in.

Regression for 2026-09-22: `chrono_pane_resolve_id` matched exact pane spellings
only, but two live callers name a window, not a pane -- registry_reconciler.py
defaults CHRONO_TMUX_TARGET to `squad:chrono`, and squad-stop.sh asks about
`${SESSION}:chrono`. Every completion notice was therefore recorded
`chrono-nudge queued-only` (138 of 260 in the dispatch logs) and none reached
the operator's chat.

The fixture mirrors the real window: `squad:chrono` holds TWO panes, .0 running
the coordinator and .1 running the dashboard. That is why selection may never be
"the active pane" -- half the time the active pane is the dashboard, and board
notices would be typed into it.

tmux and ps are controlled fixtures, including coordinator PID/start identity
and foreground status. No real tmux session or host process table is consulted.
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

# id | pane_pid placeholder | session | window_index | window_name | pane_index
# %0 and %1 share the `chrono` window; %7 sits in a window whose name contains a
# space, and %8 in one whose name contains a DOT (which is why the window match
# compares whole identities instead of splitting the target on its last dot).
PANES = [
    ("%0", "COORDINATOR", "squad", "0", "chrono", "0"),
    ("%1", "DASHBOARD", "squad", "0", "chrono", "1"),
    ("%7", "BYSTANDER", "squad", "5", "my win", "0"),
    ("%8", "BYSTANDER", "squad", "6", "my.win", "0"),
]

FAKE_TMUX = r'''#!/usr/bin/env bash
# Minimal tmux stand-in. PANE_TABLE rows are
# "id|pid|session|window_index|window_name|pane_index"; the format string is
# rendered by substituting each #{...} token, so it serves every format the
# helper asks for rather than one hard-coded layout.
render() {
    local fmt="$1" id="$2" pid="$3" sess="$4" widx="$5" wname="$6" pidx="$7"
    fmt="${fmt//\#\{pane_id\}/$id}"
    fmt="${fmt//\#\{pane_pid\}/$pid}"
    fmt="${fmt//\#\{session_name\}/$sess}"
    fmt="${fmt//\#\{window_index\}/$widx}"
    fmt="${fmt//\#\{window_name\}/$wname}"
    fmt="${fmt//\#\{pane_index\}/$pidx}"
    printf '%s\n' "$fmt"
}
case "$1" in
    list-panes)
        # $2=-a $3=-F $4=<format>
        while IFS='|' read -r id pid sess widx wname pidx; do
            [[ -n "$id" ]] || continue
            render "$4" "$id" "$pid" "$sess" "$widx" "$wname" "$pidx"
        done <<< "$PANE_TABLE"
        ;;
    display-message)
        # $2=-p $3=-t $4=<target> $5=<format>. Only pane ids are honoured; any
        # other spelling mimics real tmux's silent fallback to the active pane,
        # which is exactly what the helper must never rely on.
        target="$4"
        case "$target" in
            %*) ;;
            *) target="%1" ;;   # the DASHBOARD pane: a fallback must never pass
        esac
        while IFS='|' read -r id pid sess widx wname pidx; do
            [[ "$id" == "$target" ]] || continue
            case "$5" in
                '#{pane_pid}') printf '%s\n' "$pid"; exit 0 ;;
                '#{pane_current_command}') printf 'cmd-of-%s\n' "${id#%}"; exit 0 ;;
                *) render "$5" "$id" "$pid" "$sess" "$widx" "$wname" "$pidx"; exit 0 ;;
            esac
        done <<< "$PANE_TABLE"
        exit 1
        ;;
    *) exit 1 ;;
esac
'''

# What chrono_pane_has_coordinator did BEFORE this fix: resolve the target with
# the exact-spelling resolver only. Kept here so the defect is reproduced on the
# same fixture that proves the fix, rather than only described in prose.
LEGACY_HAS_COORDINATOR = '''
legacy_has_coordinator() {
    local pane_id pane_pid
    pane_id="$(chrono_pane_resolve_id "$1")" || return 1
    [[ -n "$pane_id" ]] || return 1
    pane_pid="$("$TMUX_BIN" display-message -p -t "$pane_id" '#{pane_pid}' 2>/dev/null)" || return 1
    chrono_pane_pid_runs_coordinator "$pane_pid"
}
'''


class ChronoPaneResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="chrono-pane-resolve-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake_tmux = self.tmp / "tmux"
        self.fake_tmux.write_text(FAKE_TMUX)
        self.fake_tmux.chmod(self.fake_tmux.stat().st_mode | stat.S_IXUSR)

        # A live process whose executable path ends in "claude": the helper
        # matches on the command line, not on a name tmux reports.
        fake_claude = self.tmp / "claude"
        fake_claude.symlink_to(shutil.which("sleep") or "/bin/sleep")
        self.coordinator = self._spawn([str(fake_claude), "30"])
        # The dashboard pane, and two unrelated windows. Real processes, none
        # of them the coordinator.
        self.dashboard = self._spawn(["/bin/sleep", "30"])
        self.bystander = self._spawn(["/bin/sleep", "30"])

        pids = {
            "COORDINATOR": str(self.coordinator.pid),
            "DASHBOARD": str(self.dashboard.pid),
            "BYSTANDER": str(self.bystander.pid),
        }
        self.pane_table = "\n".join(
            "|".join(pids.get(field, field) for field in row) for row in PANES
        )
        self.start = "Tue Sep 22 12:00:00 2026"
        self.pidfile = self.tmp / "coordinator.pid"
        self.pidfile.write_text(f"pid {self.coordinator.pid}\nshape pane\ntarget squad:chrono\nstart {self.start}\n")
        self.process_table = f"{self.coordinator.pid} 1 S+ /fixture/claude\n{self.dashboard.pid} 1 S+ /fixture/dashboard\n"
        ps = self.tmp / "ps"
        ps.write_text('#!/bin/bash\nif [[ "$1" == -o ]]; then printf "%s\\n" "$COORD_START"; else printf "%s\\n" "$PROCESS_TABLE"; fi\n')
        ps.chmod(0o755)

    def _spawn(self, argv: list[str]) -> subprocess.Popen:
        """Kill AND reap: an unwaited child stays a zombie for the whole run,
        and a stale pid is the one thing these liveness checks must not see."""
        proc = subprocess.Popen(argv)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc

    def _run(self, snippet: str, target: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["TMUX_BIN"] = str(self.fake_tmux)
        env["PANE_TABLE"] = self.pane_table
        env.update(PATH=f"{self.tmp}:{env['PATH']}", CHRONO_COORDINATOR_PIDFILE=str(self.pidfile),
                   COORD_START=self.start, PROCESS_TABLE=self.process_table)
        result = subprocess.run(
            ["bash", "-c", f'source "$0"; {snippet} "$1"', str(HELPER), target],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return result.returncode, result.stdout.strip()

    # --- the defect, on this fixture -------------------------------------

    def test_legacy_exact_only_resolution_misses_the_bare_window(self) -> None:
        """Reproduction: what the callers actually pass resolved to nothing."""
        self.assertEqual(self._run("chrono_pane_resolve_id", "squad:chrono")[1], "")
        rc, _ = self._run(LEGACY_HAS_COORDINATOR + "\nlegacy_has_coordinator", "squad:chrono")
        self.assertEqual(rc, 1, "pre-fix behaviour: a live coordinator reported absent")

    # --- the fix ----------------------------------------------------------

    def test_bare_window_resolves_to_the_coordinator_pane(self) -> None:
        for spelling in ("squad:chrono", "squad:0"):
            with self.subTest(spelling=spelling):
                self.assertEqual(
                    self._run("chrono_pane_resolve_coordinator_id", spelling), (0, "%0")
                )
                rc, _ = self._run("chrono_pane_has_coordinator", spelling)
                self.assertEqual(rc, 0)

    def test_window_name_containing_a_dot_is_not_parsed_as_a_pane_request(self) -> None:
        # `squad:my.win` names a window, not pane .win of window `my`.
        self.assertEqual(self._run("chrono_pane_window_panes", "squad:my.win")[1].split("\t")[0], "%8")

    # --- controls ---------------------------------------------------------

    def test_a_window_with_no_coordinator_still_reports_absent(self) -> None:
        """The positive control above only counts if this stays negative: the
        display-message fallback would otherwise hand back the active pane."""
        for target in ("squad:my win", "squad:5", "squad:my.win", "squad:6"):
            with self.subTest(target=target):
                self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", target)[1], "")
                rc, _ = self._run("chrono_pane_has_coordinator", target)
                self.assertEqual(rc, 1)

    def test_targets_naming_nothing_at_all_report_absent(self) -> None:
        for bogus in ("squad:99.9", "squad:99", "%99", "", "chrono", "other:chrono"):
            with self.subTest(bogus=bogus):
                self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", bogus)[1], "")
                rc, _ = self._run("chrono_pane_has_coordinator", bogus)
                self.assertEqual(rc, 1)

    def test_explicit_pane_spellings_are_unchanged(self) -> None:
        # An explicit .1 still means the DASHBOARD pane, and is still not the
        # coordinator -- the window search must not override what was asked for.
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "squad:chrono.1"), (1, ""))
        self.assertEqual(self._run("chrono_pane_has_coordinator", "squad:chrono.1")[0], 1)
        for spelling in ("%0", "squad:0.0", "squad:chrono.0"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", spelling), (0, "%0"))
                self.assertEqual(self._run("chrono_pane_has_coordinator", spelling)[0], 0)

    def test_observed_command_describes_the_pane_the_check_looked_at(self) -> None:
        # Diagnosability: the watcher's skip line must name the coordinator pane
        # for a bare window, not pane %1's command and not the active pane's.
        self.assertEqual(self._run("chrono_pane_observed_command", "squad:chrono"), (0, "cmd-of-0"))
        self.assertEqual(self._run("chrono_pane_observed_command", "squad:chrono.1"), (0, "cmd-of-1"))
        self.assertEqual(self._run("chrono_pane_observed_command", "squad:99.9"), (0, "unavailable"))

    def test_two_claudes_select_recorded_identity_independent_of_order(self):
        self.process_table += f"{self.dashboard.pid} 1 S+ /fixture/claude\n"
        for table in (self.pane_table, "\n".join(reversed(self.pane_table.splitlines()))):
            self.pane_table = table
            self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "squad:chrono"), (0, "%0"))

    def test_no_identity_record_refuses_even_two_claudes(self):
        self.process_table += f"{self.dashboard.pid} 1 S+ /fixture/claude\n"
        self.pidfile.write_text("")
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "squad:chrono"), (1, ""))

    def test_background_coordinator_and_reused_pid_are_refused(self):
        self.process_table = self.process_table.replace(" S+ /fixture/claude", " S /fixture/claude")
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "squad:chrono"), (1, ""))
        self.process_table = self.process_table.replace(" S /fixture/claude", " S+ /fixture/claude")
        self.start = "another start time"
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "squad:chrono"), (1, ""))

    def test_foreground_direct_child_of_pane_is_supported(self):
        self.process_table = f"{self.coordinator.pid} 500 S+ /fixture/claude\n"
        self.pane_table = self.pane_table.replace(f"%0|{self.coordinator.pid}|", "%0|500|")
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "squad:chrono"), (0, "%0"))

    def test_ambiguous_panes_fail_closed(self):
        self.pane_table = self.pane_table.replace(f"%1|{self.dashboard.pid}|", f"%1|{self.coordinator.pid}|")
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "squad:chrono"), (1, ""))

    def test_unrelated_background_claude_child_cannot_validate_a_shell(self):
        # Identity names the actual coordinator, while the requested pane only
        # has a different background worker whose executable happens to match.
        self.process_table += f"99999 {self.dashboard.pid} S /fixture/claude -p worker\n"
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "%1"), (1, ""))
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "%0"), (0, "%0"))

    def test_stale_recorded_target_and_stopped_process_fail_closed(self):
        original = self.pidfile.read_text()
        self.pidfile.write_text(original.replace("target squad:chrono", "target squad:my.win"))
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "%0"), (1, ""))
        self.pidfile.write_text(original)
        self.process_table = self.process_table.replace(" S+ /fixture/claude", " T+ /fixture/claude")
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "%0"), (1, ""))

    def test_unavailable_process_observations_refuse_delivery(self):
        self.process_table = ""
        self.assertEqual(self._run("chrono_pane_resolve_coordinator_id", "squad:chrono"), (1, ""))


if __name__ == "__main__":
    unittest.main()
