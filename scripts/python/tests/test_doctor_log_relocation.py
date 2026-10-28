"""Regression coverage for the durable doctor-log home."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor_fixture  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DOCTOR = ROOT / "bin" / "doctor.sh"
RESOLVER = ROOT / "bin" / "doctor-log-home.sh"


class DoctorLogRelocationTests(unittest.TestCase):
    def test_unwritable_vault_root_still_writes_configured_durable_log(self) -> None:
        with tempfile.TemporaryDirectory(prefix="doctor-log-relocation-") as raw:
            fixture = Path(raw)
            root = fixture / "root"
            doctor_fixture.install_doctor_helpers(ROOT, root)
            home = fixture / "home"
            local_bin = home / ".local" / "bin"
            doctor_fixture.stub_launch_dependencies(local_bin, ROOT)
            doctor_fixture.write_stub(local_bin, "ps", doctor_fixture.EMPTY_PS)
            doctor_fixture.write_stub(local_bin, "curl", "#!/bin/bash\nexit 7\n")
            durable_log_dir = fixture / "off-repo-state" / "doctor-logs"
            environment = dict(os.environ)
            environment["HOME"] = str(home)
            environment["VAULT_ROOT"] = str(root)
            environment["SQUAD_LAUNCHAGENTS_DIR"] = str(home / "Library/LaunchAgents")
            environment["VIBESQUAD_STATUS_DIR"] = str(fixture / "status")
            environment["CHRONO_DOCTOR_LOG_DIR"] = str(durable_log_dir)
            environment.pop("CHRONO_VAULT_ROOT", None)

            # e9b2446bb makes a missing root fatal before doctor starts. An
            # existing but unwritable root still reaches the diagnostic checks
            # and proves their failure reports can be written outside the repo.
            root.chmod(0o555)
            try:
                completed = subprocess.run(
                    ["bash", str(root / "bin/doctor.sh")],
                    cwd=fixture,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            finally:
                root.chmod(0o755)

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            report = durable_log_dir / f"{today}.md"
            summary = durable_log_dir / f"{today}-summary.json"
            diagnostic = completed.stdout + completed.stderr

            self.assertEqual(completed.returncode, 1, diagnostic)
            self.assertTrue(report.is_file(), diagnostic)
            report_text = report.read_text(encoding="utf-8")
            self.assertIn(f"# Doctor Report — {today}", report_text)
            self.assertIn(f"Runtime repository at {root} is not writable", report_text)
            self.assertTrue(summary.is_file(), diagnostic)
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn(f'"date": "{today}"', summary_text)
            self.assertIn('"issue_count": ', summary_text)
            self.assertIn("runtime repository not writable", json.loads(summary_text)["issues"])
            self.assertFalse((root / "_state" / "doctor-logs").exists())

    def test_missing_vault_root_aborts_before_log_validation_or_writes(self) -> None:
        """The fail-closed root contract precedes the log-home contract."""
        with tempfile.TemporaryDirectory(prefix="doctor-log-missing-root-") as raw:
            fixture = Path(raw)
            missing_root = fixture / "missing-vault-root"
            durable_log_dir = fixture / "off-repo-state" / "doctor-logs"
            for log_dir in (str(durable_log_dir), "relative-doctor-logs"):
                with self.subTest(log_dir=log_dir):
                    environment = {
                        **os.environ,
                        "HOME": str(fixture / "home"),
                        "VAULT_ROOT": str(missing_root),
                        "CHRONO_DOCTOR_LOG_DIR": log_dir,
                    }
                    completed = subprocess.run(
                        ["bash", str(DOCTOR)], cwd=fixture, env=environment,
                        capture_output=True, text=True, timeout=5, check=False,
                    )
                    self.assertEqual(completed.returncode, 1, completed.stderr)
                    self.assertIn(
                        f"repo-root.sh: VAULT_ROOT is not a directory: {missing_root}",
                        completed.stderr,
                    )
                    self.assertNotIn("CHRONO_DOCTOR_LOG_DIR", completed.stderr)
                    self.assertEqual(completed.stdout, "")
                    self.assertFalse(missing_root.exists())
                    self.assertFalse(durable_log_dir.exists())
                    self.assertFalse((fixture / "relative-doctor-logs").exists())
                    self.assertFalse((fixture / "home/.local/state").exists())

    def test_default_home_is_absolute_off_repo_and_attempt_independent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="doctor-log-default-") as raw:
            fixture = Path(raw)
            home = fixture / "operator-home"
            home.mkdir()
            environment = dict(os.environ)
            environment["HOME"] = str(home)
            environment["VAULT_ROOT"] = str(ROOT)
            environment.pop("CHRONO_DOCTOR_LOG_DIR", None)
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; printf "%s" "$CHRONO_DOCTOR_LOG_DIR"',
                    "bash",
                    str(RESOLVER),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            expected = home / ".local" / "state" / "chrono-vault" / "doctor-logs"
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(Path(completed.stdout), expected)
            self.assertFalse(Path(completed.stdout).is_relative_to(ROOT))
            self.assertNotIn("_state/board-worktrees", completed.stdout)

    def test_all_known_consumers_use_the_durable_home(self) -> None:
        consumers = (
            "launch-squad.sh",
            "chrono-status-segment.sh",
            "doctor-state.sh",
            "morning-brief.sh",
            "vs-lane-status.sh",
            "where-are-we.sh",
        )
        for name in consumers:
            with self.subTest(name=name):
                text = self._with_sourced_libraries(ROOT / "bin" / name)
                self.assertIn("CHRONO_DOCTOR_LOG_DIR", text)
                self.assertNotIn('${VAULT_ROOT}/_state/doctor-logs', text)

    @staticmethod
    def _with_sourced_libraries(path: Path) -> str:
        """A consumer's own text plus that of the bin/ libraries it sources.

        A consumer satisfies this invariant by reaching the durable home, not by
        naming a variable: bin/chrono-status-segment.sh gets there through
        bin/doctor-state.sh. Following one hop keeps the check honest either way
        -- if the library stopped using CHRONO_DOCTOR_LOG_DIR, every consumer
        that sources it fails here.
        """
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r'source\s+"\$\{VAULT_ROOT\}/bin/([A-Za-z0-9._-]+)"', text):
            library = ROOT / "bin" / name
            if library.is_file():
                text += "\n" + library.read_text(encoding="utf-8")
        return text
        morning_brief = (ROOT / "bin" / "morning-brief.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("../doctor-logs/", morning_brief)

    def test_relative_override_is_rejected_before_any_log_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="doctor-log-relative-") as raw:
            fixture = Path(raw)
            environment = dict(os.environ)
            # Reach doctor's log validation with a root accepted by the shared
            # initializer; the missing-root precedence is pinned separately.
            environment["VAULT_ROOT"] = str(fixture)
            environment["CHRONO_DOCTOR_LOG_DIR"] = "relative-doctor-logs"
            completed = subprocess.run(
                ["bash", str(DOCTOR)],
                cwd=fixture,
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(completed.returncode, 64, completed.stderr)
            self.assertIn("must be absolute", completed.stderr)
            self.assertFalse((fixture / "relative-doctor-logs").exists())

    def test_both_launch_entry_points_export_the_durable_home(self) -> None:
        for name in ("launch-squad.sh", "board-supervisor.sh"):
            with self.subTest(name=name):
                text = (ROOT / "bin" / name).read_text(encoding="utf-8")
                self.assertIn("doctor-log-home.sh", text)
                self.assertIn("export CHRONO_DOCTOR_LOG_DIR", text)


if __name__ == "__main__":
    unittest.main()
