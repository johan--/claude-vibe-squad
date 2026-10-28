from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
sys.path.insert(0, str(PYTHON_DIR))

from held_action_gate import HELD_CATEGORIES  # noqa: E402

from lane_capability_enforcement import (  # noqa: E402
    AGY_GLOBAL_CHRONO_MCPS,
    MCP_HEALTH_INSPECTED_LANES,
    CapabilityDenied,
    claude_plugins_to_disable,
    cli_args_for_materialized,
    load_json_mcp_servers,
    load_projection,
    load_tool_classes,
    materialize_disabled_plugins_settings,
    materialize_role_config,
    mcp_health,
    parse_claude_enabled_plugins,
    parse_claude_plugin_components,
    parse_claude_project_plugin_dirs,
    parse_live_mcp_listing,
    plan_lane,
    render_disabled_plugins_settings,
)
from specialist_capability_source import load_source  # noqa: E402
import seatbelt_profile  # noqa: E402
from scripts.python.tests.ci_host_independence import (  # noqa: E402
    skip_in_host_independent_ci,
)


class LaneCapabilityEnforcementTests(unittest.TestCase):


    @staticmethod
    def _agy_configured(
        **extra: dict[str, object],
    ) -> dict[str, dict[str, object]]:
        configured = {
            name: {"command": "/usr/bin/false"}
            for name in AGY_GLOBAL_CHRONO_MCPS
        }
        configured.update(extra)
        return configured

    def _supervisor_denial_record(
        self,
        *,
        lane: str,
        specialist: str,
        canonical_role: Path,
        adapter: Path,
        executable: Path,
    ) -> tuple[int, dict[str, object]]:
        def digest(value: bytes) -> str:
            return hashlib.sha256(value).hexdigest()
        now = int(time.time())
        task_id = f"TASK-2026-07-22-0705-{lane}-deny"
        attempt_id = "d-" + digest(lane.encode())[:32]
        resolved_executable = Path(os.path.realpath(executable))
        authority = {
            "schema": "go-live-authority/v1",
            "task_id": task_id,
            "attempt_id": attempt_id,
            "generation": 1,
            "run_id": "PROJ-SWARM-READY-2026-07-19",
            "author_family": "openai",
            "workload_class": "light-text",
            "specialist": specialist,
            "lane": lane,
            "mode_profile": "project",
            "execution_kind": "lane",
            "repo_root": str(ROOT),
            "pool_root": str(
                ROOT / "_state" / "lane-capability-wiring-2026-07-22" / "tests"
            ),
            "canonical_role_path": str(canonical_role),
            "canonical_role_sha256": digest(canonical_role.read_bytes()),
            "lane_overlay_path": str(adapter),
            "lane_overlay_sha256": digest(adapter.read_bytes()),
            "executable": str(executable),
            "executable_sha256": digest(resolved_executable.read_bytes()),
            "lane_args": [],
            "write_paths": [
                f"_state/lane-capability-wiring-2026-07-22/{lane}.txt"
            ],
            "read_scope": [
                "departments/coding/inbox/"
                "TASK-2026-07-22-0705-lane-capability-wiring.md"
            ],
            "depends_on": [],
            "resources": [],
            "scheduler_concurrency": 1,
            "scheduler_capacities": {},
            "scheduler_settled": {},
            "network_scope": [],
            "action_scope": ["repo-read", "worktree-write"],
            "budgets": {"timeout_seconds": 30},
            "expected_result_path": (
                f"_state/lane-capability-wiring-2026-07-22/{lane}.txt"
            ),
            "expected_outbox_path": (
                f"_state/lane-capability-wiring-2026-07-22/{lane}-response.md"
            ),
            "evidence_outputs": [],
            "required_phase_ids": [
                "S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"
            ],
            "verification_kinds": ["project_tests", "recipient_contract"],
            # Derived, never restated: the supervisor refuses any packet whose
            # operator_gates differ from HELD_CATEGORIES by even one value, so a
            # literal copy here turns every future gate change into a fixture
            # failure that looks like a policy failure. Restating it is what made
            # the 2026-08-08 vocabulary rename look like 21 broken tests.
            "operator_gates": sorted(HELD_CATEGORIES),
            "packet_sha256": digest(f"{task_id}:packet".encode()),
            "plan_sha256": digest(f"{task_id}:plan".encode()),
            "verification_contract_sha256": digest(
                f"{task_id}:contract".encode()
            ),
            "selected_model_sha256": digest(f"{lane}:model".encode()),
            "profile_bundle_sha256": (
                "95438e2cc6b06ab3f12622ad0a0f3e0a"
                "6654e6cf3a7b35f3908b3487f883f376"
            ),
            "auth_class": "subscription",
            "lane_policy_row_sha256": "a" * 64,
            "capability_surface_sha256": digest(f"{lane}:surface".encode()),
            "memory_context": {
                "schema": "chrono-vault-context/v1",
                "task_id": task_id,
                "attempt_id": attempt_id,
                "generation": 1,
                "mode": "project",
                "aperture": "none",
                "focus": None,
                "engagement_start": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
                ),
            },
            "active_board_tasks": [],
            "created_at": now,
            "expires_at": now + 300,
            "nonce": digest(f"{attempt_id}:nonce".encode()),
        }
        context = {
            "schema": "go-live-trusted-context/v1",
            "authority": authority,
            "task_prompt": "This launch must deny before any provider call.",
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", encoding="utf-8"
        ) as stream:
            json.dump(context, stream)
            stream.flush()
            completed = subprocess.run(
                [
                    "/bin/bash",
                    str(ROOT / "bin" / "board-supervisor.sh"),
                    "trusted-launch",
                    stream.name,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        return completed.returncode, json.loads(completed.stdout)

    def test_projection_reads_aliases_and_separates_lead_brokered_mcps(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / "adapter.yaml"
            overlay = root / "overlay.md"
            adapter.write_text(
                'capability_mcps: ["playwright","lead:chrono-vault"]\n'
                'tools: ["read_file","run_shell_command"]\n',
                encoding="utf-8",
            )
            overlay.write_text(
                'mcps = ["chrome-devtools","lead:sequential-thinking"]\n',
                encoding="utf-8",
            )
            projection = load_projection(
                lane="claude",
                specialist="summarizer",
                adapter_path=adapter,
                overlay_path=overlay,
            )
        self.assertEqual(
            projection["mcps"], ["chrome-devtools", "playwright"]
        )
        self.assertEqual(
            projection["brokered_mcps"], ["chrono-vault", "sequential-thinking"]
        )
        self.assertEqual(
            projection["tools"], ["read_file", "run_shell_command"]
        )

    def test_live_inventory_parsers_normalize_native_names_and_status(self) -> None:
        claude = parse_live_mcp_listing(
            lane="claude",
            output=(
                "Checking MCP server health…\n"
                "claude.ai Google Drive: https://drivemcp.googleapis.com/mcp/v1 - ✔ Connected\n"
                "claude.ai Google Calendar: https://calendarmcp.googleapis.com/mcp/v1 - ✔ Connected\n"
                "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected\n"
                "plugin:chrono-vault:chrono-vault: /bin/tool - ✔ Connected\n"
                "plugin:chrono-recon:chrono-recon: /bin/tool - ! Needs authentication\n"
                "sequential-thinking: /bin/tool - ⏸ Pending approval (run `claude` to approve)\n"
            ),
        )
        self.assertEqual(
            claude["chrono-vault"]["live_name"],
            "plugin:chrono-vault:chrono-vault",
        )
        self.assertIn("Needs authentication", claude["chrono-recon"]["status"])
        self.assertIn(
            "Pending approval", claude["sequential-thinking"]["status"]
        )
        self.assertEqual(
            claude["claude-ai-google-drive"]["live_name"],
            "claude.ai Google Drive",
        )
        self.assertEqual(
            claude["claude-ai-google-calendar"]["live_name"],
            "claude.ai Google Calendar",
        )
        self.assertEqual(
            claude["claude-ai-gmail"]["live_name"],
            "claude.ai Gmail",
        )
        self.assertEqual(
            claude["claude-ai-google-drive"]["tool_namespace"],
            "claude_ai_Google_Drive",
        )
        self.assertEqual(
            claude["claude-ai-google-calendar"]["tool_namespace"],
            "claude_ai_Google_Calendar",
        )
        self.assertEqual(
            claude["claude-ai-gmail"]["tool_namespace"],
            "claude_ai_Gmail",
        )

        gemini = parse_live_mcp_listing(
            lane="gemini",
            output=(
                "NAME          TYPE   STATUS    COMMAND/URL\n"
                "chrono-vault  stdio  enabled   /bin/tool\n"
                "stitch        http   enabled   https://example.invalid/mcp\n"
                "disabled-one  stdio  disabled  /bin/false\n"
            ),
        )
        self.assertEqual(set(gemini), {"chrono-vault", "stitch"})
        self.assertEqual(gemini["stitch"]["live_name"], "stitch")

        grok = parse_live_mcp_listing(
            lane="grok",
            output=(
                "  MCP Servers (3)\n"
                "  └ chrono-vault (stdio) plugin: chrono-vault\n"
                "  └ context7 (http) plugin: context7\n"
                "  └ sequential-thinking (stdio) ~/.claude.json [claude]\n"
                "\n  LSP Servers (0)\n"
            ),
        )
        self.assertEqual(
            set(grok), {"chrono-vault", "context7", "sequential-thinking"}
        )

    def test_grok_inventory_rejects_count_drift(self) -> None:
        with self.assertRaisesRegex(CapabilityDenied, "count does not match"):
            parse_live_mcp_listing(
                lane="grok",
                output=(
                    "  MCP Servers (2)\n"
                    "  └ chrono-vault (stdio) plugin: chrono-vault\n"
                    "\n  LSP Servers (0)\n"
                ),
            )

    def test_claude_live_inventory_rejects_ambiguous_plugin_names(self) -> None:
        with self.assertRaisesRegex(CapabilityDenied, "ambiguous"):
            parse_live_mcp_listing(
                lane="claude",
                output=(
                    "Checking MCP server health…\n"
                    "plugin:first:shared: /bin/one - ✔ Connected\n"
                    "plugin:second:shared: /bin/two - ✔ Connected\n"
                ),
            )

    def test_claude_live_inventory_normalizes_elevenlabs_connector(self) -> None:
        claude = parse_live_mcp_listing(
            lane="claude",
            output=(
                "Checking MCP server health…\n"
                "claude.ai ElevenLabs: https://example.invalid/mcp - ✔ Connected\n"
                "plugin:chrono-vault:chrono-vault: /bin/tool - ✔ Connected\n"
            ),
        )
        self.assertEqual(set(claude), {"claude-ai-elevenlabs", "chrono-vault"})
        self.assertEqual(
            claude["claude-ai-elevenlabs"],
            {
                "live_name": "claude.ai ElevenLabs",
                "tool_namespace": "claude_ai_ElevenLabs",
                "status": "✔ Connected",
            },
        )

    def test_claude_live_inventory_rejects_unknown_first_party_rows(self) -> None:
        with self.assertRaisesRegex(CapabilityDenied, "unsafe server name"):
            parse_live_mcp_listing(
                lane="claude",
                output=(
                    "Checking MCP server health…\n"
                    "claude.ai Google Docs: https://example.invalid/mcp - ✔ Connected\n"
                ),
            )

    def test_claude_live_inventory_rejects_tool_namespace_collisions(self) -> None:
        with self.assertRaisesRegex(CapabilityDenied, "ambiguous tool namespace"):
            parse_live_mcp_listing(
                lane="claude",
                output=(
                    "Checking MCP server health…\n"
                    "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected\n"
                    "claude_ai_Gmail: /bin/tool - ✔ Connected\n"
                ),
            )

    def test_claude_live_inventory_rejects_hosted_logical_name_collisions(
        self,
    ) -> None:
        with self.assertRaisesRegex(CapabilityDenied, "ambiguous server name"):
            parse_live_mcp_listing(
                lane="claude",
                output=(
                    "Checking MCP server health…\n"
                    "claude.ai Google Drive: https://drivemcp.googleapis.com/mcp/v1 - ✔ Connected\n"
                    "claude-ai-google-drive: /bin/tool - ✔ Connected\n"
                ),
            )

    def test_live_inventory_parsers_reject_missing_headers_and_drifted_rows(
        self,
    ) -> None:
        with self.assertRaisesRegex(CapabilityDenied, "header is missing"):
            parse_live_mcp_listing(
                lane="gemini",
                output="chrono-vault  stdio  enabled  /bin/tool\n",
            )
        with self.assertRaisesRegex(CapabilityDenied, "unparseable row"):
            parse_live_mcp_listing(
                lane="claude",
                output=(
                    "Checking MCP server health…\n"
                    "plugin:chrono-vault:chrono-vault changed format\n"
                ),
            )

    def test_claude_enabled_plugin_inventory_is_fail_closed(self) -> None:
        self.assertEqual(
            parse_claude_enabled_plugins(
                json.dumps(
                    [
                        {
                            "id": "example-plugin@claude-plugins-official",
                            "enabled": True,
                        },
                        {
                            "id": "disabled@claude-plugins-official",
                            "enabled": False,
                        },
                    ]
                )
            ),
            ("example-plugin",),
        )
        with self.assertRaisesRegex(CapabilityDenied, "wrong schema"):
            parse_claude_enabled_plugins("{}")
        self.assertEqual(
            parse_claude_project_plugin_dirs(
                json.dumps(
                    [
                        {
                            "id": "chrono-media-studio@claude-vibe-squad",
                            "enabled": True,
                            "scope": "project",
                            "installPath": "/tmp/chrono-media-studio",
                        }
                    ]
                )
            ),
            {"chrono-media-studio": "/tmp/chrono-media-studio"},
        )

    def test_claude_emits_plugin_aware_mcp_tool_denials(self) -> None:
        projection = {
            "lane": "claude",
            "specialist": "sample",
            "mcps": ["chrono-vault"],
            "brokered_mcps": [],
            "tools": [],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        configured = {
            "chrono-vault": {
                "live_name": "plugin:chrono-vault:chrono-vault",
                "tool_namespace": "plugin_chrono-vault_chrono-vault",
                "status": "✔ Connected",
            },
            "chrono-recon": {
                "live_name": "plugin:chrono-recon:chrono-recon",
                "tool_namespace": "plugin_chrono-recon_chrono-recon",
                "status": "✔ Connected",
            },
            "sequential-thinking": {
                "live_name": "sequential-thinking",
                "tool_namespace": "sequential-thinking",
                "status": "✔ Connected",
            },
            "claude-ai-elevenlabs": {
                "live_name": "claude.ai ElevenLabs",
                "tool_namespace": "claude_ai_ElevenLabs",
                "status": "✔ Connected",
            },
            "claude-ai-gmail": {
                "live_name": "claude.ai Gmail",
                "tool_namespace": "claude_ai_Gmail",
                "status": "✔ Connected",
            },
            "claude-ai-google-drive": {
                "live_name": "claude.ai Google Drive",
                "tool_namespace": "claude_ai_Google_Drive",
                "status": "✔ Connected",
            },
            "claude-ai-google-calendar": {
                "live_name": "claude.ai Google Calendar",
                "tool_namespace": "claude_ai_Google_Calendar",
                "status": "✔ Connected",
            },
        }
        plan = plan_lane(
            lane="claude",
            projection=projection,
            configured_servers=configured,
        )
        self.assertEqual(plan.authorized_mcps, ("chrono-vault",))
        self.assertEqual(
            plan.disabled_mcps,
            (
                "chrono-recon",
                "claude-ai-elevenlabs",
                "claude-ai-gmail",
                "claude-ai-google-calendar",
                "claude-ai-google-drive",
                "sequential-thinking",
            ),
        )
        self.assertIsNone(plan.role_config_json)
        self.assertEqual(
            plan.capability_enforcement,
            "claude-cli-disallowed-mcp-tools/v1",
        )
        self.assertEqual(
            plan.cli_args,
            (
                "--disallowedTools",
                (
                    "mcp__plugin_chrono-recon_chrono-recon__*,"
                    "mcp__claude_ai_ElevenLabs__*,"
                    "mcp__claude_ai_Gmail__*,"
                    "mcp__claude_ai_Google_Calendar__*,"
                    "mcp__claude_ai_Google_Drive__*,"
                    "mcp__sequential-thinking__*"
                ),
            ),
        )

    def test_claude_unhealthy_authorized_mcp_degrades_instead_of_denying(self) -> None:
        # An authorized server that is CONFIGURED BUT UNHEALTHY must not gate
        # the launch. Measured 2026-08-09: `github` answering HTTP 400 made the
        # whole `research` role undispatchable on a packet that never used it.
        projection = {
            "lane": "claude",
            "specialist": "sample",
            "mcps": ["chrono-recon"],
            "brokered_mcps": [],
            "tools": [],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        plan = plan_lane(
            lane="claude",
            projection=projection,
            configured_servers={
                "chrono-recon": {
                    "live_name": "plugin:chrono-recon:chrono-recon",
                    "tool_namespace": "plugin_chrono-recon_chrono-recon",
                    "status": "! Needs authentication",
                }
            },
        )
        self.assertEqual(plan.authorized_mcps, ("chrono-recon",))
        self.assertEqual(plan.unhealthy_mcps, ("chrono-recon",))
        # Verbatim, so the receipt records what the runtime said.
        self.assertEqual(
            plan.unhealthy_mcp_status,
            (("chrono-recon", "! Needs authentication"),),
        )

    def test_structurally_absent_authorized_mcp_still_fails_closed(self) -> None:
        projection = {
            "lane": "claude",
            "specialist": "sample",
            "mcps": ["chrono-recon"],
            "brokered_mcps": [],
            "tools": [],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        with self.assertRaisesRegex(
            CapabilityDenied, r"unconfigured MCP servers: chrono-recon \(absent\)"
        ):
            plan_lane(
                lane="claude",
                projection=projection,
                configured_servers={
                    "sequential-thinking": {
                        "live_name": "sequential-thinking",
                        "tool_namespace": "sequential-thinking",
                        "status": "✔ Connected",
                    }
                },
            )

    def test_required_absent_mcp_class_still_fails_closed(self) -> None:
        projection = {
            "lane": "claude",
            "specialist": "sample",
            "mcps": ["chrono-recon"],
            "brokered_mcps": [],
            "tools": [],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        with self.assertRaisesRegex(
            CapabilityDenied, r"unconfigured MCP servers: chrono-recon \(absent\)"
        ):
            plan_lane(
                lane="claude",
                projection=projection,
                configured_servers={},
                mcp_classes={"chrono-recon": {"requirement": "required"}},
            )

    def test_preferred_absent_mcp_degrades_instead_of_denying(self) -> None:
        projection = {
            "lane": "claude",
            "specialist": "sample",
            "mcps": ["chrono-recon"],
            "brokered_mcps": [],
            "tools": [],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        plan = plan_lane(
            lane="claude",
            projection=projection,
            configured_servers={},
            mcp_classes={"chrono-recon": {"requirement": "preferred"}},
        )
        self.assertEqual(plan.authorized_mcps, ())
        self.assertEqual(plan.unhealthy_mcps, ("chrono-recon",))
        self.assertEqual(plan.unhealthy_mcp_status, (("chrono-recon", "absent"),))

    def test_malformed_absent_mcp_requirement_still_fails_closed(self) -> None:
        projection = {
            "lane": "claude",
            "specialist": "sample",
            "mcps": ["chrono-recon"],
            "brokered_mcps": [],
            "tools": [],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        with self.assertRaisesRegex(
            CapabilityDenied, r"unconfigured MCP servers: chrono-recon \(absent\)"
        ):
            plan_lane(
                lane="claude",
                projection=projection,
                configured_servers={},
                mcp_classes={"chrono-recon": {"requirement": "unexpected"}},
            )

    def test_real_research_preferred_github_absence_degrades(self) -> None:
        for lane in ("claude", "codex"):
            with self.subTest(lane=lane):
                projection = {
                    "lane": lane,
                    "specialist": "research",
                    "mcps": ["github"],
                    "brokered_mcps": [],
                    "tools": [],
                    "skills": [],
                    "sources": [],
                    "schema": "role-capability-projection/v1",
                }
                classes = load_tool_classes(
                    repo_root=ROOT,
                    lane=lane,
                    specialist="research",
                )
                self.assertEqual(classes["mcp:github"]["requirement"], "preferred")
                plan = plan_lane(
                    lane=lane,
                    projection=projection,
                    configured_servers={},
                    tool_classes=classes,
                )
                self.assertEqual(plan.authorized_mcps, ())
                self.assertEqual(plan.unhealthy_mcps, ("github",))
                self.assertEqual(
                    plan.unhealthy_mcp_status, (("github", "absent"),)
                )

    def test_unsafe_authorized_server_name_or_record_still_fails_closed(self) -> None:
        def projection_for(name: str) -> dict[str, object]:
            return {
                "lane": "claude",
                "specialist": "sample",
                "mcps": [name],
                "brokered_mcps": [],
                "tools": [],
                "skills": [],
                "sources": [],
                "schema": "role-capability-projection/v1",
            }

        with self.assertRaisesRegex(CapabilityDenied, "MCP server name is unsafe"):
            plan_lane(
                lane="claude",
                projection=projection_for("chrono recon; rm -rf /"),
                configured_servers={
                    "chrono recon; rm -rf /": {
                        "live_name": "plugin:chrono-recon:chrono-recon",
                        "tool_namespace": "plugin_chrono-recon_chrono-recon",
                        "status": "✔ Connected",
                    }
                },
            )
        for label, record in (
            (
                "unsafe live name",
                {
                    "live_name": "plugin:chrono-recon:chrono-recon --dangerously",
                    "status": "✔ Connected",
                },
            ),
            (
                "unsafe status",
                {
                    "live_name": "plugin:chrono-recon:chrono-recon",
                    "status": "✔ Connected\ninjected: row",
                },
            ),
            (
                "non-string status",
                {"live_name": "plugin:chrono-recon:chrono-recon", "status": 200},
            ),
            (
                "oversized status",
                {
                    "live_name": "plugin:chrono-recon:chrono-recon",
                    "status": "x" * 4096,
                },
            ),
        ):
            with self.subTest(record=label):
                with self.assertRaisesRegex(CapabilityDenied, "unsafe"):
                    plan_lane(
                        lane="claude",
                        projection=projection_for("chrono-recon"),
                        configured_servers={"chrono-recon": record},
                        mcp_classes={
                            "chrono-recon": {"requirement": "preferred"}
                        },
                    )

    def test_mcp_health_means_one_thing_on_every_lane(self) -> None:
        # The claude branch used to demand a literal "✔ Connected" while the
        # gemini branch treated a status-less row as available. That asymmetry
        # is why `research` ran twice on gemini and failed its first claude
        # dispatch. One predicate now decides, and the lanes differ only in
        # whether they collect a status at all.
        self.assertEqual(MCP_HEALTH_INSPECTED_LANES, frozenset({"claude"}))
        for lane in ("claude", "gemini", "codex"):
            with self.subTest(lane=lane):
                self.assertEqual(
                    mcp_health(lane=lane, details={"status": "✔ Connected"}), "healthy"
                )
                self.assertEqual(
                    mcp_health(lane=lane, details={"status": "Connected"}), "healthy"
                )
                self.assertEqual(
                    mcp_health(lane=lane, details={"status": "Disconnected"}),
                    "unhealthy",
                )
                # No status collected is not a health verdict either way.
                self.assertEqual(
                    mcp_health(lane=lane, details={"command": "/usr/bin/false"}),
                    "unknown",
                )
        pending = {
            "status": "⏸ Pending approval",
            "project_config": {"command": "/usr/bin/false"},
        }
        self.assertEqual(mcp_health(lane="claude", details=pending), "healthy")
        self.assertEqual(
            mcp_health(lane="claude", details={"status": "⏸ Pending approval"}),
            "unhealthy",
        )

    def test_healthy_authorized_mcps_report_no_degradation(self) -> None:
        plan = plan_lane(
            lane="gemini",
            projection={
                "lane": "gemini",
                "specialist": "sample",
                "mcps": ["playwright"],
                "brokered_mcps": [],
                "tools": [],
                "skills": [],
                "sources": [],
                "schema": "role-capability-projection/v1",
            },
            configured_servers=self._agy_configured(
                playwright={"status": "Connected"}
            ),
        )
        self.assertEqual(plan.unhealthy_mcps, ())
        self.assertEqual(plan.unhealthy_mcp_status, ())

    def test_agy_global_exposure_emits_no_rejected_scope_flags(self) -> None:
        projection = {
            "lane": "gemini",
            "specialist": "ui-engineer",
            "mcps": ["chrome-devtools", "playwright"],
            "brokered_mcps": [],
            "tools": ["read_file"],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        configured = self._agy_configured(
            playwright={"command": "/usr/bin/false"},
            **{
                "chrome-devtools": {"command": "/usr/bin/false"},
                "unrelated": {"command": "/usr/bin/false"},
            },
        )
        plan = plan_lane(
            lane="gemini",
            projection=projection,
            configured_servers=configured,
        )
        self.assertEqual(plan.cli_args, ())
        self.assertEqual(plan.authorized_mcps, tuple(sorted(configured)))
        self.assertEqual(plan.disabled_mcps, ())
        self.assertEqual(
            plan.capability_enforcement, "agy-cli-global-mcp-exposure/v1"
        )
        self.assertEqual(plan.available_tools, ("read_file",))

    def test_agy_global_config_does_not_materialize_role_config(self) -> None:
        projection = {
            "lane": "gemini",
            "specialist": "ui-engineer",
            "mcps": ["playwright"],
            "brokered_mcps": [],
            "tools": [],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        server_config = {"command": "/usr/bin/false", "args": []}
        plan = plan_lane(
            lane="gemini",
            projection=projection,
            configured_servers=self._agy_configured(
                playwright={
                    "live_name": "playwright",
                    "project_config": server_config,
                },
            ),
        )
        self.assertIsNone(plan.role_config_json)
        self.assertEqual(plan.cli_args, ())

    def test_grok_global_exposure_is_reported_without_fictitious_scope_args(self) -> None:
        configured = {
            "chrono-vault": {"live_name": "chrono-vault"},
            "context7": {"live_name": "context7"},
        }
        plan = plan_lane(
            lane="grok",
            projection={
                "lane": "grok",
                "specialist": "smokey",
                "mcps": ["chrono-vault"],
                "brokered_mcps": [],
                "tools": [],
                "skills": [],
                "sources": [],
                "schema": "role-capability-projection/v1",
            },
            configured_servers=configured,
        )
        self.assertEqual(plan.authorized_mcps, ("chrono-vault", "context7"))
        self.assertEqual(plan.disabled_mcps, ())
        self.assertEqual(plan.cli_args, ())
        self.assertIsNone(plan.role_config_json)
        self.assertEqual(
            plan.capability_enforcement, "grok-cli-global-mcp-exposure/v1"
        )


    def test_lanes_fail_closed_on_missing_authorized_mcp(self) -> None:
        for lane in ("codex", "claude", "gemini"):
            with self.subTest(lane=lane):
                projection = {
                    "lane": lane,
                    "specialist": "sample",
                    "mcps": ["missing-server"],
                    "brokered_mcps": [],
                    "tools": [],
                    "skills": [],
                    "sources": [],
                    "schema": "role-capability-projection/v1",
                }
                with self.assertRaisesRegex(
                    CapabilityDenied, "unconfigured MCP servers"
                ):
                    plan_lane(
                        lane=lane,
                        projection=projection,
                        configured_servers={},
                    )

    def test_local_tool_names_and_availability_fail_closed(self) -> None:
        unsafe = {
            "lane": "claude",
            "specialist": "sample",
            "mcps": [],
            "brokered_mcps": [],
            "tools": ["bad tool"],
            "skills": [],
            "sources": [],
            "schema": "role-capability-projection/v1",
        }
        with self.assertRaisesRegex(CapabilityDenied, "unsafe local tool"):
            plan_lane(
                lane="claude",
                projection=unsafe,
                configured_servers={},
            )

        missing = dict(unsafe)
        missing["tools"] = ["semgrep"]
        with self.assertRaisesRegex(CapabilityDenied, "unavailable local tools"):
            plan_lane(
                lane="claude",
                projection=missing,
                configured_servers={},
                tool_lookup=lambda _name: None,
            )

    def test_materialized_config_is_worktree_local_and_owner_only(self) -> None:
        server = {"command": "/usr/bin/false"}
        plan = plan_lane(
            lane="claude",
            projection={"mcps": ["chrono-vault"], "tools": []},
            configured_servers={"chrono-vault": {"project_config": server}},
        )
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            path = materialize_role_config(
                plan,
                worktree_root=worktree,
                task_id="TASK-2026-07-22-0705-lane-capability-wiring",
                attempt_id="d-" + "1" * 32,
            )
            self.assertEqual(
                json.loads(path.read_text()),
                {"mcpServers": {"chrono-vault": server}},
            )
            self.assertEqual(
                stat.S_IMODE(path.stat().st_mode),
                0o600,
            )
            self.assertTrue(path.is_relative_to(worktree.resolve()))
            self.assertEqual(
                cli_args_for_materialized(plan, path),
                ("--mcp-config", str(path)),
            )

    def _assert_all_real_adapters_plan_from_live_inventory(
        self,
        *,
        lane: str,
        executable: Path,
        adapter_root: Path,
        project_config_path: Path | None,
    ) -> None:
        completed = subprocess.run(
            [str(executable), "mcp", "list"],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(ROOT / "model-lanes" / lane),
            timeout=30,
            env={
                key: value
                for key, value in {
                    **os.environ,
                    "NO_COLOR": "1",
                }.items()
                if lane != "claude"
                or key
                not in {
                    "ANTHROPIC_API_KEY",
                    "CLAUDECODE",
                    "CLAUDE_CODE_CHILD_SESSION",
                    "CLAUDE_CODE_ENTRYPOINT",
                    "CLAUDE_CODE_EXECPATH",
                    "CLAUDE_CODE_SESSION_ID",
                }
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        configured = parse_live_mcp_listing(
            lane=lane,
            output=completed.stdout,
        )
        project_servers = (
            load_json_mcp_servers(project_config_path)
            if project_config_path is not None
            else {}
        )
        self.assertFalse(set(project_servers) - set(configured))
        for name, config in project_servers.items():
            configured[name]["project_config"] = config
        native_tools = set(configured)
        if lane == "claude":
            plugins = subprocess.run(
                [str(executable), "plugin", "list", "--json"],
                check=False,
                capture_output=True,
                text=True,
                cwd=str(ROOT / "model-lanes" / lane),
                timeout=30,
                env={
                    key: value
                    for key, value in {
                        **os.environ,
                        "NO_COLOR": "1",
                    }.items()
                    if key
                    not in {
                        "ANTHROPIC_API_KEY",
                        "CLAUDECODE",
                        "CLAUDE_CODE_CHILD_SESSION",
                        "CLAUDE_CODE_ENTRYPOINT",
                        "CLAUDE_CODE_EXECPATH",
                        "CLAUDE_CODE_SESSION_ID",
                    }
                },
            )
            self.assertEqual(plugins.returncode, 0, plugins.stderr)
            native_tools.update(parse_claude_enabled_plugins(plugins.stdout))
        adapters = sorted(
            path
            for path in adapter_root.glob("*.md")
            if path.name != "README.md"
        )
        source_entries, _ = load_source(ROOT)
        expected_specialists = {
            specialist
            for specialist, source_lane in source_entries
            if source_lane == lane
        }
        self.assertEqual(
            {adapter.stem for adapter in adapters},
            expected_specialists,
        )
        planned = 0
        for adapter in adapters:
            projection = load_projection(
                lane=lane,
                specialist=adapter.stem,
                adapter_path=adapter,
                overlay_path=adapter,
            )
            plan_lane(
                lane=lane,
                projection=projection,
                configured_servers=configured,
                tool_classes=load_tool_classes(
                    repo_root=ROOT,
                    lane=lane,
                    specialist=adapter.stem,
                ),
                tool_lookup=lambda name: (
                    name if name in native_tools else shutil.which(name)
                ),
            )
            planned += 1
        self.assertEqual(planned, len(adapters))

    @unittest.skipUnless(
        Path(seatbelt_profile.LANE_CLI_PATHS["claude"]).is_file(),
        "requires the installed Claude CLI",
    )
    @skip_in_host_independent_ci(
        "runs installed Claude CLI against live MCP/plugin inventory"
    )
    def test_all_real_claude_adapters_plan_from_live_inventory(self) -> None:
        self._assert_all_real_adapters_plan_from_live_inventory(
            lane="claude",
            executable=Path(seatbelt_profile.LANE_CLI_PATHS["claude"]),
            adapter_root=ROOT / "model-lanes/claude/.claude/agents",
            project_config_path=ROOT / "model-lanes/claude/.mcp.json",
        )

    @unittest.skipUnless(
        Path(seatbelt_profile.LANE_CLI_PATHS["gemini"]).is_file(),
        "requires the installed agy CLI",
    )
    @skip_in_host_independent_ci(
        "runs installed agy CLI against its persistent global MCP inventory"
    )
    def test_all_real_gemini_adapters_plan_from_live_inventory(self) -> None:
        self._assert_all_real_adapters_plan_from_live_inventory(
            lane="gemini",
            executable=Path(seatbelt_profile.LANE_CLI_PATHS["gemini"]),
            adapter_root=ROOT / "model-lanes/gemini/.gemini/agents",
            project_config_path=None,
        )


class ClaudePluginTrimTests(unittest.TestCase):
    """Keeping unused operator-enabled plugins from spawning their MCP servers."""

    PLUGIN_LISTING = json.dumps(
        [
            {
                "id": "firebase@claude-plugins-official",
                "enabled": True,
                "scope": "user",
                "installPath": "/x/firebase",
                "mcpServers": {"firebase": {"command": "npx"}},
            },
            {
                "id": "playwright@claude-plugins-official",
                "enabled": True,
                "scope": "user",
                "installPath": "/x/playwright",
                "mcpServers": {"playwright": {"command": "npx"}},
            },
            {
                "id": "figma@claude-plugins-official",
                "enabled": True,
                "scope": "user",
                "installPath": "/x/figma",
                "mcpServers": {"figma": {"type": "http"}},
            },
            {
                "id": "chrono-vault@claude-vibe-squad",
                "enabled": True,
                "scope": "user",
                "installPath": "/x/chrono-vault",
                "mcpServers": {"chrono-vault": {}, "chrono-kg": {}},
            },
            {
                "id": "superpowers@claude-plugins-official",
                "enabled": True,
                "scope": "user",
                "installPath": "/x/superpowers",
                "mcpServers": {},
            },
            {
                "id": "disabled-thing@claude-plugins-official",
                "enabled": False,
                "scope": "user",
                "installPath": "/x/disabled",
                "mcpServers": {"disabled-thing": {}},
            },
        ]
    )

    def test_parse_components_keys_by_full_id_and_skips_disabled(self):
        components = parse_claude_plugin_components(self.PLUGIN_LISTING)
        self.assertIn("firebase@claude-plugins-official", components)
        self.assertNotIn("disabled-thing@claude-plugins-official", components)
        self.assertEqual(
            components["chrono-vault@claude-vibe-squad"]["mcp_servers"],
            ("chrono-kg", "chrono-vault"),
        )
        self.assertEqual(
            components["superpowers@claude-plugins-official"]["mcp_servers"], ()
        )

    def test_parse_components_rejects_unsafe_records(self):
        for bad in (
            '[{"id":"noatsign","enabled":true,"installPath":"/x","mcpServers":{}}]',
            '[{"id":"p@m","enabled":true,"installPath":"relative","mcpServers":{}}]',
            '[{"id":"p@m","enabled":true,"installPath":"/x","mcpServers":{"bad name":{}}}]',
            "not json",
            '{"not":"a list"}',
        ):
            with self.assertRaises(CapabilityDenied):
                parse_claude_plugin_components(bad)

    def test_parse_components_merges_duplicate_scopes(self):
        # A plugin enabled in both user and project scope appears twice with the
        # same id; merge into the superset of MCP servers instead of failing.
        listing = json.dumps(
            [
                {
                    "id": "chrono-media-studio@claude-vibe-squad",
                    "enabled": True,
                    "scope": "user",
                    "installPath": "/x/cms",
                    "mcpServers": {"chrono-media-studio": {}, "elevenlabs": {}},
                },
                {
                    "id": "chrono-media-studio@claude-vibe-squad",
                    "enabled": True,
                    "scope": "project",
                    "installPath": "/x/cms",
                    "mcpServers": {"chrono-media-studio": {}, "higgsfield": {}},
                },
            ]
        )
        components = parse_claude_plugin_components(listing)
        self.assertEqual(len(components), 1)
        self.assertEqual(
            components["chrono-media-studio@claude-vibe-squad"]["mcp_servers"],
            ("chrono-media-studio", "elevenlabs", "higgsfield"),
        )

    def _resolve(self, components, *, skills_by_plugin=None, ships=None):
        skills_by_plugin = skills_by_plugin or {}
        ships = ships or {}
        resolved = {}
        for plugin_id, record in components.items():
            resolved[plugin_id] = {
                "mcp_servers": record["mcp_servers"],
                "skills": tuple(skills_by_plugin.get(plugin_id, ())),
                "provides_commands": ships.get(plugin_id, (False, False))[0],
                "provides_agents": ships.get(plugin_id, (False, False))[1],
            }
        return resolved

    def test_disables_unused_pure_mcp_plugin(self):
        components = parse_claude_plugin_components(self.PLUGIN_LISTING)
        resolved = self._resolve(components)
        disable = claude_plugins_to_disable(
            plugin_components=resolved,
            authorized_mcps=("chrono-vault", "chrono-kg"),
        )
        # firebase, playwright, figma are unauthorized pure-MCP (no skills in
        # this fixture) -> disabled. chrono-vault has an authorized server ->
        # kept. superpowers has no MCP server -> never touched.
        self.assertEqual(
            disable,
            (
                "figma@claude-plugins-official",
                "firebase@claude-plugins-official",
                "playwright@claude-plugins-official",
            ),
        )

    def test_keeps_plugin_when_its_mcp_is_authorized(self):
        components = parse_claude_plugin_components(self.PLUGIN_LISTING)
        resolved = self._resolve(components)
        disable = claude_plugins_to_disable(
            plugin_components=resolved,
            authorized_mcps=("playwright", "chrono-vault", "chrono-kg"),
        )
        self.assertNotIn("playwright@claude-plugins-official", disable)

    def test_keeps_plugin_that_ships_skills(self):
        # A plugin that ships ANY skill is kept, even when its MCP is unused and
        # the skill is not projected: disabling it would strip skills the worker
        # can reach opportunistically. figma's skills keep it alive.
        components = parse_claude_plugin_components(self.PLUGIN_LISTING)
        resolved = self._resolve(
            components,
            skills_by_plugin={
                "figma@claude-plugins-official": ("figma-code-connect",),
            },
        )
        disable = claude_plugins_to_disable(
            plugin_components=resolved,
            authorized_mcps=("chrono-vault", "chrono-kg"),
        )
        self.assertNotIn("figma@claude-plugins-official", disable)
        self.assertIn("firebase@claude-plugins-official", disable)

    def test_keeps_plugin_that_ships_commands_or_agents(self):
        components = parse_claude_plugin_components(self.PLUGIN_LISTING)
        resolved = self._resolve(
            components,
            ships={"firebase@claude-plugins-official": (False, True)},
        )
        disable = claude_plugins_to_disable(
            plugin_components=resolved,
            authorized_mcps=(),
        )
        self.assertNotIn("firebase@claude-plugins-official", disable)

    def test_render_settings_is_canonical_and_merges(self):
        rendered = render_disabled_plugins_settings(
            ("playwright@claude-plugins-official", "firebase@claude-plugins-official")
        )
        self.assertEqual(
            rendered,
            '{"enabledPlugins":{"firebase@claude-plugins-official":false,'
            '"playwright@claude-plugins-official":false}}',
        )
        self.assertEqual(json.loads(rendered)["enabledPlugins"][
            "firebase@claude-plugins-official"], False)

    def test_render_settings_rejects_unsafe_id(self):
        with self.assertRaises(CapabilityDenied):
            render_disabled_plugins_settings(("no-at-sign",))

    def test_materialize_writes_owner_only_file_in_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            path = materialize_disabled_plugins_settings(
                ("firebase@claude-plugins-official",),
                worktree_root=worktree,
                task_id="TASK-2026-09-26-1507-4788a9cb",
                attempt_id="d0deadbeef",
            )
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(str(path.resolve()).startswith(str(worktree.resolve())))
            payload = json.loads(path.read_text())
            self.assertIs(
                payload["enabledPlugins"]["firebase@claude-plugins-official"], False
            )

    def test_materialize_refuses_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CapabilityDenied):
                materialize_disabled_plugins_settings(
                    (),
                    worktree_root=Path(tmp),
                    task_id="TASK-x",
                    attempt_id="d0",
                )


if __name__ == "__main__":
    unittest.main()
