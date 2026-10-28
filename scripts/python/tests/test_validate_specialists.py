from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.python.validate_specialists import (
    LEGACY_RUNTIME_HEADER,
    POLICY_HEADER,
    PROFILE_HEADER,
    RUNTIME_HEADER,
    TOOL_HEADER,
    Validator,
    blind_discovery_issues,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts/python/validate_specialists.py"
WRAPPER = REPO_ROOT / "bin/validate-specialists.sh"


def line(fields: list[str]) -> str:
    return "\t".join(fields) + "\n"


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.row = [
            "sample", "coding", "implementation", "low", "[]", "none",
            "codex", "codex.primary", "claude", "claude.backup", "claude",
            "claude.escalate", "escalation.signal.v1", "gemini", "gemini.review",
            "none", "none", "none", "throughput.never.v1",
            "failover.conservative.v1", "[]", "false", "[]", "[]", "[]",
            "Fixture notes.", "[]", "1.0", "false",
        ]
        self.runtime_header = RUNTIME_HEADER
        self.tool_rows: list[list[str]] = []
        self._write_static()
        self.flush()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _write_static(self) -> None:
        self.write("shared/registries/profiles.tsv", line(PROFILE_HEADER) + "".join([
            line(["claude.backup", "claude", "claude", "high", "none", "backup"]),
            line(["claude.escalate", "claude", "claude", "max", "none", "escalation"]),
            line(["codex.primary", "codex", "codex", "high", "none", "primary"]),
            line(["gemini.review", "gemini", "gemini", "high", "none", "review"]),
        ]))
        self.write("shared/registries/policies.tsv", line(POLICY_HEADER) + "".join([
            line(["escalation.safety_floor.v1", "escalation", "safety floor"]),
            line(["escalation.signal.v1", "escalation", "signal"]),
            line(["failover.conservative.v1", "failover", "conservative"]),
            line(["throughput.downshift_gated.v1", "throughput", "per-task gate"]),
            line(["throughput.never.v1", "throughput", "never"]),
        ]))
        self.write("shared/api-catalog.md", "# fixture\n")
        self.write("departments/coding/specialists/sample.md", """# sample

## Tools available to me

### MCPs

### Skills

## When to fan out

No peers.

## When to escalate

On ambiguity.

## What I do NOT do

No unrelated work.
""")
        self.write("model-lanes/gpt-codex/.codex/agents/sample.toml",
                   'name = "sample"\ndescription = "fixture"\n')

    def add_tool(self, name: str, *, lanes: str = "codex", state: str = "yes",
                 record_type: str = "mcp-tool") -> None:
        row = [name, "tool", record_type, "fixture", lanes, "invoke",
               state, "none", "fixture", "fixture",
               "hunting", "—", "any"]
        # Width is derived from TOOL_HEADER, not hardcoded. Adding purpose,
        # hunting_type and target_class to the registry left these rows at ten
        # fields against a thirteen-field header, and the resulting
        # `wrong-registry-column-count` cascaded: zero rows indexed as tools, so
        # every specialist tool reference became unresolvable and CI reported
        # seven failures whose single cause was one column count.
        self.tool_rows.append((row + ["—"] * len(TOOL_HEADER))[:len(TOOL_HEADER)])

    def flush(self) -> None:
        self.write("shared/specialist-runtime-map.tsv", line(self.runtime_header) + line(self.row))
        self.write("shared/registries/skill-tool-registry.tsv",
                   line(TOOL_HEADER) + "".join(line(row) for row in self.tool_rows))

    def result(self, strict_adapters: bool = False):
        self.flush()
        return Validator(self.root, expected_rows=1, strict_adapters=strict_adapters).run()


class SpecialistValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def issues(self) -> list[str]:
        findings, _summary, _code = self.fixture.result()
        return [issue for finding in findings for issue in finding.issues]

    def test_clean_fixture_passes_with_compatible_summary(self) -> None:
        findings, summary, code = self.fixture.result()
        self.assertEqual(code, 0)
        self.assertEqual(summary, "Total: 1  Passed: 1  Failed: 0  Warnings(non-fatal): 0")
        self.assertEqual([finding.status for finding in findings], ["pass"])

    def test_legacy_runtime_schema_defaults_optional_consult_false(self) -> None:
        self.fixture.runtime_header = LEGACY_RUNTIME_HEADER
        self.fixture.row = self.fixture.row[:-1]
        findings, _summary, code = self.fixture.result()
        self.assertEqual(code, 0)
        self.assertEqual([finding.status for finding in findings], ["pass"])

    def test_operator_model_consult_requires_boolean_when_present(self) -> None:
        self.fixture.row[-1] = "maybe"
        self.assertIn("invalid-operator-model-consult:maybe", self.issues())

    def test_empty_operator_model_consult_defaults_false(self) -> None:
        self.fixture.row[-1] = ""
        _findings, _summary, code = self.fixture.result()
        self.assertEqual(code, 0)

    def budget_slot(self, lane="codex", profile="codex.primary") -> None:
        self.fixture.row[16:19] = [lane, profile, "throughput.downshift_gated.v1"]

    def test_budget_slots_accept_all_active_lanes(self) -> None:
        for lane, profile in (("codex", "codex.primary"),
                              ("claude", "claude.backup"),
                              ("gemini", "gemini.review")):
            with self.subTest(lane=lane):
                self.budget_slot(lane, profile)
                self.assertEqual(self.fixture.result()[2], 0)

    def test_budget_slot_does_not_lower_role_safety_metadata(self) -> None:
        for safety in ("low", "medium", "high"):
            for tags in ("[]", "[live_target]"):
                with self.subTest(safety=safety, tags=tags):
                    self.budget_slot()
                    self.fixture.row[3:5] = [safety, tags]
                    self.fixture.row[12] = ("escalation.safety_floor.v1" if safety == "high"
                                            else "escalation.signal.v1")
                    self.assertEqual(self.fixture.result()[2], 0)
                    self.assertEqual(self.fixture.row[3:5], [safety, tags])
        # This is a static slot check; task permission still comes from the gate.
        self.fixture.row[12] = "escalation.signal.v1"
        self.assertIn("risk-requires-safety-floor", self.issues())

    def test_sensitive_tags_still_cannot_have_budget_slots(self) -> None:
        for tag in ("privacy", "financial", "dual_use"):
            with self.subTest(tag=tag):
                self.budget_slot()
                self.fixture.row[4] = f"[{tag}]"
                self.assertIn(f"sensitive-tag-forbids-throughput:{tag}", self.issues())

    def test_named_sensitive_exception_is_same_lane_and_tag_specific(self) -> None:
        policy = (REPO_ROOT / "shared/lane-policy.tsv").read_text(encoding="utf-8")
        self.fixture.write("shared/lane-policy.tsv", policy + line(
            ["throughput_exception", "privacy", "allow", "sample", "fixture"]))
        self.fixture.row[4] = "[privacy]"
        self.budget_slot("codex", "codex.primary")
        self.assertEqual(self.fixture.result()[2], 0)
        self.budget_slot("claude", "claude.backup")
        self.assertIn("sensitive-throughput-leaves-primary-lane:claude", self.issues())
        self.budget_slot("codex", "codex.primary")
        self.fixture.row[4] = "[financial, privacy]"
        self.assertIn("sensitive-tag-forbids-throughput:financial", self.issues())

    def test_heightened_roles_still_cannot_have_budget_slots(self) -> None:
        self.budget_slot()
        self.fixture.row[21] = "true"
        self.fixture.row[12] = "escalation.safety_floor.v1"
        self.assertIn("heightened-risk-forbids-throughput", self.issues())

    def test_budget_slot_profile_must_resolve_on_its_lane(self) -> None:
        self.budget_slot(profile="missing.profile")
        self.assertIn("unknown-profile:missing.profile", self.issues())
        self.budget_slot("gemini", "codex.primary")
        self.assertIn("profile-lane-mismatch:gemini:codex.primary", self.issues())
        self.budget_slot("grok", "codex.primary")
        self.assertIn("invalid-throughput-lane:grok", self.issues())

    def test_budget_slot_sentinels_and_policy_must_agree(self) -> None:
        for lane, profile in (("none", "none"), ("none", "codex.primary"),
                              ("codex", "none")):
            with self.subTest(lane=lane, profile=profile):
                self.budget_slot(lane, profile)
                self.assertIn("gated-throughput-requires-profile", self.issues())
        self.budget_slot()
        self.fixture.row[18] = "throughput.never.v1"
        self.assertIn("never-throughput-requires-none", self.issues())
        self.fixture.row[18] = "escalation.signal.v1"
        self.assertIn("invalid-throughput-policy:escalation.signal.v1", self.issues())

    def test_retired_lane_has_no_policy_or_adapter_requirement(self) -> None:
        validator = Validator(REPO_ROOT)
        for specialist in ("summarizer", "kestrel", "sample"):
            self.assertFalse(validator.primary_allowed("kimi", specialist))
        self.assertEqual(validator.vocabulary("route_lane"),
                         {"codex", "claude", "gemini", "grok"})
        self.assertFalse(validator.policy_rows("adapter_template", "kimi"))
        self.assertNotIn("kimi", validator.vocabulary("throughput_lane"))
        self.fixture.row[6] = "kimi"
        self.assertIn("primary-lane-forbidden:kimi:sample", self.issues())
        self.assertIn("invalid-routing-lane:kimi", self.issues())

    def test_current_map_has_no_kimi_routes_and_independent_reviews(self) -> None:
        validator = Validator(REPO_ROOT)
        rows = [dict(zip(RUNTIME_HEADER, row))
                for row in validator.normalized_runtime_rows()]
        for row in rows:
            with self.subTest(specialist=row["specialist"]):
                for slot in ("primary", "backup", "throughput", "escalate", "review"):
                    self.assertNotEqual(row[slot + "_lane"], "kimi")
                    self.assertFalse(row[slot + "_profile"].startswith("kimi."))
                self.assertNotEqual(row["primary_lane"], row["review_lane"])
                if row["throughput_profile"] == "none":
                    self.assertEqual(row["throughput_lane"], "none")
                    self.assertEqual(row["throughput_policy"], "throughput.never.v1")

    def test_missing_tool_is_fatal(self) -> None:
        self.fixture.row[23] = "[missing-tool]"
        self.assertIn("unresolved-tool-reference:required:missing-tool", self.issues())

    def test_retirement_accepts_absent_tools_but_rejects_dormant_profiles(self) -> None:
        # This fixture has no retired lane adapters or retired arsenal tools.
        self.fixture.add_tool("arxiv_search", lanes="all")
        self.fixture.row[23] = "[arxiv_search]"
        self.assertEqual(self.fixture.result()[2], 0)
        profile = self.fixture.root / "shared/registries/profiles.tsv"
        profile.write_text(profile.read_text() + line(
            ["kimi.dormant", "kimi", "legacy", "high", "none", "dormant"]))
        self.assertEqual(self.fixture.result()[2], 1)
        self.assertIn("invalid-registry-value:kimi.dormant:kimi", self.issues())
        for name in ("xai_search", "perplexity_search", "firecrawl_scrape",
                     "firecrawl_crawl", "firecrawl_parse"):
            with self.subTest(tool=name):
                self.fixture.row[23] = f"[{name}]"
                self.assertIn(f"unresolved-tool-reference:required:{name}", self.issues())
        self.fixture.row[23] = "[arxiv_search]"
        self.fixture.row[8:10] = ["kimi", "kimi.dormant"]
        self.assertIn("invalid-routing-lane:kimi", self.issues())

    def test_firecrawl_plugin_operation_validation_is_not_arsenal_requirement(self) -> None:
        self.fixture.add_tool("firecrawl", lanes="claude")
        self.fixture.row[24] = "[firecrawl]"
        self.fixture.row[25] = "operations[firecrawl]=scrape; plugin family."
        self.assertEqual(self.fixture.result()[2], 0)
        self.fixture.row[25] = "operations[firecrawl]=invented; plugin family."
        self.assertIn("unknown-operation:firecrawl:invented", self.issues())

    def test_required_no_stale_and_retired_tools_are_fatal(self) -> None:
        cases = (("no", "mcp-tool"), ("stale", "mcp-tool"), ("yes", "retired-mcp"))
        for index, (state, record_type) in enumerate(cases):
            with self.subTest(state=state, record_type=record_type):
                fixture = Fixture(Path(self.temp.name) / str(index))
                fixture.add_tool("bad-tool", state=state, record_type=record_type)
                fixture.row[23] = "[bad-tool]"
                findings, _summary, code = fixture.result()
                all_issues = [item for finding in findings for item in finding.issues]
                self.assertEqual(code, 1)
                self.assertTrue(any(issue.startswith("unusable-required-tool:bad-tool")
                                    for issue in all_issues))

    def test_required_wrong_lane_is_fatal(self) -> None:
        self.fixture.add_tool("wrong-lane", lanes="local")
        self.fixture.row[23] = "[wrong-lane]"
        self.assertIn("required-tool-wrong-lane:wrong-lane:local", self.issues())

    def test_ambiguous_namespaced_operation_is_fatal(self) -> None:
        self.fixture.row[23] = "[family:operation]"
        self.assertIn("ambiguous-namespaced-tool:required:family:operation", self.issues())

    def test_elevenlabs_operations_cannot_be_parented_to_media_wrapper(self) -> None:
        self.fixture.add_tool("chrono-media-studio", lanes="all")
        self.fixture.row[24] = "[chrono-media-studio]"
        self.fixture.row[25] = (
            "operations[chrono-media-studio]=elevenlabs__text_to_speech; "
            "unverified and unavailable."
        )
        self.assertIn("unknown-operation-root:chrono-media-studio", self.issues())

    def test_registered_colon_identifier_remains_valid(self) -> None:
        self.fixture.add_tool("plugin:github:github", lanes="all")
        self.fixture.row[23] = "[plugin:github:github]"
        _findings, _summary, code = self.fixture.result()
        self.assertEqual(code, 0)

    def test_partial_preferred_requires_degradation(self) -> None:
        self.fixture.add_tool("partial-tool", state="partial")
        self.fixture.row[24] = "[partial-tool]"
        self.assertIn("partial-preferred-missing-degradation:partial-tool", self.issues())
        self.fixture.row[25] = "Partial tool falls back to manual work when unavailable."
        _findings, _summary, code = self.fixture.result()
        self.assertEqual(code, 0)

    def test_unrelated_degradation_keyword_does_not_cover_partial_tool(self) -> None:
        self.fixture.add_tool("partial-tool", state="partial")
        self.fixture.row[24] = "[partial-tool]"
        self.fixture.row[25] = "An unrelated compliance control is optional."
        self.assertIn("partial-preferred-missing-degradation:partial-tool", self.issues())

    def test_stale_route_name_does_not_match_inside_larger_identifier(self) -> None:
        mode = self.fixture.root / "shared/modes/project.md"
        mode.parent.mkdir(parents=True, exist_ok=True)
        mode.write_text("vibecoding-check delegates to legacy-code-auditor-v2.\n")
        _findings, _summary, code = self.fixture.result()
        self.assertEqual(code, 0)

    def test_registry_shape_duplicate_sort_and_foreign_key_failures(self) -> None:
        profile = self.fixture.root / "shared/registries/profiles.tsv"
        rows = profile.read_text().splitlines()
        profile.write_text("\n".join([rows[0], rows[2], rows[1], *rows[3:]]) + "\n")
        self.fixture.row[7] = "missing.profile"
        self.fixture.add_tool("duplicate")
        self.fixture.add_tool("duplicate")
        issues = self.issues()
        self.assertIn("registry-not-sorted", issues)
        self.assertIn("unknown-profile:missing.profile", issues)
        self.assertIn("duplicate-registry-ids:duplicate", issues)

    def test_strict_adapter_mode_fails_missing_adapter(self) -> None:
        (self.fixture.root / "model-lanes/gpt-codex/.codex/agents/sample.toml").unlink()
        findings, _summary, code = self.fixture.result(strict_adapters=True)
        self.assertEqual(code, 1)
        self.assertIn("missing-codex-agent-adapter:sample",
                      [issue for finding in findings for issue in finding.issues])

    def test_cli_emits_jsonl_and_stderr_summary(self) -> None:
        self.fixture.flush()
        completed = subprocess.run(
            [sys.executable, os.fspath(SCRIPT), "--root", os.fspath(self.fixture.root),
             "--expected-runtime-rows", "1", "--quiet"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        records = [json.loads(item) for item in completed.stdout.splitlines()]
        expected_file = (self.fixture.root /
                         "departments/coding/specialists/sample.md").resolve()
        self.assertEqual(records, [{"file": os.fspath(expected_file),
                                    "status": "pass", "issues": []}])
        self.assertEqual(completed.stderr,
                         "\nTotal: 1  Passed: 1  Failed: 0  Warnings(non-fatal): 0\n")

    def test_production_registry_has_zero_warning_budget(self) -> None:
        completed = subprocess.run(
            [sys.executable, os.fspath(SCRIPT), "--root", os.fspath(REPO_ROOT)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Warnings(non-fatal): 0", completed.stderr)
        self.assertFalse(any(json.loads(line)["status"] == "warn"
                             for line in completed.stdout.splitlines()))

    def test_codex_registry_indexes_exact_meaningful_subcommands(self) -> None:
        path = REPO_ROOT / "shared/registries/skill-tool-registry.tsv"
        with path.open(encoding="utf-8", newline="") as handle:
            records = {
                row["name"]: row for row in csv.DictReader(handle, delimiter="\t")
            }
        expected = {
            "codex apply",
            "codex debug app-server send-message-v2",
            "codex debug models",
            "codex debug prompt-input",
            "codex doctor",
            "codex exec",
            "codex fork",
            "codex mcp",
            "codex mcp-server",
            "codex resume",
            "codex review",
            "codex sandbox",
        }
        self.assertTrue(expected <= records.keys())
        self.assertNotIn("codex mcp/mcp-server", records)
        self.assertNotIn("codex resume/fork", records)
        for name in expected:
            with self.subTest(name=name):
                self.assertEqual(records[name]["type"], "native-cli-subcommand")
                self.assertIn(
                    records[name]["verified_state"],
                    {"yes", "partial", "needs-research"},
                )

    def test_wrapper_splits_only_live_existence_from_generic_ci(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory) / "bin"
            fake_bin.mkdir()
            call_log = Path(directory) / "calls.log"
            python_stub = fake_bin / "python3"
            python_stub.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$*\" >> \"${VALIDATOR_CALL_LOG:?}\"\n",
                encoding="utf-8",
            )
            python_stub.chmod(0o755)
            base_env = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "VALIDATOR_CALL_LOG": os.fspath(call_log),
                "VAULT_ROOT": os.fspath(REPO_ROOT),
            }
            base_env.pop("SQUAD_CI_HOST_INDEPENDENT", None)

            full = subprocess.run(
                ["bash", os.fspath(WRAPPER), "--quiet"],
                env=base_env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(full.returncode, 0, full.stderr)
            full_calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                full_calls[-1],
                f"{REPO_ROOT}/scripts/python/validate_capability_homes.py "
                f"--repo-root {REPO_ROOT}",
            )
            self.assertNotIn("--only", full_calls[-1])

            call_log.write_text("", encoding="utf-8")
            ci = subprocess.run(
                ["bash", os.fspath(WRAPPER), "--quiet"],
                env={**base_env, "SQUAD_CI_HOST_INDEPENDENT": "1"},
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(ci.returncode, 0, ci.stderr)
            ci_calls = call_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                ci_calls[-1],
                f"{REPO_ROOT}/scripts/python/validate_capability_homes.py "
                f"--repo-root {REPO_ROOT} --only "
                "boundary,parity,index,source,required",
            )
            self.assertIn("host-independent CI subset", ci.stderr)


class BlindDiscoveryFrontmatterTests(unittest.TestCase):
    """The blindness marker is the one brief field a typo can silently undo.

    `dispatch_context_builder` fails closed on a brief it cannot parse, so a
    bad *value* costs a role its memory and nothing worse. A misspelled *key*
    is the dangerous one: the brief parses, the key the floor looks for is
    absent, and a rediscovery scout quietly keeps its memory. Nothing else in
    this validator reads specialist frontmatter, so this is the only place
    that can say so out loud.
    """

    def frontmatter(self, *lines: str) -> str:
        return "---\nspecialist: candidate\n" + "".join(
            f"{line}\n" for line in lines
        ) + "---\n\n# brief\n"

    def test_a_correct_declaration_is_accepted(self):
        for value in ("true", "false"):
            with self.subTest(value=value):
                self.assertEqual(
                    blind_discovery_issues(
                        self.frontmatter(f"blind_discovery: {value}")
                    ),
                    [],
                )

    def test_an_absent_declaration_is_accepted(self):
        # 61 of the 68 shipped briefs say nothing, and that is a real answer.
        self.assertEqual(blind_discovery_issues(self.frontmatter("tags: []")), [])

    def test_a_misspelled_key_is_rejected(self):
        for key in ("blind-discovery", "Blind_Discovery", "BLINDDISCOVERY"):
            with self.subTest(key=key):
                self.assertEqual(
                    blind_discovery_issues(self.frontmatter(f"{key}: true")),
                    [f"misspelled-blind-discovery-key:{key}"],
                )

    def test_a_non_boolean_value_is_rejected(self):
        for value, reported in (("yes", "yes"), ("", "(empty)"), ("[true]", "[true]")):
            with self.subTest(value=value):
                self.assertEqual(
                    blind_discovery_issues(
                        self.frontmatter(f"blind_discovery: {value}".rstrip())
                    ),
                    [f"invalid-blind-discovery:{reported}"],
                )

    def test_a_duplicated_declaration_is_rejected(self):
        self.assertEqual(
            blind_discovery_issues(
                self.frontmatter("blind_discovery: true", "blind_discovery: false")
            ),
            ["duplicate-blind-discovery"],
        )

    def test_the_body_is_not_scanned(self):
        # Prose after the frontmatter that mentions the key is documentation,
        # not a declaration, and must not be reported as a misspelling.
        text = self.frontmatter("tags: []") + "\nWe honour blind-discovery here.\n"
        self.assertEqual(blind_discovery_issues(text), [])

    def test_the_shipped_briefs_all_pass_this_gate(self):
        for pattern in ("departments/*/specialists/*.md", "shared/specialists/*.md"):
            for brief in REPO_ROOT.glob(pattern):
                with self.subTest(brief=brief.name):
                    self.assertEqual(
                        blind_discovery_issues(brief.read_text(encoding="utf-8")), []
                    )


if __name__ == "__main__":
    unittest.main()
