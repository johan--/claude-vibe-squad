#!/usr/bin/env python3
"""Plan D Task 3: doctor must gate on the commands `squad up` gates on.

README's Quickstart runs ``squad doctor`` immediately before ``squad up``, so
doctor is the documented pre-flight for the launcher's hard dependency gate.
Measured 2026-08-17 against bin/doctor.sh's 1,390 lines: the strings ``fswatch``,
``uv`` and ``curl`` appeared ZERO times, while bin/launch-squad.sh exits 1
without them. A cloner missing fswatch therefore read a green health report and
then could not launch -- the pre-flight passing for the launch it pre-flights.

These tests pin both halves:

  1. every command on the shared list is actually probed, and a missing one is
     an ISSUE (exit 1), not a warning the exit code ignores; and
  2. the launcher and doctor read the SAME list, so it cannot drift back apart.

SAFETY: doctor runs in a throwaway tree with a stubbed HOME. Every dependency,
including tmux, is stubbed on the fixture's own PATH (doctor prepends
``$HOME/.local/bin``), so nothing here reaches the operator's repository, tmux
server, processes or doctor logs. bin/launch-squad.sh is READ, never run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import ast
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.python.tests.ci_host_independence import skip_in_host_independent_ci


sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor_fixture  # noqa: E402

# These tests read/run doctor fixtures, never dispatch a board task. Reading the
# current worktree also makes an uncommitted doctor fix visible to its tests.
ROOT = Path(__file__).resolve().parents[3]


def seed_guard_inputs(repo_root: Path, fixture_root: Path) -> list[str]:
    """Copy the guard plus every input IT declares into a fixture checkout.

    One home for this, because hand-listing broke three separate fixtures in two
    days: each guard input added silently invalidated them until someone chased
    the failure back. The guard's own *_INPUTS tuples are the source of truth.
    Returns the paths actually copied.
    """
    guard = repo_root / "scripts" / "hooks" / "pre-commit"
    (fixture_root / "scripts" / "hooks").mkdir(parents=True, exist_ok=True)
    shutil.copy2(guard, fixture_root / "scripts" / "hooks" / "pre-commit")
    declared = ["scripts/python/validate_release_version.py"]
    for node in ast.parse(guard.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith("_INPUTS"):
                    for element in getattr(node.value, "elts", []):
                        if isinstance(element, ast.Constant) and isinstance(element.value, str):
                            declared.append(element.value)
    copied = []
    for rel in declared:
        src = repo_root / rel
        if not src.is_file():
            continue
        dst = fixture_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied

def install_managed_hook(repo_root: Path, fixture_root: Path, home: Path) -> None:
    """Supply doctor's hook prerequisite in the disposable Git repo only.

    Shared by the six doctor suites: use the real installer and reviewed guard
    so a passing fixture cannot drift from the installation doctor checks.
    """
    # Seed the guard plus every input IT declares, read from the guard's own
    # SNAPSHOT_INPUTS tuple rather than hand-listed here. Hand-listing is why
    # this helper broke three times in two days: each new guard input silently
    # invalidated every fixture until someone chased the failure back here.
    guard = repo_root / "scripts" / "hooks" / "pre-commit"
    hooks_source = fixture_root / "scripts" / "hooks"
    hooks_source.mkdir(parents=True, exist_ok=True)
    shutil.copy2(guard, hooks_source)

    declared: list[str] = ["scripts/python/validate_release_version.py"]
    tree = ast.parse(guard.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.endswith("_INPUTS"):
                for element in getattr(node.value, "elts", []):
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        declared.append(element.value)
    for rel in declared:
        src = repo_root / rel
        if not src.is_file():
            continue  # conditional inputs (moat, typescript) may be absent
        dst = fixture_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    subprocess.run(
        ["/bin/bash", str(repo_root / "docs/install/install-pre-commit-hook.sh")],
        cwd=fixture_root,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


# The negative control cannot be built by DELETING a stub: doctor prepends
# /opt/homebrew/bin to PATH unconditionally, so a masked `fswatch` still
# resolves to the maintainer's real one and the test would pass for the wrong
# reason (measured: all nine "missing" subtests exited 0). Instead the fixture
# gets its OWN required-command list of names no host can supply, one stubbed
# and one not -- which exercises the same loop over a list whose answers this
# test controls completely.
_PROBE_PRESENT = "vs-doctor-probe-present"
_PROBE_ABSENT = "vs-doctor-probe-absent"
_SENTINEL_LIST = f"""#!/usr/bin/env bash
SQUAD_REQUIRED_COMMANDS=({_PROBE_PRESENT} {_PROBE_ABSENT})
SQUAD_REQUIRED_COMMANDS_HINT='sentinel remedy line'
"""


class DoctorLaunchDependencyParityTest(unittest.TestCase):
    def run_doctor(self, *, sentinel_list: bool = False):
        with tempfile.TemporaryDirectory(prefix="doctor-dep-parity-") as temp:
            fixture = Path(temp)
            root = fixture / "root"
            doctor_fixture.install_doctor_helpers(ROOT, root)
            subprocess.run(
                ["git", "init", "-q"], cwd=root, check=True, capture_output=True
            )

            home = fixture / "home"
            install_managed_hook(ROOT, root, home)
            local_bin = home / ".local" / "bin"
            doctor_fixture.write_stub(local_bin, "ps", doctor_fixture.EMPTY_PS)
            doctor_fixture.stub_launch_dependencies(local_bin, ROOT)
            if sentinel_list:
                (root / "shared" / "launch-dependencies.sh").write_text(
                    _SENTINEL_LIST, encoding="utf-8"
                )
                doctor_fixture.write_stub(local_bin, _PROBE_PRESENT)

            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "VAULT_ROOT": str(root),
                "STATE_DIR": str(root / "_state"),
                "VIBESQUAD_STATUS_DIR": str(fixture / "status"),
                "TERM": "dumb",
                "LANG": "C",
                "TMPDIR": str(fixture),
            }
            environment.pop("CHRONO_DOCTOR_LOG_DIR", None)
            environment.pop("CHRONO_VAULT_ROOT", None)

            result = subprocess.run(
                ["/bin/bash", str(root / "bin" / "doctor.sh")],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            log_dir = home / ".local/state/chrono-vault/doctor-logs"
            summaries = sorted(log_dir.glob("*-summary.json"))
            self.assertEqual(
                len(summaries),
                1,
                f"doctor did not emit one summary: {result.stdout}{result.stderr}",
            )
            summary = json.loads(summaries[0].read_text(encoding="utf-8"))
            reports = sorted(log_dir.glob("[0-9]*.md"))
            self.assertEqual(len(reports), 1, "doctor did not emit one report")
            report = reports[0].read_text(encoding="utf-8")
            return result, summary, report

    def test_complete_install_passes_the_dependency_gate(self):
        """The positive control: with every dependency present, no issue.

        Without it, a check that could never fire would satisfy the test below
        for the wrong reason.
        """
        result, summary, report = self.run_doctor()
        self.assertEqual(summary["issues"], [], result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Every command `squad up` requires", report)

    def test_a_missing_dependency_is_a_blocking_issue(self):
        """The cloner's actual situation: one required command is not installed.

        WARN would not do. bin/launch-squad.sh answers this state with exit 1,
        and a pre-flight whose exit code disagrees with the launch it precedes
        is the defect being fixed, not a stylistic choice.
        """
        result, summary, report = self.run_doctor(sentinel_list=True)

        self.assertEqual(
            result.returncode, 1, result.stdout + result.stderr
        )
        self.assertTrue(
            any(
                "missing launch dependencies" in issue and _PROBE_ABSENT in issue
                for issue in summary["issues"]
            ),
            f"the absent command was not reported as an issue: {summary['issues']}",
        )
        # ...and only the absent one. A check that named every entry whenever
        # any entry was missing would be useless to the reader.
        self.assertNotIn(
            _PROBE_PRESENT,
            " ".join(summary["issues"]),
            "a present command was reported missing",
        )
        # The remedy travels with the diagnosis, from the same shared file.
        self.assertIn("squad up` will exit 1", result.stdout)
        self.assertIn("sentinel remedy line", report)

    def test_every_command_on_the_real_list_is_actually_probed(self):
        """A check that probed a SUBSET is how `uv` and `curl` stayed invisible.

        Reads the report line back and requires each shared-list entry by name,
        so adding a dependency to shared/launch-dependencies.sh cannot leave
        doctor silently not looking for it.
        """
        _result, _summary, report = self.run_doctor()
        probed = [
            line for line in report.splitlines() if "Every command `squad up`" in line
        ]
        self.assertEqual(len(probed), 1, report)
        for command in doctor_fixture.launch_dependencies(ROOT):
            with self.subTest(command=command):
                self.assertIn(command, probed[0])

    def test_unreadable_dependency_list_is_unknown_not_healthy(self):
        """Fail closed: an unrunnable gate must never read as nothing-missing."""
        with tempfile.TemporaryDirectory(prefix="doctor-dep-parity-gone-") as temp:
            fixture = Path(temp)
            root = fixture / "root"
            doctor_fixture.install_doctor_helpers(ROOT, root)
            (root / "shared" / "launch-dependencies.sh").unlink()
            subprocess.run(
                ["git", "init", "-q"], cwd=root, check=True, capture_output=True
            )
            home = fixture / "home"
            install_managed_hook(ROOT, root, home)
            local_bin = home / ".local" / "bin"
            doctor_fixture.write_stub(local_bin, "ps", doctor_fixture.EMPTY_PS)
            doctor_fixture.stub_launch_dependencies(local_bin, ROOT)
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "VAULT_ROOT": str(root),
                "STATE_DIR": str(root / "_state"),
                "VIBESQUAD_STATUS_DIR": str(fixture / "status"),
                "TERM": "dumb",
                "LANG": "C",
                "TMPDIR": str(fixture),
            }
            environment.pop("CHRONO_DOCTOR_LOG_DIR", None)
            environment.pop("CHRONO_VAULT_ROOT", None)
            result = subprocess.run(
                ["/bin/bash", str(root / "bin" / "doctor.sh")],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            log_dir = home / ".local/state/chrono-vault/doctor-logs"
            summary = json.loads(
                sorted(log_dir.glob("*-summary.json"))[0].read_text(encoding="utf-8")
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(
                any(
                    "launch dependency parity could not be checked" in entry
                    for entry in summary["gate_unknowns"]
                ),
                summary["gate_unknowns"],
            )
            self.assertEqual(summary["issues"], [])


class LaunchDependencyListHasOneHomeTest(unittest.TestCase):
    """The launcher and doctor must READ the list, never restate it."""

    def test_required_commands_match_the_active_lane_registry(self):
        import csv

        with (ROOT / "model-lanes/lane-capabilities.tsv").open() as stream:
            lane_commands = {row["cli"] for row in csv.DictReader(stream, delimiter="\t")}
        self.assertEqual(lane_commands, {"claude", "codex", "agy", "grok"})
        self.assertEqual(
            set(doctor_fixture.launch_dependencies(ROOT)),
            {"tmux", "fswatch", "jq", "curl", "uv"} | lane_commands,
        )

    def test_launcher_iterates_the_shared_list(self):
        launcher = (ROOT / "bin" / "launch-squad.sh").read_text(encoding="utf-8")
        self.assertIn("shared/launch-dependencies.sh", launcher)
        self.assertIn('for dep in "${SQUAD_REQUIRED_COMMANDS[@]}"', launcher)
        # The literal list the shared file replaced must not linger anywhere.
        self.assertNotIn("for dep in tmux fswatch jq curl", launcher)

    def test_doctor_iterates_the_shared_list(self):
        doctor = (ROOT / "bin" / "doctor.sh").read_text(encoding="utf-8")
        self.assertIn("shared/launch-dependencies.sh", doctor)
        self.assertIn('for _dep in "${SQUAD_REQUIRED_COMMANDS[@]}"', doctor)

    def test_readme_quickstart_names_every_required_command(self):
        """The third reader of this list is a human following the Quickstart."""
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        quickstart = readme.split("## Quickstart", 1)[1][:1200]
        for command in doctor_fixture.launch_dependencies(ROOT):
            if command in ("claude", "codex", "gemini", "grok"):
                continue  # named as model-lane products rather than shell commands
            with self.subTest(command=command):
                self.assertIn(command, quickstart)


@skip_in_host_independent_ci(
    "probes real launchd entry points; launchctl does not exist on the Linux CI runner, "
    "so these measure host infrastructure rather than repository behaviour"
)
class DoctorExternalEntryPointsTest(unittest.TestCase):
    """Exercise the existing external-entry block without running other phases.

    No Git initialization or hook installation is needed for this block. The
    installer fixture supplies the same directory seam and fake launchd state.
    """

    def run_external_entries(self, *, missing_monitor=False, unloaded_monitor=False, launchctl=True):
        sys.path.insert(0, str(ROOT / "tests" / "hooks"))
        from test_install_routines import CHROME, MONITOR, PLIST_TOOL, RoutineFixture

        fixture = RoutineFixture(launchctl=launchctl)
        self.addCleanup(fixture.close)
        # The installer models lint/extract only; doctor also reads a full dump.
        fixture.write_command("plutil", f"#!{sys.executable}\n" + """
import pathlib, plistlib, sys
if sys.argv[1:2] == ["-p"]:
    print(plistlib.loads(pathlib.Path(sys.argv[2]).read_bytes()))
    raise SystemExit(0)
""" + PLIST_TOOL)
        fixture.seed(MONITOR, loaded=not unloaded_monitor)
        fixture.seed(CHROME)
        for script in ("squad-monitor.sh", "chrome-bootstrap.sh"):
            if missing_monitor and script == "squad-monitor.sh":
                continue
            (fixture.root / "bin" / script).write_text("# fixture; never executed\n")
        doctor = (ROOT / "bin" / "doctor.sh").read_text()
        start = doctor.index("# --- Externally-registered entry points")
        end = doctor.index("# DECISION (2026-08-11) on docs/brain-map.md", start)
        block = doctor[start:end]
        # Refuse to run an old copy whose glob would read the operator's agents.
        self.assertIn("${SQUAD_LAUNCHAGENTS_DIR:-", block)
        notes = """set -uo pipefail
GREP_USABLE=true
ISSUES=()
note_ok() { printf 'OK: %s\\n' "$*"; }
note_skip() { printf 'SKIP: %s\\n' "$*"; }
note_unknown() { printf 'UNKNOWN: %s\\n' "$*"; }
note_warn() { printf 'WARN: %s\\n' "$*"; }
note_issue() { printf 'ISSUE: %s\\n' "$*"; }
"""
        counts = '\nprintf "COUNTS: plists=%s scripts=%s missing=%s probed=%s unprobed=%s\\n" "$PLIST_COUNT" "$REGISTERED_COUNT" "$REGISTERED_MISSING" "$LAUNCHD_PROBED" "$LAUNCHD_UNPROBED"\n'
        before = fixture.snapshot(), fixture.state()
        result = subprocess.run(
            ["/bin/bash", "-c", notes + block + counts],
            env=fixture.env, cwd=fixture.root, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((fixture.snapshot(), fixture.state()), before)
        self.assertEqual(fixture.mutations(), [])
        return result, fixture

    def test_existing_block_discovers_and_probes_both_jobs_once(self):
        result, fixture = self.run_external_entries()
        self.assertIn("COUNTS: plists=2 scripts=2 missing=0 probed=2 unprobed=0", result.stdout)
        self.assertIn("all 2 launchd job(s)", result.stdout)
        self.assertEqual(len(fixture.calls()), 2)
        self.assertEqual({call[1].rsplit("/", 1)[-1] for call in fixture.calls()}, {"com.chrono.squad-monitor", "com.vibesquad.chrome"})

    def test_missing_script_positive_control(self):
        result, _fixture = self.run_external_entries(missing_monitor=True)
        self.assertIn("COUNTS: plists=2 scripts=2 missing=1 probed=2 unprobed=0", result.stdout)
        self.assertIn("1/2 launchd-registered script(s) missing", result.stdout)

    def test_unloaded_monitor_positive_control_names_monitor(self):
        result, _fixture = self.run_external_entries(unloaded_monitor=True)
        issues = [line for line in result.stdout.splitlines() if line.startswith("ISSUE:")]
        self.assertEqual(len(issues), 1, result.stdout)
        self.assertIn("com.chrono.squad-monitor", issues[0])
        self.assertNotIn("com.vibesquad.chrome", issues[0])

    def test_missing_launchctl_reports_unmeasured_jobs(self):
        result, _fixture = self.run_external_entries(launchctl=False)
        self.assertIn("COUNTS: plists=2 scripts=2 missing=0 probed=0 unprobed=2", result.stdout)
        self.assertIn("UNKNOWN: 2 launchd job(s)", result.stdout)
        self.assertNotIn("all 2 launchd job(s)", result.stdout)


if __name__ == "__main__":
    unittest.main()
