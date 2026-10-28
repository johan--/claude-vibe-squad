#!/usr/bin/env python3
"""bin/canary.sh — the properties that make it a gate rather than decoration.

WHY THIS FILE EXISTS
  This suite is in the awkward position of being a unit test for a program
  whose entire premise is that unit tests cannot measure capability. It does
  not try to. It pins the four properties that decide whether the live probes
  are worth believing, every one of them learned from a green-but-broken case:

    1. Every probe demonstrably FAILS when its capability is broken. Four
       capabilities shipped green and dead in one build -- board fan-out,
       swarm, the notification spine, anti-affinity review -- and one of them
       still passed a 35-test suite with its enforcement replaced by
       ``if False:``. A canary that cannot fail is not a gate.
    2. Every probe demonstrably PASSES on a working fixture. A probe stuck at
       FAIL satisfies every inversion above while measuring nothing.
    3. NOT MEASURED is never scored as a pass, and never collapses to a
       boolean. Same reason doctor.sh carries COULD NOT DETERMINE.
    4. The skills oracle is not quotable from the packet. If the packet
       contained the sentinel, a lane could echo it back without ever loading
       the skill -- and the probe would certify "fired" for a projection.
    5. The expected MCP surface is not quotable from the packet. The worker
       must enumerate and exercise its own live runtime namespaces; a config
       read or echoed allowlist is not capability evidence.

SAFETY
  No test here writes to the live private vault. Every invocation either
  passes ``--no-memory-write`` or runs with ``CHRONO_VAULT_ROOT`` unset, and
  the write path is refused before ``notes.record`` is reached. Registry probes
  run against ``CANARY_ROOT_UNDER_TEST`` fixtures in a temp directory; the only
  live-tree assertions read the probe-canary skill, public source, export
  policy, and canonical documentation. Expectations use synthetic prefixes.
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
CANARY = REPO_ROOT / "bin" / "canary.sh"
SKILL_FILE = REPO_ROOT / ".claude" / "skills" / "probe-canary" / "SKILL.md"
MCP_SURFACE_DOC = REPO_ROOT / "docs" / "board-mcp-surface.md"
MCP_MARKER = "MCP_SURFACE_JSON:"
CONTEXT_SCHEMA = "go-live-trusted-context/v1"

# Duplicated from canary.sh on purpose, and the duplication is the point: if
# either copy drifts, test_sentinel_is_live_in_the_skill_file below goes red
# and names the drift. An oracle nobody notices going stale is how a probe
# starts reporting on nothing.
SENTINEL = "project-scoped skill loading works"
AGENTS_SENTINEL = "You reached this file."


def fixture_mcp_surface() -> list[str]:
    """Synthetic projection; never read this operator's local expectation."""
    return ["fixture_alpha", "fixture_beta"]


def run_canary(
    *args: str,
    root: Path | None = None,
    env_overrides: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke canary.sh with the live vault write path disabled."""
    env = dict(os.environ)
    env.pop("CHRONO_VAULT_ROOT", None)
    env.pop("CANARY_ROOT_UNDER_TEST", None)
    env.pop("CANARY_MCP_EXPECTED_FILE", None)
    env["CHRONO_PY"] = sys.executable
    env["CANARY_MCP_EXPECTED_JSON"] = json.dumps(fixture_mcp_surface())
    if root is not None:
        env["CANARY_ROOT_UNDER_TEST"] = str(root)
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["bash", str(CANARY), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=300,
    )


def status_of(output: str, probe: str) -> str:
    """Return PASS / FAIL / NOT MEASURED for one probe, or '' if absent."""
    for line in output.splitlines():
        if not line.startswith("["):
            continue
        status, _, rest = line[1:].partition("]")
        if rest.split() and rest.split()[0] == probe:
            return status.strip()
    return ""


def write_fixture(root: Path, entry: dict, task_id: str) -> None:
    (root / "_state").mkdir(parents=True, exist_ok=True)
    (root / "_state" / "active-tasks.json").write_text(
        json.dumps({task_id: entry}), encoding="utf-8"
    )


def write_persisted_prompt(
    root: Path, entry: dict, task_id: str, prompt: str
) -> None:
    """Write the controller-built brief that remains after inbox consumption."""
    attempt_id = entry["delivery_attempt_id"]
    generation = entry["delivery_generation"]
    context_dir = root / "_state" / "board-dispatch"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / f"{task_id}.{attempt_id}.context.json").write_text(
        json.dumps(
            {
                "schema": CONTEXT_SCHEMA,
                "authority": {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "generation": generation,
                },
                "task_prompt": prompt,
            }
        ),
        encoding="utf-8",
    )


class SelfTestIsTheGate(unittest.TestCase):
    """canary.sh --self-test is the inverted-control suite; it must hold."""

    def test_every_inversion_and_control_holds(self) -> None:
        result = run_canary("--self-test")
        self.assertEqual(
            result.returncode,
            0,
            f"--self-test failed:\n{result.stdout}\n{result.stderr}",
        )
        self.assertNotIn("INVERSION FAILED", result.stdout)
        self.assertNotIn("CONTROL FAILED", result.stdout)
        # An empty self-test would pass both assertions above while proving
        # nothing -- the same silent-no-op shape the probes guard against.
        self.assertGreaterEqual(result.stdout.count("inversion holds"), 10)
        self.assertGreaterEqual(result.stdout.count("control holds"), 5)

    def test_public_dependencies_run_without_private_state_or_venv(self) -> None:
        from tools.export.path_policy import load_policy

        policy = load_policy(REPO_ROOT / "tools/export/policy/path-policy.json")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in ("bin/canary.sh", "shared/repo-root.sh",
                             "shared/specialist-runtime-map.tsv"):
                self.assertEqual(policy.classify(relative), "public", relative)
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((REPO_ROOT / relative).read_bytes())
            env = dict(os.environ)
            for key in ("VAULT_ROOT", "CHRONO_PY", "CHRONO_VAULT_ROOT",
                        "CANARY_ROOT_UNDER_TEST", "CANARY_MCP_EXPECTED_JSON",
                        "CANARY_MCP_EXPECTED_FILE"):
                env.pop(key, None)
            self.assertFalse((root / "_state").exists())
            self.assertFalse((root / ".venv").exists())
            result = subprocess.run(
                ["bash", str(root / "bin/canary.sh"), "--self-test"],
                cwd=root, env=env, text=True, capture_output=True, timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("working fixture / mcp_surface PASS", result.stdout)
            # The script pads columns; use the shared result text for inversions.
            self.assertIn("MCP namespace missing", result.stdout)
            self.assertNotIn("CONTROL FAILED", result.stdout)


class ThreeOutcomesNeverTwo(unittest.TestCase):
    """pass / fail / NOT MEASURED, and NOT MEASURED is never a pass."""

    def test_absent_registry_is_unmeasured_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_canary("--task", "TASK-X", "--no-memory-write", root=root)
        for probe in ("dispatch", "round_trip", "labelling", "mcp_surface"):
            self.assertEqual(
                status_of(result.stdout, probe),
                "NOT MEASURED",
                f"{probe} scored an unreadable registry:\n{result.stdout}",
            )
        # Exit 2, not 0: an unmeasured run must not read as a healthy one.
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_a_broken_capability_exits_one(self) -> None:
        task = "TASK-2099-01-01-0004-brk"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_fixture(
                root,
                {
                    "source_namespace": "coding",
                    "status": "complete",
                    "dispatched_at": "2099-01-01T00:00:00+00:00",
                    "return_artifact": f"departments/coding/outbox/{task}-response.md",
                    # Queued and never claimed: the shape a fan-out that was
                    # refused before host admission actually leaves behind.
                    "delivery_history": [{"event": "queued"}],
                },
                task,
            )
            result = run_canary("--task", task, "--no-memory-write", root=root)
        self.assertEqual(status_of(result.stdout, "dispatch"), "FAIL", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_unknown_argument_is_refused(self) -> None:
        # Silently ignoring `--tsk` would run the default path while the caller
        # believed a task had been adjudicated. 64 is EX_USAGE, as in doctor.sh.
        result = run_canary("--tsk", "TASK-X")
        self.assertEqual(result.returncode, 64, result.stdout + result.stderr)


class SkillsProbeMeasuresFiringNotProjection(unittest.TestCase):
    def test_sentinel_is_live_in_the_skill_file(self) -> None:
        if not SKILL_FILE.is_file():
            self.skipTest(f"NOT MEASURED: skill oracle is absent: {SKILL_FILE}")

        # The oracle rots silently otherwise: reword the skill and the probe
        # keeps running while it can no longer detect anything.
        self.assertIn(
            SENTINEL,
            SKILL_FILE.read_text(encoding="utf-8"),
            "the probe-canary sentinel changed; canary.sh SKILL_SENTINEL and this "
            "test must be updated together or the skills probe measures nothing",
        )

    def test_emitted_packet_never_quotes_the_sentinel(self) -> None:
        # If it did, a lane could satisfy the probe by echoing the packet back
        # without loading the skill, and "projected" would certify as "fired".
        result = run_canary("--emit-packet", "TASK-2099-01-01-0005-pkt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(SENTINEL, result.stdout)
        self.assertNotIn(AGENTS_SENTINEL, result.stdout)
        self.assertIn("probe-canary", result.stdout)
        self.assertIn("run_id: TASK-2099-01-01-0005-pkt", result.stdout)
        self.assertIn("to_model: gpt-codex", result.stdout)
        self.assertIn("specialist: backend-engineer", result.stdout)
        self.assertNotIn(MCP_MARKER, result.stdout)

    def test_emitted_mcp_packet_never_quotes_expected_surface(self) -> None:
        result = run_canary(
            "--emit-mcp-packet", "TASK-2099-01-01-0008-mcp-pkt"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("to_model: gpt-codex", result.stdout)
        self.assertIn("specialist: systems-engineer", result.stdout)
        self.assertIn("run_id: TASK-2099-01-01-0008-mcp-pkt", result.stdout)
        self.assertIn(MCP_MARKER, result.stdout)
        self.assertIn("tool_names", result.stdout)
        self.assertIn("mcp__<runtime prefix>__<tool>", result.stdout)
        self.assertNotIn(
            json.dumps(fixture_mcp_surface(), separators=(",", ":")),
            result.stdout,
            "the MCP packet quoted the expected answer instead of asking for a probe",
        )
        for prefix in fixture_mcp_surface():
            self.assertNotIn(prefix, result.stdout)

    def test_persisted_assembled_brief_can_reach_pass(self) -> None:
        """The ask oracle survives after the dispatcher consumes the inbox packet."""
        task = "TASK-2099-01-01-0009-persisted"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / ".claude" / "skills" / "probe-canary"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"**{SENTINEL}**\n", encoding="utf-8")
            outbox = root / "departments" / "coding" / "outbox"
            outbox.mkdir(parents=True)
            (outbox / f"{task}-response.md").write_text(
                f"The loaded skill says {SENTINEL}.\n", encoding="utf-8"
            )
            entry = {
                "source_namespace": "coding",
                "status": "complete",
                "dispatched_at": "2099-01-01T00:00:00+00:00",
                "delivery_attempt_id": "d-persisted",
                "delivery_generation": 1,
                "delivery_lane": "claude",
                "return_artifact": f"departments/coding/outbox/{task}-response.md",
                "delivery_history": [
                    {"event": "queued"},
                    {"event": "board-claimed"},
                    {"event": "terminal"},
                ],
            }
            write_fixture(root, entry, task)
            write_persisted_prompt(
                root,
                entry,
                task,
                "Invoke the project skill named probe-canary and quote it.",
            )
            self.assertFalse((root / "departments/coding/inbox" / f"{task}.md").exists())
            self.assertFalse((root / "departments/coding/archive" / f"{task}.md").exists())
            result = run_canary("--task", task, "--no-memory-write", root=root)
        self.assertEqual(status_of(result.stdout, "skills"), "PASS", result.stdout)

    def test_released_attempt_uses_the_dispatched_lane_working_tree(self) -> None:
        """The artifact outlives both the attempt directory and its Git ref."""
        task = "TASK-2099-01-01-0012-released"
        homes = (
            ("claude", ".claude/skills", SENTINEL, AGENTS_SENTINEL),
            ("codex", ".agents/skills", AGENTS_SENTINEL, SENTINEL),
            ("gpt-codex", ".agents/skills", AGENTS_SENTINEL, SENTINEL),
            ("gemini", "model-lanes/gemini/.agents/skills", AGENTS_SENTINEL, SENTINEL),
        )
        for lane, home, expected, wrong in homes:
            with self.subTest(lane=lane), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                skill = root / home / "probe-canary/SKILL.md"
                skill.parent.mkdir(parents=True)
                outbox = root / "departments/coding/outbox"
                outbox.mkdir(parents=True)
                artifact = outbox / f"{task}-response.md"
                entry = {
                    "status": "complete",
                    "delivery_lane": lane,
                    "delivery_attempt_id": "d-released",
                    "delivery_generation": 1,
                    "return_artifact": str(artifact.relative_to(root)),
                    "delivery_history": [{"event": "board-claimed"}, {"event": "terminal"}],
                }
                write_fixture(root, entry, task)
                write_persisted_prompt(root, entry, task, "Invoke probe-canary and quote it.")
                context_path = root / "_state/board-dispatch" / f"{task}.d-released.context.json"
                context = json.loads(context_path.read_text(encoding="utf-8"))
                pool = root / "_state/board-worktrees"
                context["authority"].update(lane=lane, pool_root=str(pool))
                context_path.write_text(json.dumps(context), encoding="utf-8")
                self.assertFalse(pool.exists())
                self.assertFalse((root / ".git").exists())

                cases = (
                    (expected.encode(), expected, "PASS", "quoted the probe-canary sentinel"),
                    (expected.encode(), wrong, "FAIL", "never quoted the sentinel"),
                    (b"sentinel removed", expected, "NOT MEASURED", "no longer contains its sentinel"),
                    (b"\xff", expected, "NOT MEASURED", "oracle absent or unreadable"),
                )
                for source, quote, status, detail in cases:
                    with self.subTest(status=status, detail=detail):
                        skill.write_bytes(source)
                        artifact.write_text(quote, encoding="utf-8")
                        result = run_canary("--task", task, "--no-memory-write", root=root)
                        self.assertEqual(status_of(result.stdout, "skills"), status, result.stdout)
                        self.assertIn(detail, result.stdout)
                        self.assertNotIn("dispatched skill snapshot", result.stdout)

                skill.write_text(expected, encoding="utf-8")
                artifact.write_text(expected, encoding="utf-8")
                context["authority"]["lane"] = "grok" if lane == "claude" else "claude"
                context_path.write_text(json.dumps(context), encoding="utf-8")
                result = run_canary("--task", task, "--no-memory-write", root=root)
                self.assertEqual(status_of(result.stdout, "skills"), "NOT MEASURED", result.stdout)
                self.assertIn("disagree on the lane", result.stdout)

    def test_a_task_never_asked_is_unmeasured_not_failed(self) -> None:
        """An ordinary board task is not evidence about skills either way."""
        task = "TASK-2099-01-01-0006-ord"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / ".claude" / "skills" / "probe-canary"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"**{SENTINEL}**\n", encoding="utf-8")
            outbox = root / "departments" / "coding" / "outbox"
            outbox.mkdir(parents=True)
            # Deliberately quote both sentinels in the response: response text
            # is output, never proof the worker was asked to produce evidence.
            (outbox / f"{task}-response.md").write_text(
                f"ordinary work; {SENTINEL}; {MCP_MARKER}\n", encoding="utf-8"
            )
            entry = {
                "source_namespace": "coding",
                "status": "complete",
                "dispatched_at": "2099-01-01T00:00:00+00:00",
                "delivery_attempt_id": "d-ordinary",
                "delivery_generation": 1,
                "return_artifact": f"departments/coding/outbox/{task}-response.md",
                "delivery_history": [
                    {"event": "queued"},
                    {"event": "board-claimed"},
                    {"event": "terminal"},
                ],
            }
            write_fixture(root, entry, task)
            write_persisted_prompt(root, entry, task, "Perform ordinary work only.")
            result = run_canary("--task", task, "--no-memory-write", root=root)
        self.assertEqual(
            status_of(result.stdout, "skills"), "NOT MEASURED", result.stdout
        )
        self.assertEqual(
            status_of(result.stdout, "mcp_surface"), "NOT MEASURED", result.stdout
        )

    def test_malformed_persisted_brief_is_unmeasured_not_a_crash(self) -> None:
        task = "TASK-2099-01-01-0011-malformed"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / ".claude" / "skills" / "probe-canary"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"**{SENTINEL}**\n", encoding="utf-8")
            outbox = root / "departments" / "coding" / "outbox"
            outbox.mkdir(parents=True)
            (outbox / f"{task}-response.md").write_text(
                f"{SENTINEL}\n", encoding="utf-8"
            )
            entry = {
                "source_namespace": "coding",
                "status": "complete",
                "dispatched_at": "2099-01-01T00:00:00+00:00",
                "delivery_attempt_id": "d-malformed",
                "delivery_generation": 1,
                "return_artifact": f"departments/coding/outbox/{task}-response.md",
                "delivery_history": [
                    {"event": "queued"},
                    {"event": "board-claimed"},
                    {"event": "terminal"},
                ],
            }
            write_fixture(root, entry, task)
            write_persisted_prompt(root, entry, task, "Invoke probe-canary.")
            context = (
                root
                / "_state"
                / "board-dispatch"
                / f"{task}.d-malformed.context.json"
            )
            context.write_text("[]\n", encoding="utf-8")
            result = run_canary("--task", task, "--no-memory-write", root=root)
        self.assertEqual(
            status_of(result.stdout, "skills"), "NOT MEASURED", result.stdout
        )
        self.assertIn("failed task/attempt binding", result.stdout)


class McpSurfaceMeasuresTheWorkerNotConfig(unittest.TestCase):
    def run_surface_fixture(
        self,
        *,
        server_prefixes: list[str],
        successful_probes: list[str],
        tool_names: list[str] | None = None,
        env_overrides: dict[str, str | None] | None = None,
        local_expectation: bytes | None = None,
        report_overrides: dict | None = None,
    ) -> subprocess.CompletedProcess:
        task = "TASK-2099-01-01-0007-mcp"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outbox = root / "departments" / "coding" / "outbox"
            outbox.mkdir(parents=True)
            report = {
                "tool_names": (
                    tool_names if tool_names is not None
                    else sorted({f"mcp__{prefix}__probe" for prefix in server_prefixes})
                ),
                "inventory_command": "fixture live tool manifest",
                "server_prefixes": server_prefixes,
                "successful_probes": successful_probes,
            }
            report.update(report_overrides or {})
            (outbox / f"{task}-response.md").write_text(
                f"{MCP_MARKER} {json.dumps(report, separators=(',', ':'))}\n",
                encoding="utf-8",
            )
            entry = {
                "source_namespace": "coding",
                "status": "complete",
                "dispatched_at": "2099-01-01T00:00:00+00:00",
                "delivery_attempt_id": "d-mcp",
                "delivery_generation": 1,
                "return_artifact": f"departments/coding/outbox/{task}-response.md",
                "delivery_history": [
                    {"event": "queued"},
                    {"event": "board-claimed"},
                    {"event": "terminal"},
                ],
            }
            write_fixture(root, entry, task)
            if local_expectation is not None:
                (root / "_state/canary-mcp-expected.json").write_bytes(local_expectation)
            write_persisted_prompt(
                root,
                entry,
                task,
                f"Measure the live tool manifest and return {MCP_MARKER} evidence.",
            )
            return run_canary(
                "--mcp-task", task, "--no-memory-write", root=root,
                env_overrides=env_overrides,
            )

    def test_exact_live_surface_and_calls_pass(self) -> None:
        expected = fixture_mcp_surface()
        result = self.run_surface_fixture(
            server_prefixes=expected,
            successful_probes=expected,
        )
        self.assertEqual(status_of(result.stdout, "mcp_surface"), "PASS", result.stdout)

    def test_missing_namespace_fails(self) -> None:
        expected = fixture_mcp_surface()
        broken = expected[:-1]
        result = self.run_surface_fixture(
            server_prefixes=broken,
            successful_probes=broken,
        )
        self.assertEqual(status_of(result.stdout, "mcp_surface"), "FAIL", result.stdout)

    def test_changed_projection_fails_against_the_same_observation(self) -> None:
        observed = fixture_mcp_surface()
        for projection, status in ((observed, "PASS"), (["fixture_other"], "FAIL")):
            with self.subTest(projection=projection):
                result = self.run_surface_fixture(
                    server_prefixes=observed, successful_probes=observed,
                    env_overrides={"CANARY_MCP_EXPECTED_JSON": json.dumps(projection)},
                )
                self.assertEqual(status_of(result.stdout, "mcp_surface"), status, result.stdout)
                if status == "FAIL":
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("missing=['fixture_other']", result.stdout)
                    self.assertIn(f"unexpected={observed}", result.stdout)

    def test_visible_but_uncallable_namespace_fails(self) -> None:
        expected = fixture_mcp_surface()
        result = self.run_surface_fixture(
            server_prefixes=expected,
            successful_probes=expected[:-1],
        )
        self.assertEqual(status_of(result.stdout, "mcp_surface"), "FAIL", result.stdout)

    def test_visible_namespace_without_tool_inventory_is_unmeasured(self) -> None:
        expected = fixture_mcp_surface()
        result = self.run_surface_fixture(
            server_prefixes=expected,
            successful_probes=expected,
            tool_names=[],
        )
        self.assertEqual(
            status_of(result.stdout, "mcp_surface"), "NOT MEASURED", result.stdout
        )

    def test_documented_canary_contract_preserves_outcome_vocabulary(self) -> None:
        self.assertTrue(MCP_SURFACE_DOC.is_file(), f"missing {MCP_SURFACE_DOC}")
        document = MCP_SURFACE_DOC.read_text(encoding="utf-8")
        self.assertIn("## Canary contract", document)
        for outcome in ("PASS", "FAIL", "NOT_MEASURED"):
            self.assertIn(f"`{outcome}`", document)

    def test_tool_inventory_structure_and_count_are_consistent(self) -> None:
        expected = fixture_mcp_surface()
        # Vary the synthetic inventory; its total is derived, never a host snapshot.
        for operations in (("probe",), ("health", "probe", "version")):
            tools = sorted(f"mcp__{prefix}__{op}" for prefix in expected for op in operations)
            with self.subTest(operations=operations):
                result = self.run_surface_fixture(
                    server_prefixes=expected, successful_probes=expected, tool_names=tools,
                )
                self.assertEqual(status_of(result.stdout, "mcp_surface"), "PASS", result.stdout)
                self.assertIn(f"enumerated tools={len(tools)}", result.stdout)

    def test_inconsistent_or_malformed_tool_inventories_are_unmeasured(self) -> None:
        expected = fixture_mcp_surface()
        tools = sorted(f"mcp__{prefix}__probe" for prefix in expected)
        invalid = (
            tools[:-1], tools + ["mcp__fixture_unexpected__probe"],
            tools + [tools[-1]], list(reversed(tools)),
            ["mcp__fixture_alpha__"], ["mcp____probe"], ["not_a_tool"], [None],
        )
        for broken in invalid:
            with self.subTest(tools=broken):
                result = self.run_surface_fixture(
                    server_prefixes=expected, successful_probes=expected, tool_names=broken,
                )
                self.assertEqual(status_of(result.stdout, "mcp_surface"), "NOT MEASURED", result.stdout)

    def test_empty_or_unexpected_surface_fails(self) -> None:
        for visible in ([], fixture_mcp_surface() + ["fixture_unexpected"]):
            with self.subTest(visible=visible):
                result = self.run_surface_fixture(server_prefixes=visible, successful_probes=visible)
                self.assertEqual(status_of(result.stdout, "mcp_surface"), "FAIL", result.stdout)
                self.assertEqual(result.returncode, 1, result.stdout)

    def test_malformed_namespace_arrays_are_unmeasured(self) -> None:
        expected = fixture_mcp_surface()
        for key in ("server_prefixes", "successful_probes"):
            for broken in (expected + [expected[-1]], list(reversed(expected)), [None], "prefix"):
                with self.subTest(key=key, broken=broken):
                    result = self.run_surface_fixture(
                        server_prefixes=expected, successful_probes=expected,
                        report_overrides={key: broken},
                    )
                    self.assertEqual(status_of(result.stdout, "mcp_surface"), "NOT MEASURED", result.stdout)

    def test_local_expectation_file_and_environment_precedence(self) -> None:
        expected = fixture_mcp_surface()
        cases = (
            (None, json.dumps(expected).encode(), "PASS"),
            (json.dumps(expected), b'["fixture_other"]', "PASS"),
            (json.dumps(["fixture_other"]), json.dumps(expected).encode(), "FAIL"),
            ("", json.dumps(expected).encode(), "NOT MEASURED"),
            ("not-json", json.dumps(expected).encode(), "NOT MEASURED"),
        )
        for env_json, file_bytes, status in cases:
            with self.subTest(env_json=env_json, status=status):
                result = self.run_surface_fixture(
                    server_prefixes=expected, successful_probes=expected,
                    env_overrides={"CANARY_MCP_EXPECTED_JSON": env_json},
                    local_expectation=file_bytes,
                )
                self.assertEqual(status_of(result.stdout, "mcp_surface"), status, result.stdout)

    def test_missing_or_invalid_local_configuration_is_unmeasured(self) -> None:
        expected = fixture_mcp_surface()
        for file_bytes in (None, b"", b"not-json", b"\xff", b"[]", b"{}", b'[null]',
                           b'["bad prefix"]', b'["bad__prefix"]',
                           json.dumps(expected + [expected[-1]]).encode(),
                           json.dumps(list(reversed(expected))).encode()):
            with self.subTest(file_bytes=file_bytes):
                result = self.run_surface_fixture(
                    server_prefixes=expected, successful_probes=expected,
                    env_overrides={"CANARY_MCP_EXPECTED_JSON": None},
                    local_expectation=file_bytes,
                )
                self.assertEqual(status_of(result.stdout, "mcp_surface"), "NOT MEASURED", result.stdout)
                self.assertEqual(result.returncode, 2, result.stdout)

    def test_external_expectation_path_and_read_error(self) -> None:
        expected = fixture_mcp_surface()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operator.json"
            path.write_text(json.dumps(expected), encoding="utf-8")
            for filename, status in ((path, "PASS"), (path.parent, "NOT MEASURED"),
                                     (path.with_suffix(".absent"), "NOT MEASURED")):
                with self.subTest(filename=filename):
                    result = self.run_surface_fixture(
                        server_prefixes=expected, successful_probes=expected,
                        env_overrides={"CANARY_MCP_EXPECTED_JSON": None,
                                       "CANARY_MCP_EXPECTED_FILE": str(filename)},
                        local_expectation=b'["fixture_other"]',
                    )
                    self.assertEqual(status_of(result.stdout, "mcp_surface"), status, result.stdout)

    def test_placeholder_example_and_help_work_without_configuration(self) -> None:
        for args in (("--emit-mcp-expectation-example",), ("--help",),
                     ("--emit-mcp-packet", "TASK-2099-01-01-example")):
            with self.subTest(args=args):
                result = run_canary(*args, env_overrides={"CANARY_MCP_EXPECTED_JSON": None})
                self.assertEqual(result.returncode, 0, result.stderr)
                if args == ("--emit-mcp-expectation-example",):
                    example = json.loads(result.stdout)
                    self.assertTrue(example)
                    self.assertEqual(example, sorted(set(example)))
                    self.assertTrue(all(prefix.startswith("example_") for prefix in example))
                elif args == ("--help",):
                    self.assertIn("Generate yours:", result.stdout)
                    self.assertIn("CANARY_MCP_EXPECTED_FILE", result.stdout)


class MemoryProbeFailsClosed(unittest.TestCase):
    def test_unset_vault_root_is_unmeasured(self) -> None:
        # Board spawns have shipped without CHRONO_VAULT_ROOT; the vault then
        # fails closed, and a failed-closed vault must not read as a live one.
        result = run_canary()
        self.assertEqual(
            status_of(result.stdout, "memory"), "NOT MEASURED", result.stdout
        )

    def test_read_only_mode_cannot_claim_a_round_trip(self) -> None:
        result = run_canary("--no-memory-write")
        self.assertEqual(
            status_of(result.stdout, "memory"), "NOT MEASURED", result.stdout
        )


if __name__ == "__main__":
    unittest.main()
