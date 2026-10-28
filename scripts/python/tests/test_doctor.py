#!/usr/bin/env python3
"""Doctor warning fixes (TASK-2026-09-27-1440-bb7348f9).

Three standing warnings were misdiagnoses, not real breakage:

  1. The dispatch-rail PATH cross-check compared ``command -v <lane-name>`` to
     the lane's authority path. The gemini lane dispatches through ``agy`` (root
     CLAUDE.md: the standalone ``gemini`` binary is retired), so it compared a
     leftover homebrew ``gemini`` against the ``agy`` authority and "disagreed"
     forever. The fix cross-checks the basename of the authority path.
  2. The MCP-registration count folded in the OPTIONAL Trail-of-Bits
     context-protector wrapper -- a ~704 MB checkout bootstrap-mcps.sh cannot
     install -- so it told the operator to run a script that can never clear it.
     The fix scoped the count to the CLI-registration rows. The wrapper and its
     guarded MCPs were retired on 2026-10-27, so --status now prints only those
     rows and doctor no longer mentions the wrapper.
  3. The auto-capture warning GUESSED "unauthenticated or unreachable gemini",
     which is one of several failure shapes. The fix surfaces the dominant
     reason the failure log actually recorded (e.g. a distiller timeout).

SAFETY: every test runs a real bin/doctor.sh inside a throwaway HOME whose
``.local/bin`` doctor prepends to PATH, so every lane CLI, ``python3``, and MCP
CLI is a stub this test wrote. Nothing here touches the operator's repository,
processes, MCP registrations, or doctor logs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_checkout import normal_checkout_root  # noqa: E402
import doctor_fixture  # noqa: E402

ROOT = normal_checkout_root(Path(__file__).resolve().parents[3])

# Answers the two python3 calls doctor's lane-inventory gate reaches: the
# effective-UID account home (so HOME == that home and the block runs at all)
# and dispatch_context_builder.py lane-inventory (from an env var). The
# vault-root probe -- told apart by the chrono-vault PYTHONPATH doctor sets for
# it -- must keep failing, because this fixture configures no vault. Mirrors the
# stub in test_doctor_auth_probes.py; doctor_fixture (its natural home) is
# outside this task's write scope, so it is restated here with this note.
PYTHON3_INVENTORY_STUB = r"""#!/bin/bash
for argument in "$@"; do
    case "$argument" in
        *dispatch_context_builder.py)
            printf '%s\n' "${DOCTOR_TEST_LANE_INVENTORY:-}"
            exit 0
            ;;
    esac
done
case "${PYTHONPATH:-}" in
    *chrono-vault*) exit 1 ;;
esac
printf '%s\n' "${HOME}"
exit 0
"""

# A CLI that answers `mcp list` with every chrono MCP registered, so
# bootstrap-mcps.sh --status reports a fully-registered lane.
MCP_LIST_CLI = """#!/bin/bash
if [[ "$1" == "mcp" && "$2" == "list" ]]; then
    printf 'chrono-vault\\nchrono-obsidian\\nchrono-research-arsenal\\nchrono-media-studio\\n'
    exit 0
fi
printf 'mcp-list stub 0.0.0\\n'
exit 0
"""


class DoctorRunner(unittest.TestCase):
    """Runs a real bin/doctor.sh in a throwaway tree with a stubbed HOME."""

    def run_doctor(self, *, setup=None, env=None):
        with tempfile.TemporaryDirectory(prefix="doctor-warnfix-") as temp:
            fixture = Path(temp)
            root = fixture / "root"
            doctor_fixture.install_doctor_helpers(ROOT, root)
            subprocess.run(
                ["git", "init", "-q"], cwd=root, check=True, capture_output=True
            )

            home = fixture / "home"
            local_bin = home / ".local" / "bin"
            doctor_fixture.write_stub(local_bin, "ps", doctor_fixture.EMPTY_PS)
            doctor_fixture.stub_launch_dependencies(local_bin, ROOT)

            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "VAULT_ROOT": str(root),
                "TERM": "dumb",
                "LANG": "C",
                "TMPDIR": str(fixture),
            }
            for leak in ("CHRONO_DOCTOR_LOG_DIR", "CHRONO_VAULT_ROOT", "GEMINI_API_KEY"):
                environment.pop(leak, None)
            if setup is not None:
                setup(root, home, local_bin, environment)
            environment.update(env or {})

            result = subprocess.run(
                ["/bin/bash", str(root / "bin" / "doctor.sh")],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
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
            return result, summary, reports[0].read_text(encoding="utf-8")

    @staticmethod
    def _inventory_row(lane: str, binary: Path, *, installed: str = "true") -> str:
        return "\t".join(
            [lane, installed, str(binary), str(binary), "1.0.0", "subscription", ""]
        )


class GeminiAgyPathCrossCheckTest(DoctorRunner):
    """CLAUDE.md rule: the gemini lane dispatches through agy, not `gemini`."""

    def test_gemini_lane_via_agy_does_not_warn(self):
        """agy IS the gemini lane's authority, and a stray `gemini` on PATH must
        not be treated as the lane's binary."""

        def setup(_root, home, local_bin, environment):
            doctor_fixture.write_stub(local_bin, "python3", PYTHON3_INVENTORY_STUB)
            # A retired `gemini` binary ALSO sits on PATH, at a different path
            # than agy -- the exact trap the old check fell into.
            doctor_fixture.write_stub(local_bin, "gemini")
            agy = home / ".local" / "bin" / "agy"
            environment["DOCTOR_TEST_LANE_INVENTORY"] = self._inventory_row(
                "gemini", agy
            )

        _result, summary, report = self.run_doctor(setup=setup)
        joined = " ".join(summary["warnings"])
        self.assertNotIn("dispatch authority path disagree", joined)
        self.assertNotIn("dispatch authority sees a CLI this HOME does not", joined)
        # Positive control: the check RAN and PASSED, rather than being skipped.
        self.assertIn("PATH and dispatch authority agree on every lane", report)

    def test_real_path_disagreement_still_warns(self):
        """The fix must not blind the check: a genuine mismatch still warns."""

        def setup(_root, _home, local_bin, environment):
            doctor_fixture.write_stub(local_bin, "python3", PYTHON3_INVENTORY_STUB)
            # codex is stubbed on PATH by the fixture, but the authority points
            # at another account's install -- a real disagreement.
            bogus = Path("/opt/another-account/bin/codex")
            environment["DOCTOR_TEST_LANE_INVENTORY"] = self._inventory_row(
                "codex", bogus
            )

        _result, summary, _report = self.run_doctor(setup=setup)
        self.assertTrue(
            any("dispatch authority path disagree" in w for w in summary["warnings"]),
            summary["warnings"],
        )


class McpRegistrationCountTest(DoctorRunner):
    """Full CLI registration reads clean, and no guarded-MCP section returns."""

    def _install_bootstrap(self, root: Path, local_bin: Path) -> None:
        target = root / "scripts"
        target.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copy2(
            ROOT / "scripts" / "bootstrap-mcps.sh", target / "bootstrap-mcps.sh"
        )
        for cli in ("codex", "agy"):
            doctor_fixture.write_stub(local_bin, cli, MCP_LIST_CLI)

    def test_full_registration_is_clean_and_names_no_guarded_mcps(self):
        def setup(root, _home, local_bin, _environment):
            self._install_bootstrap(root, local_bin)

        _result, summary, report = self.run_doctor(setup=setup)
        self.assertFalse(
            any("MCP registrations missing" in w for w in summary["warnings"]),
            summary["warnings"],
        )
        # All CLI registrations are present, so the count is clean...
        self.assertIn("MCPs registered", report)
        # ...and the retired guarded MCPs are neither reported nor sent to an
        # install procedure. A returning guarded section fails here.
        self.assertFalse(
            any("guarded" in s.lower() for s in summary["skipped"]),
            summary["skipped"],
        )
        self.assertNotIn("mcp-context-protector", report)


class AutoCaptureReasonTest(DoctorRunner):
    """The warning must report the recorded reason, not guess at auth."""

    def _install_memory_metrics(self, root: Path) -> None:
        import shutil

        target = root / "scripts" / "python"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            ROOT / "scripts" / "python" / "memory_metrics.py",
            target / "memory_metrics.py",
        )

    def _write_failures(self, root: Path, reason: str, count: int) -> None:
        from datetime import datetime, timezone

        state = root / "_state"
        state.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            json.dumps(
                {
                    "at": stamp,
                    "reason": reason,
                    "response_path": f"_state/episodic/x{i}.md",
                    "schema_version": 1,
                }
            )
            for i in range(count)
        ]
        (state / "autocapture-failures.jsonl").write_text(
            "\n".join(rows) + "\n", encoding="utf-8"
        )

    def test_dominant_reason_is_surfaced_not_an_auth_guess(self):
        reason = "distillation_failed:distiller timed out after 120s"

        def setup(root, _home, _local_bin, _environment):
            self._install_memory_metrics(root)
            self._write_failures(root, reason, count=6)

        _result, summary, report = self.run_doctor(setup=setup)
        # The headline fires once (summary carries the headline, the report the
        # detail); the headline no longer asserts the lane is "down".
        autocapture_warnings = [
            w for w in summary["warnings"] if "auto-capture wrote NO note" in w
        ]
        self.assertEqual(len(autocapture_warnings), 1, summary["warnings"])
        self.assertNotIn("the distillation lane may be down", autocapture_warnings[0])
        # The detail names the reason the log recorded, not a guess at auth.
        self.assertIn("Dominant recorded reason", report)
        self.assertIn("timed out after 120s", report)
        self.assertNotIn("unauthenticated or unreachable", report)


if __name__ == "__main__":
    unittest.main()
