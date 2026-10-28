#!/usr/bin/env python3
"""Focused red/green/indeterminate controls for the P13 batch checks."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_checkout import normal_checkout_root  # noqa: E402
import doctor_fixture  # noqa: E402

# See dispatch_checkout: send-task.sh refuses to dispatch from a linked
# worktree, and that refusal runs before the guards this suite tests -- so
# without this the result depends on checkout shape, not on behaviour.
# The helper returns the root unchanged in a main checkout.
ROOT = normal_checkout_root(Path(__file__).resolve().parents[3])
VERIFY = ROOT / "bin" / "dispatch-toolkit-verify.sh"


def verifier_lane_clis() -> dict[str, str]:
    """Read the lane-to-CLI pairing from the verifier's parallel arrays."""
    source = VERIFY.read_text(encoding="utf-8")
    arrays = {}
    for name in ("LANES", "CLIS"):
        match = re.search(rf"^{name}=\(([^)]*)\)", source, re.M)
        if match is None:
            raise RuntimeError(f"{name}=(...) not found in {VERIFY}")
        arrays[name] = match.group(1).split()
    if len(arrays["LANES"]) != len(arrays["CLIS"]):
        raise RuntimeError("verifier LANES and CLIS arrays are misaligned")
    return dict(zip(arrays["LANES"], arrays["CLIS"]))


LANE_CLIS = verifier_lane_clis()

_EMPTY_PS = doctor_fixture.EMPTY_PS


def run_bash(script: Path, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script)],
        cwd=ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


class GraduationScanTriStateTest(unittest.TestCase):
    def run_scan(self, ledger: str | None, *, jq_bin: str = "jq"):
        with tempfile.TemporaryDirectory(prefix="graduation-scan-") as temp:
            root = Path(temp)
            patterns = root / "patterns.jsonl"
            candidates = root / "candidates.md"
            if ledger is not None:
                patterns.write_text(ledger, encoding="utf-8")
            result = run_bash(
                ROOT / "bin/graduation-scan.sh",
                env={
                    "GRADUATION_PATTERNS_UNDER_TEST": str(patterns),
                    "GRADUATION_CANDIDATES_UNDER_TEST": str(candidates),
                    "GRADUATION_JQ_UNDER_TEST": jq_bin,
                },
            )
            report = candidates.read_text(encoding="utf-8") if candidates.exists() else ""
            return result, report

    def test_absent_or_malformed_ledger_is_indeterminate(self):
        for ledger in (None, "{not-json}\n", '{"ts": 3}\n'):
            with self.subTest(ledger=ledger):
                result, report = self.run_scan(ledger)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("COULD NOT DETERMINE", report)

    def test_missing_parser_is_indeterminate(self):
        result, report = self.run_scan("", jq_bin="/definitely/missing/jq")
        self.assertEqual(result.returncode, 2)
        self.assertIn("COULD NOT DETERMINE", report)

    def test_empty_completed_scan_passes(self):
        result, report = self.run_scan("")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No graduation candidates", report)
        self.assertIn("PASS:", result.stdout)

    def test_threshold_hit_is_a_finding(self):
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            {
                "ts": now,
                "routine_signature": "same-routine",
                "engagement_id": f"engagement-{index}",
                "specialist": "devops-engineer",
            }
            for index in range(3)
        ]
        result, report = self.run_scan(
            "".join(json.dumps(row) + "\n" for row in rows)
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("same-routine", report)
        self.assertIn("FAIL:", result.stderr)


class DispatchToolkitTriStateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dispatch-toolkit-")
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory"
        self.inventory.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write_toolkit(self, codex_expected: str = "alpha") -> Path:
        """A RUNNABLE stand-in, invoked as `bash <toolkit> <namespace> <lane>`.

        This fixture used to be the bare `case` branches with no surrounding
        script -- a file bash cannot parse. That went unnoticed because the
        verifier only ever read the toolkit's source text (G-N1); it now runs
        the subject, so the fixture has to be a real script. The breakage
        shapes that the old fixture accidentally embodied are asserted red in
        test_dispatch_toolkit_verify_subject.py.
        """
        toolkit = self.root / "dispatch-toolkit.sh"
        blocks = {
            "gpt-codex": codex_expected,
            "claude": "bravo",
            "gemini": "charlie",
            "grok": "echo",
        }
        # The shell consumer lands in wave 2. Seed any still-declared legacy
        # lane without requiring it once the consumer uses the four-lane set.
        blocks.update({lane: "delta" for lane in LANE_CLIS if lane not in blocks})
        branches = "".join(
            f"    {lane})\n"
            "        cat <<'EOF'\n"
            "\n"
            "## Expected Model Lane Tool Surface\n"
            "\n"
            f"This lane expects `{name}`. Later tools include `not_a_server`.\n"
            "EOF\n"
            "        ;;\n"
            for lane, name in blocks.items()
        )
        toolkit.write_text(
            '#!/bin/bash\ncase "${2:-}" in\n' + branches + "esac\n",
            encoding="utf-8",
        )
        return toolkit

    def write_clean_inventories(self):
        fixture_by_lane = {
            "gpt-codex": json.dumps([{"name": "alpha"}]),
            "claude": "Checking MCP server health…\nbravo: /bin/bravo - ✔ Connected\n",
            "gemini": (
                "NAME                     TYPE   STATUS   COMMAND/URL\n"
                "charlie                  stdio  enabled  /bin/charlie\n"
            ),
            "grok": "MCP Servers (1)\n└── echo (stdio)\n",
        }
        for lane in LANE_CLIS:
            contents = fixture_by_lane.get(lane, "delta /bin/delta enabled\n")
            (self.inventory / f"{LANE_CLIS[lane]}.txt").write_text(
                contents, encoding="utf-8"
            )

    def run_verify(self, toolkit: Path, *, extra_env: dict[str, str] | None = None):
        environment = {
            "DISPATCH_TOOLKIT_UNDER_TEST": str(toolkit),
            "DISPATCH_TOOLKIT_MCP_LIST_DIR_UNDER_TEST": str(self.inventory),
        }
        if extra_env:
            environment.update(extra_env)
        return run_bash(
            VERIFY,
            env=environment,
        )

    def write_cli_stubs(self) -> Path:
        """Install deterministic native-inventory twins ahead of the real CLIs."""
        stub_bin = self.root / "bin"
        stub_bin.mkdir()
        outputs = {
            "codex": "printf '[{\"name\": \"alpha\"}]\\n'\n",
            "claude": (
                '/bin/sleep "${FAKE_CLAUDE_DELAY_SECONDS:-0}"\n'
                "printf 'Checking MCP server health…\\n'\n"
                "printf 'bravo: /bin/bravo - ✔ Connected\\n'\n"
            ),
            "agy": (
                "printf 'NAME                     TYPE   STATUS   COMMAND/URL\\n'\n"
                "printf 'charlie                  stdio  enabled  /bin/charlie\\n'\n"
            ),
            "grok": "printf 'MCP Servers (1)\\n└── echo (stdio)\\n'\n",
        }
        outputs.update({cli: "printf 'delta /bin/delta enabled\\n'\n"
                        for cli in LANE_CLIS.values() if cli not in outputs})
        for cli, body in outputs.items():
            path = stub_bin / cli
            path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
            path.chmod(0o755)
        return stub_bin

    def run_verify_with_cli_stubs(
        self,
        toolkit: Path,
        *,
        claude_delay_seconds: int,
        timeout_override_seconds: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        stub_bin = self.write_cli_stubs()
        environment = {
            "DISPATCH_TOOLKIT_UNDER_TEST": str(toolkit),
            "DISPATCH_TOOLKIT_MCP_LIST_DIR_UNDER_TEST": "",
            "FAKE_CLAUDE_DELAY_SECONDS": str(claude_delay_seconds),
            "PATH": f"{stub_bin}{os.pathsep}{os.environ['PATH']}",
        }
        if timeout_override_seconds is not None:
            environment["DISPATCH_TOOLKIT_MCP_LIST_TIMEOUT_SECONDS"] = str(
                timeout_override_seconds
            )
        return run_bash(VERIFY, env=environment)

    def test_clean_exact_sets_pass(self):
        self.write_clean_inventories()
        result = self.run_verify(self.write_toolkit())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS:", result.stdout)

    def test_novel_name_mismatch_fails(self):
        self.write_clean_inventories()
        result = self.run_verify(self.write_toolkit("future_mcp.v2"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("future_mcp.v2", result.stdout)
        self.assertIn("FAIL:", result.stdout)

    def test_inventory_that_cannot_run_is_indeterminate(self):
        self.write_clean_inventories()
        (self.inventory / f"{LANE_CLIS['gemini']}.txt").unlink()
        result = self.run_verify(self.write_toolkit())
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("COULD NOT DETERMINE", result.stdout)

    def test_default_timeout_scales_past_a_healthy_nine_second_probe(self):
        result = self.run_verify_with_cli_stubs(
            self.write_toolkit(), claude_delay_seconds=9
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS:", result.stdout)

    def test_timeout_override_keeps_a_timeout_indeterminate(self):
        result = self.run_verify_with_cli_stubs(
            self.write_toolkit(),
            claude_delay_seconds=5,
            timeout_override_seconds=1,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "COULD NOT DETERMINE: claude MCP inventory failed or timed out (exit 124)",
            result.stdout,
        )
        self.assertNotIn("PASS:", result.stdout)

    def test_registered_compatibility_alias_is_visible_without_failure(self):
        self.write_clean_inventories()
        codex_inventory = self.inventory / f"{LANE_CLIS['gpt-codex']}.txt"
        codex_inventory.write_text(
            json.dumps([{"name": "alpha"}, {"name": "chrono-kg"}]),
            encoding="utf-8",
        )
        gemini_inventory = self.inventory / f"{LANE_CLIS['gemini']}.txt"
        gemini_inventory.write_text(
            "NAME TYPE STATUS COMMAND/URL\n"
            "charlie stdio enabled /bin/charlie\n"
            "chrono-kg stdio enabled /bin/chrono-kg\n",
            encoding="utf-8",
        )

        result = self.run_verify(self.write_toolkit())

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NOTE: codex lists 'chrono-kg'", result.stdout)
        self.assertIn(f"NOTE: {LANE_CLIS['gemini']} lists 'chrono-kg'", result.stdout)
        self.assertIn("compatibility-only", result.stdout)
        self.assertNotIn("WARN: codex lists 'chrono-kg'", result.stdout)
        self.assertNotIn(f"WARN: {LANE_CLIS['gemini']} lists 'chrono-kg'", result.stdout)

    def test_future_registry_declared_compatibility_alias_needs_no_code_change(self):
        self.write_clean_inventories()
        codex_inventory = self.inventory / f"{LANE_CLIS['gpt-codex']}.txt"
        codex_inventory.write_text(
            json.dumps([{"name": "alpha"}, {"name": "legacy-bridge"}]),
            encoding="utf-8",
        )
        registry = self.root / "skill-tool-registry.tsv"
        registry.write_text(
            "\t".join(
                (
                    "name",
                    "record_kind",
                    "type",
                    "path_or_source",
                    "lanes",
                    "invocation",
                    "verified_state",
                    "cost_tier",
                    "evidence",
                    "notes",
                    "purpose",
                    "hunting_type",
                    "target_class",
                )
            )
            + "\n"
            + "\t".join(
                (
                    "legacy-bridge",
                    "tool",
                    "mcp",
                    "/bin/legacy-bridge",
                    "codex",
                    "archive compatibility calls; prefer canonical-next for new callers",
                    "yes",
                    "subscription",
                    "fixture:1",
                    "Compatibility alias backed by the canonical server.",
                    "dispatch",
                    "—",
                    "—",
                )
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.run_verify(
            self.write_toolkit(),
            extra_env={"DISPATCH_TOOLKIT_REGISTRY_UNDER_TEST": str(registry)},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NOTE: codex lists 'legacy-bridge'", result.stdout)
        self.assertIn("compatibility-only", result.stdout)
        self.assertNotIn("WARN: codex lists 'legacy-bridge'", result.stdout)

    def test_ordinary_installed_but_unadvertised_server_still_fails(self):
        self.write_clean_inventories()
        codex_inventory = self.inventory / f"{LANE_CLIS['gpt-codex']}.txt"
        codex_inventory.write_text(
            json.dumps([{"name": "alpha"}, {"name": "ordinary-extra"}]),
            encoding="utf-8",
        )

        result = self.run_verify(self.write_toolkit())

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            "WARN: codex lists 'ordinary-extra' but gpt-codex does not enumerate it",
            result.stdout,
        )
        self.assertIn("FAIL: 1 mismatch(es) found", result.stdout)


class WriteScopeGuardTriStateTest(unittest.TestCase):
    def make_repro(self, body: str) -> Path:
        path = Path(self.temporary.name) / "repro.sh"
        path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wsguard-c4-")
        temporary_root = Path(self.temporary.name)
        fixtures = temporary_root / "fixtures"
        shutil.copytree(ROOT / "tools/wsguard/fixtures", fixtures)
        for packet in fixtures.glob("*.md"):
            source = packet.read_text(encoding="utf-8")
            self.assertNotIn("reviews:", source, packet)
            packet.write_text(
                source.replace(
                    "direct_lane_work_allowed: true\n",
                    "direct_lane_work_allowed: true\nreviews: none\n",
                    1,
                ),
                encoding="utf-8",
            )

        verifier_source = (ROOT / "tools/wsguard/verify.sh").read_text(
            encoding="utf-8"
        )
        old_root = 'ROOT="$(cd -- "${HERE}/../.." && pwd -P)"'
        self.assertIn(old_root, verifier_source)
        verifier_source = verifier_source.replace(
            old_root, f"ROOT={shlex.quote(str(ROOT))}", 1
        ).replace(
            'rm -f -- "$REPRO_LOG"',
            'find "$REPRO_LOG" -type f -delete',
            1,
        )
        self.verifier = temporary_root / "verify.sh"
        self.verifier.write_text(verifier_source, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_verify(self, repro: Path):
        return run_bash(
            self.verifier,
            # C1/C3 must reach the write-scope guard before C4's tri-state
            # result is meaningful, including in a detached CI checkout.
            env={"WSGUARD_REPRO_UNDER_TEST": str(repro),
                 "SQUAD_BASE_BRANCH": "v2"},
        )

    def test_completed_silent_twin_passes(self):
        result = self.run_verify(self.make_repro("echo WSGUARD_REPRO_COMPLETE\n"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_completed_warning_twin_fails(self):
        result = self.run_verify(
            self.make_repro(
                "echo 'WARNING: write_scope path is gitignored'\n"
                "echo WSGUARD_REPRO_COMPLETE\n"
            )
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_failed_twin_is_indeterminate(self):
        result = self.run_verify(self.make_repro("exit 42\n"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("COULD NOT DETERMINE", result.stdout)


class DoctorTargetContractTest(unittest.TestCase):
    def test_idless_dispatches_count_toward_volume(self):
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for named_count in (0, 200):
            with self.subTest(named_count=named_count):
                records = [{"ts": now, "task_id": f"TASK-{i}"}
                           for i in range(named_count)]
                records += [{"ts": now}] * (601 - named_count)
                result, summary = self.run_doctor(
                    dispatch_log="".join(json.dumps(row) + "\n" for row in records)
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("dispatch volume 601 exceeds 600/24h ceiling", summary["issues"])
                self.assertFalse(any("times in 24h" in issue for issue in summary["issues"]))

    def test_only_nonempty_string_ids_form_retry_groups(self):
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = [{"ts": now, "task_id": task_id}
                   for task_id in (None, "", 7, False, [], {}) for _ in range(10)]
        records += [{"ts": "2000-01-01T00:00:00Z", "task_id": "TASK-old"}] * 601
        result, summary = self.run_doctor(
            dispatch_log="".join(json.dumps(row) + "\n" for row in records)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(summary["issue_count"], 0, summary["issues"])
        self.assertEqual(summary["gate_unknown_count"], 0, summary["gate_unknowns"])

    def run_doctor(self, *, dispatch_log: str | None = None):
        with tempfile.TemporaryDirectory(prefix="doctor-target-contract-") as temp:
            fixture = Path(temp)
            root = fixture / "root"
            doctor_fixture.install_doctor_helpers(ROOT, root)

            subprocess.run(
                ["git", "init", "-q"], cwd=root, check=True, capture_output=True
            )

            home = fixture / "home"
            local_bin = home / ".local" / "bin"
            doctor_fixture.write_stub(local_bin, "ps", _EMPTY_PS)
            # Doctor now gates on the launcher's required-command list, so the
            # fixture supplies it rather than inheriting the maintainer host's
            # answer to whether a required CLI is installed.
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
            environment.pop("CHRONO_DOCTOR_LOG_DIR", None)
            environment.pop("CHRONO_VAULT_ROOT", None)

            # A zero-state install still needs the reviewed, installed guard.
            # Match the README setup before asking doctor whether it is clean.
            from scripts.python.tests.test_doctor_launch_dependency_parity import seed_guard_inputs
            seed_guard_inputs(ROOT, root)
            subprocess.run(
                ["bash", str(ROOT / "docs/install/install-pre-commit-hook.sh")],
                cwd=root, env=environment, check=True, capture_output=True,
            )

            if dispatch_log is not None:
                ledger = root / "_state" / "dispatch-log.jsonl"
                ledger.parent.mkdir(parents=True)
                ledger.write_text(dispatch_log, encoding="utf-8")

            result = subprocess.run(
                ["/bin/bash", str(root / "bin" / "doctor.sh")],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            summaries = sorted(
                (home / ".local/state/chrono-vault/doctor-logs").glob(
                    "*-summary.json"
                )
            )
            self.assertEqual(
                len(summaries),
                1,
                f"doctor did not emit one summary: {result.stdout}{result.stderr}",
            )
            summary = json.loads(summaries[0].read_text(encoding="utf-8"))
            return result, summary

    def test_absent_and_unreadable_present_targets_have_distinct_exit_codes(self):
        zero_state, zero_summary = self.run_doctor()
        self.assertEqual(
            zero_state.returncode, 0, zero_state.stdout + zero_state.stderr
        )
        self.assertGreater(zero_summary["absent_input_count"], 0)
        self.assertEqual(
            zero_summary["gate_unknown_count"], 0, zero_summary["gate_unknowns"]
        )
        self.assertIn("what a fresh install looks like", zero_state.stdout)

        unreadable, unreadable_summary = self.run_doctor(dispatch_log="{not-json}\n")
        self.assertEqual(
            unreadable.returncode, 2, unreadable.stdout + unreadable.stderr
        )
        self.assertIn(
            "dispatch-log token-spend scan failed",
            unreadable_summary["gate_unknowns"],
        )
        self.assertIn("input was there", unreadable.stdout)

    def test_present_dispatch_volume_still_detects_a_burst(self):
        empty, summary = self.run_doctor(dispatch_log="")
        self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
        self.assertEqual(summary["issue_count"], 0)
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        def rows(task_ids):
            return "".join(
                json.dumps({"ts": now, "model_lane": "codex", "task_id": t}) + "\n"
                for t in task_ids
            )

        # A busy day of distinct tasks warns but does not block a launch.
        busy, summary = self.run_doctor(
            dispatch_log=rows(f"TASK-{i}" for i in range(201))
        )
        self.assertEqual(summary["issue_count"], 0, summary["issues"])
        self.assertIn("dispatch volume 201 above 200/24h baseline", summary["warnings"])

        # The same task re-dispatched 10 times is a loop and blocks.
        loop, summary = self.run_doctor(dispatch_log=rows(["TASK-loop"] * 10))
        self.assertEqual(loop.returncode, 1, loop.stdout + loop.stderr)
        self.assertIn("task TASK-loop dispatched 10 times in 24h", summary["issues"])

        # Runaway task creation (distinct ids) past the ceiling also blocks.
        flood, summary = self.run_doctor(
            dispatch_log=rows(f"TASK-{i}" for i in range(601))
        )
        self.assertEqual(flood.returncode, 1, flood.stdout + flood.stderr)
        self.assertIn("dispatch volume 601 exceeds 600/24h ceiling", summary["issues"])



if __name__ == "__main__":
    unittest.main()
