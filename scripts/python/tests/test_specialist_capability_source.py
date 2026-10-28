from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/python"))
import gen_runtime_tool_summary  # noqa: E402
from specialist_capability_source import (  # noqa: E402
    LANES,
    SURFACE_SCHEMA,
    CapabilityRef,
    CapabilitySourceError,
    SOURCE_RELATIVE,
    accepted_source_sha256s,
    available_arrays,
    load_source,
    role_surface_payload,
    role_surface_sha256,
    source_sha256,
)

VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_capability_homes_source_tests",
    ROOT / "scripts/python/validate_capability_homes.py",
)
validator = importlib.util.module_from_spec(VALIDATOR_SPEC)
assert VALIDATOR_SPEC.loader is not None
VALIDATOR_SPEC.loader.exec_module(validator)

REGISTRY_SPEC = importlib.util.spec_from_file_location(
    "lane_adapter_registry_source_tests",
    ROOT / "scripts/python/lane_adapter_registry.py",
)
registry = importlib.util.module_from_spec(REGISTRY_SPEC)
assert REGISTRY_SPEC.loader is not None
REGISTRY_SPEC.loader.exec_module(registry)


class SpecialistCapabilitySourceTests(unittest.TestCase):
    def test_retired_lane_entry_is_rejected(self) -> None:
        payload = json.loads((ROOT / SOURCE_RELATIVE).read_text(encoding="utf-8"))
        retired = json.loads(json.dumps(next(e for e in payload["entries"] if e["lane"] == "claude")))
        retired["lane"] = "kimi"
        payload["entries"].append(retired)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / SOURCE_RELATIVE.name
            source.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(CapabilitySourceError, "invalid lane 'kimi'"):
                load_source(ROOT, source)

    def test_runtime_summary_maps_exactly_the_four_lanes(self) -> None:
        self.assertEqual(set(gen_runtime_tool_summary.LANE_NAMES.values()), set(LANES))

    def test_public_prior_art_operation_promise_is_rejected(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "public_operation_control", ROOT / "model-lanes/check_public_plugin_capabilities.py"
        )
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "model-lanes", root / "model-lanes")
            for relative in ("shared/specialist-runtime-map.tsv",
                             "tools/export/policy/path-policy.json"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, path)
            planted = "model-lanes/gpt-codex/.codex/agents/operation-control.toml"
            (root / planted).write_text('tools = ["prior_art_check"]\n')
            issues = check.audit(root)
            self.assertEqual(
                {issue["identifier"] for issue in issues if issue["path"] == planted},
                {"chrono-dedup"},
            )
            result = subprocess.run(
                [sys.executable, "-B", str(ROOT / "model-lanes/check_public_plugin_capabilities.py"),
                 "--repo-root", str(root)], capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertTrue(any(issue["path"] == planted
                                for issue in json.loads(result.stdout)["diagnostics"]))

    def test_projection_compatibility_hashes_include_current_and_explicit_prior(self) -> None:
        _entries, payload = load_source(ROOT)
        accepted = accepted_source_sha256s(ROOT, payload)
        self.assertIn(source_sha256(ROOT), accepted)
        self.assertTrue(
            set(payload["projection_compatible_source_sha256s"]) < accepted
        )

    def test_role_surface_payload_is_canonical_and_splits_brokered_mcps(self) -> None:
        entry = {
            "specialist": "example",
            "lane": "claude",
            "coverage": "partial",
            "limitations": ("metadata is not part of the surface",),
            "skills": (
                CapabilityRef("z-skill", "preferred", "available", "lane-inventory"),
                CapabilityRef("a-skill", "required", "installed-skill-root", "installed-skill-root"),
                CapabilityRef("draft", "preferred", "authored:stub", "shared-skills:stub"),
            ),
            "tools": (
                CapabilityRef("z-tool", "preferred", "available", "host-PATH"),
                CapabilityRef("missing", "preferred", "uninstalled", "host-PATH:absent"),
                CapabilityRef("a-tool", "required", "available", "host-PATH"),
            ),
            "mcps": (
                CapabilityRef("lead:research", "preferred", "available", "lane-inventory"),
                CapabilityRef("vault", "required", "available", "lane-inventory"),
            ),
        }

        self.assertEqual(
            role_surface_payload(entry),
            {
                "schema": SURFACE_SCHEMA,
                "lane": "claude",
                "skills": ["a-skill", "z-skill"],
                "tools": ["a-tool", "z-tool"],
                "mcps": ["vault"],
                "brokered_mcps": ["research"],
            },
        )

    def test_role_surface_hash_ignores_metadata_but_binds_lane_and_route(self) -> None:
        direct = {
            "specialist": "alpha",
            "lane": "claude",
            "coverage": "full",
            "limitations": (),
            "skills": (),
            "tools": (),
            "mcps": (
                CapabilityRef("vault", "required", "available", "lane-inventory"),
                CapabilityRef("ignored-a", "preferred", "uninstalled", "lane-not-wired"),
            ),
        }
        metadata_changed = {
            **direct,
            "specialist": "beta",
            "coverage": "partial",
            "limitations": ("different",),
            "mcps": (
                CapabilityRef("ignored-b", "preferred", "probe-failed", "pending-reprobe"),
                CapabilityRef("vault", "preferred", "available", "verified-registry:claude-mcp"),
            ),
        }
        brokered = {
            **direct,
            "mcps": (CapabilityRef("lead:vault", "required", "available", "lane-inventory"),),
        }

        self.assertEqual(role_surface_sha256(direct), role_surface_sha256(metadata_changed))
        self.assertNotEqual(role_surface_sha256(direct), role_surface_sha256({**direct, "lane": "gpt-codex"}))
        self.assertNotEqual(role_surface_sha256(direct), role_surface_sha256(brokered))

    def test_source_covers_every_routed_pair_and_every_primary_is_full(self) -> None:
        entries, payload = load_source(ROOT)
        rows = validator.runtime_rows(ROOT)
        expected = {
            (specialist, lane)
            for specialist, row in rows.items()
            for lane in validator.routed_lanes(row)
        }
        self.assertEqual(set(entries), expected)
        self.assertEqual(
            len(entries),
            sum(len(validator.routed_lanes(row)) for row in rows.values()),
        )
        self.assertEqual(
            sum(entry["coverage"] == "full" for entry in entries.values()),
            len(rows),
        )
        self.assertEqual(
            sum("primary_requirements" in entry for entry in entries.values()), 0
        )
        self.assertEqual(validator.source_coverage_diagnostics(rows, entries), [])
        self.assertEqual(
            validator.required_primary_diagnostics(rows, entries, root=ROOT), []
        )

    def test_migrated_primary_capabilities_and_gemini_budget_adapter(self) -> None:
        entries, _payload = load_source(ROOT)
        for specialist, lane in (("detection-engineer", "gpt-codex"),
                                 ("kestrel", "claude"), ("summarizer", "gemini")):
            with self.subTest(specialist=specialist):
                self.assertEqual(entries[specialist, lane]["coverage"], "full")
                self.assertIn("chrono-vault", available_arrays(entries, specialist, lane)["mcps"])
        self.assertIn("detection-as-code",
                      available_arrays(entries, "detection-engineer", "gpt-codex")["skills"])
        self.assertEqual(entries["detection-engineer", "claude"]["coverage"], "partial")
        self.assertEqual(entries["synthesizer", "gemini"]["coverage"], "partial")
        for name in ("summarizer", "synthesizer"):
            adapter = ROOT / "model-lanes/gemini/.gemini/agents" / f"{name}.md"
            registry.validate_adapter_file(ROOT, "gemini", adapter)
        self.assertFalse(any(lane == "kimi" for _, lane in entries))

    def test_primary_coverage_check_detects_regression_after_migration(self) -> None:
        entries, _payload = load_source(ROOT)
        rows = validator.runtime_rows(ROOT)
        changed = dict(entries)
        changed["detection-engineer", "gpt-codex"] = {
            **entries["detection-engineer", "gpt-codex"], "coverage": "partial"}
        issues = validator.source_coverage_diagnostics(rows, changed)
        self.assertTrue(any(item["identifier"] == "detection-engineer:gpt-codex"
                            and "primary lane must declare full coverage" in item["message"]
                            for item in issues), issues)
        del changed["summarizer", "gemini"]
        issues = validator.source_coverage_diagnostics(rows, changed)
        self.assertTrue(any(item["identifier"] == "summarizer:gemini" for item in issues), issues)

    def test_required_installed_skill_root_is_usable(self) -> None:
        payload = json.loads((ROOT / SOURCE_RELATIVE).read_text(encoding="utf-8"))
        entry = next(item for item in payload["entries"] if item["skills"])
        skill = entry["skills"][0]
        skill.update(
            requirement="required",
            availability="installed-skill-root",
            evidence="installed-skill-root",
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            entries, _payload = load_source(ROOT, path)

        self.assertIn(
            skill["id"],
            available_arrays(entries, entry["specialist"], entry["lane"])["skills"],
        )

    def test_operation_provider_relations_are_reciprocal_and_assigned(self) -> None:
        entries, payload = load_source(ROOT)
        servers = {
            item["id"]: set(item["provides"])
            for item in payload["servers"]
        }
        assigned_operations = {
            server_id: set()
            for server_id in servers
            if not server_id.startswith("lead:")
        }
        for entry in entries.values():
            assigned = {ref.identifier for ref in entry["mcps"]}
            for ref in entry["tools"]:
                if not ref.provided_by:
                    continue
                self.assertIn(ref.identifier, servers[ref.provided_by])
                self.assertIn(ref.provided_by, assigned)
                if not ref.provided_by.startswith("lead:"):
                    assigned_operations[ref.provided_by].add(ref.identifier)
        # Every directly callable provider operation must have at least one
        # current routed consumer. This closes both directions without copying a
        # roster-dependent declaration count into the test.
        direct_server_operations = {
            server_id: operations
            for server_id, operations in servers.items()
            if not server_id.startswith("lead:")
        }
        if "public_projection" in payload:
            # The public catalogue deliberately retains private operation
            # names so S2 still detects a planted promise. Only providers
            # withheld by the actual export policy may lack assignments.
            sys.path.insert(0, str(ROOT / "tools/export"))
            from path_policy import load_policy
            policy = load_policy(ROOT / "tools/export/policy/path-policy.json")
            declared_providers = set(servers) | {
                ref.identifier for entry in entries.values() for ref in entry["mcps"]
            }
            withheld = {
                identifier for identifier in declared_providers
                if any(policy.classify(f"plugins/{identifier.removeprefix('lead:')}/{suffix}")
                       != "public" for suffix in (".claude-plugin/plugin.json", "mcp_server.py"))
            }
            self.assertEqual(set(payload["public_projection"]["withheld_providers"]), withheld)
            for identifier in withheld:
                if identifier in direct_server_operations:
                    self.assertEqual(assigned_operations[identifier], set())
                    direct_server_operations[identifier] = set()
        self.assertEqual(assigned_operations, direct_server_operations)

    def test_validator_fails_closed_on_runtime_projection_drift(self) -> None:
        entries, _payload = load_source(ROOT)
        rows = validator.runtime_rows(ROOT)
        with mock.patch.object(validator, "render_runtime_map", return_value="drift\n"):
            issues = validator.required_primary_diagnostics(
                rows,
                entries,
                root=ROOT,
            )
        self.assertIn("runtime-tool-summary", {issue["check"] for issue in issues})

    @unittest.skipUnless(
        (ROOT / "shared/registries/skill-tool-registry.tsv").is_file(),
        "private integration: provider closure requires the withheld skill-tool registry",
    )
    def test_validator_fails_closed_when_provider_registry_record_is_missing(self) -> None:
        entries, _payload = load_source(ROOT)
        rows = validator.runtime_rows(ROOT)
        records = validator.shared_registry_records(ROOT)
        records.pop("chrono-research-arsenal")
        with mock.patch.object(
            validator,
            "shared_registry_records",
            return_value=records,
        ):
            issues = validator.required_primary_diagnostics(
                rows,
                entries,
                root=ROOT,
            )
        self.assertIn("provider-closure", {issue["check"] for issue in issues})

    def test_tool_backed_host_path_absent_cannot_be_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "specialist-lane-capabilities/v1",
                        "version": 1,
                        "entries": [
                            {
                                "specialist": "x",
                                "lane": "claude",
                                "coverage": "full",
                                "limitations": [],
                                "skills": [],
                                "tools": [
                                    {
                                        "id": "missing",
                                        "requirement": "required",
                                        "availability": "uninstalled",
                                        "evidence": "host-PATH:absent",
                                    }
                                ],
                                "mcps": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(CapabilitySourceError, "must be preferred"):
                load_source(ROOT, path)

    def test_required_tools_are_satisfied_only_by_primary_available_source(self) -> None:
        entries, _payload = load_source(ROOT)
        rows = validator.runtime_rows(ROOT)
        self.assertEqual(
            validator.required_primary_diagnostics(rows, entries, namespace="security"),
            [],
        )
        self.assertTrue(
            {"chrome-devtools", "playwright"}.issubset(
                set(available_arrays(entries, "scout", "claude")["mcps"])
            )
        )
        self.assertEqual(
            {lane for specialist, lane in entries if specialist == "scout"},
            {"claude", "gpt-codex"},
        )
        self.assertEqual(entries[("scout", "gpt-codex")]["coverage"], "partial")
        frontend = entries[("frontend-engineer", "gpt-codex")]
        self.assertEqual(
            tuple(
                ref.identifier
                for ref in frontend["mcps"]
                if ref.requirement == "required"
            ),
            ("chrome-devtools", "playwright"),
        )

    @unittest.skipUnless(
        (ROOT / "shared/registries/skill-tool-registry.tsv").is_file(),
        "private integration: authored skill registration requires the withheld skill-tool registry",
    )
    def test_security_capability_skills_are_active_authored_and_registered(self) -> None:
        expected = {
            "detection-as-code",
            "program-rubric-lookup",
            "forensic-timeline-authoring",
            "incident-response-runbook",
            "data-flow-trace",
            "pre-audit-threat-model",
            "security-ownership-map",
            "security-threat-model",
        }
        with (ROOT / "shared/registries/skill-tool-registry.tsv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = {row["name"]: row for row in csv.DictReader(handle, delimiter="\t")}
        self.assertTrue(expected.issubset(rows))
        for name in expected:
            self.assertEqual(rows[name]["type"], "authored-pattern-doc")
            self.assertEqual(rows[name]["verified_state"], "authored")
            self.assertIn("status: authored", (ROOT / rows[name]["path_or_source"]).read_text(encoding="utf-8"))

    def test_adapter_render_is_deterministic_and_preserves_gemini_native_tools(self) -> None:
        first = registry.render_adapter(ROOT, "gemini", "scout")
        second = registry.render_adapter(ROOT, "gemini", "scout")
        self.assertEqual(first, second)
        self.assertIn('tools: ["read_file"', first)
        self.assertIn(f"capability_source: {SOURCE_RELATIVE.as_posix()}", first)
        self.assertNotIn("skills:", first)

    def test_projection_roundtrip_preserves_unrelated_adapter_content(self) -> None:
        codex = (
            'name = "frontend_engineer"\n'
            'sandbox_mode = "workspace-write"\n'
            '# curated-policy: keep-me\n'
            'developer_instructions = """Canonical specialist instructions live at '
            '`departments/coding/specialists/frontend-engineer.md`."""\n'
        )
        first = registry.upsert_capability_projection(
            ROOT, "gpt-codex", "frontend-engineer", codex
        )
        second = registry.upsert_capability_projection(
            ROOT, "gpt-codex", "frontend-engineer", first
        )
        self.assertEqual(first, second)
        self.assertIn('sandbox_mode = "workspace-write"', second)
        self.assertIn("# curated-policy: keep-me", second)
        self.assertEqual(second.count(registry.PROJECTION_BEGIN), 1)

        gemini = (
            "---\nname: bounty-researcher\n"
            'tools: ["read_file","run_shell_command"]\n'
            "---\n\nCanonical specialist instructions live at "
            "`departments/research/specialists/bounty-researcher.md`.\n"
        )
        projected = registry.upsert_capability_projection(
            ROOT, "gemini", "bounty-researcher", gemini
        )
        self.assertIn('tools: ["read_file","run_shell_command"]', projected)
        self.assertIn(
            'capability_mcps: ["chrono-vault","sequential-thinking"]',
            projected,
        )
        self.assertNotIn("capability_tools:", projected)
        self.assertEqual(
            projected,
            registry.upsert_capability_projection(
                ROOT, "gemini", "bounty-researcher", projected
            ),
        )

    def test_codex_adapter_render_pins_no_reasoning_effort(self) -> None:
        # `model_reasoning_effort` is a dispatch-time (route-tier) decision applied
        # at launch via `-c model_reasoning_effort=<profile effort>`. A static pin in
        # the codex adapter would shadow the selected profile (e.g. ultra/max), so a
        # fresh render must never emit it, while unrelated policy (sandbox_mode) stays.
        rendered = registry.render_adapter(ROOT, "gpt-codex", "devops-engineer")
        self.assertNotIn("model_reasoning_effort", rendered)
        self.assertIn('sandbox_mode = "workspace-write"', rendered)

    def test_codex_projection_upsert_strips_legacy_reasoning_effort(self) -> None:
        codex_with_pin = (
            'name = "frontend_engineer"\n'
            'description = "x"\n'
            'model_reasoning_effort = "high"\n'
            'sandbox_mode = "workspace-write"\n'
            "# BEGIN SPECIALIST CAPABILITY PROJECTION\n"
            "# END SPECIALIST CAPABILITY PROJECTION\n"
            'developer_instructions = """Canonical specialist instructions live at '
            '`departments/coding/specialists/frontend-engineer.md`."""\n'
        )
        first = registry.upsert_capability_projection(
            ROOT, "gpt-codex", "frontend-engineer", codex_with_pin
        )
        self.assertNotIn("model_reasoning_effort", first)
        self.assertIn('sandbox_mode = "workspace-write"', first)
        # Idempotent: a second pass on already-clean text keeps it clean.
        second = registry.upsert_capability_projection(
            ROOT, "gpt-codex", "frontend-engineer", first
        )
        self.assertEqual(first, second)

    def test_committed_codex_adapters_pin_no_reasoning_effort(self) -> None:
        import re
        import tomllib

        pin = re.compile(r"(?m)^\s*model_reasoning_effort\s*=")
        adapters = sorted(
            (ROOT / "model-lanes/gpt-codex/.codex/agents").glob("*.toml")
        )
        self.assertTrue(adapters, "no committed codex adapters found")
        offenders = []
        for adapter in adapters:
            text = adapter.read_text(encoding="utf-8")
            data = tomllib.loads(text)
            if pin.search(text) or "model_reasoning_effort" in data:
                offenders.append(adapter.name)
        self.assertEqual(offenders, [], f"codex adapters pin effort: {offenders}")

    def test_committed_codex_overlays_pin_no_reasoning_effort(self) -> None:
        # One fact, one home: effort is a route-tier launch decision (profiles.tsv),
        # never restated in a static role overlay. A stale overlay copy ages
        # independently of the profile registry, so the gpt-codex overlays must not
        # carry model_reasoning_effort.
        import json

        overlays = sorted(
            (ROOT / "shared/lane-role-overlay/v1/gpt-codex").glob("*.json")
        )
        self.assertTrue(overlays, "no gpt-codex role overlays found")
        offenders = [
            overlay.name
            for overlay in overlays
            if "model_reasoning_effort"
            in json.loads(overlay.read_text(encoding="utf-8")).get("fields", {})
        ]
        self.assertEqual(offenders, [], f"overlays pin effort: {offenders}")

    def test_nmap_is_explicitly_preferred_and_available(self) -> None:
        entries, _payload = load_source(ROOT)
        nmap = next(ref for ref in entries[("scout", "claude")]["tools"] if ref.identifier == "nmap")
        self.assertEqual(nmap.requirement, "preferred")
        self.assertEqual(nmap.availability, "available")
        self.assertIn("nmap", available_arrays(entries, "scout", "claude")["tools"])

    def test_arxiv_wrapper_survives_while_firecrawl_is_absent_on_every_lane(self) -> None:
        entries, _payload = load_source(ROOT)
        rows = validator.runtime_rows(ROOT)
        self.assertEqual({lane for _, lane in entries}, set(LANES))
        for (specialist, lane), entry in entries.items():
            with self.subTest(specialist=specialist, lane=lane):
                self.assertNotIn("firecrawl", {ref.identifier for ref in entry["tools"]})
                self.assertNotIn("firecrawl", {ref.identifier for ref in entry["mcps"]})
        gemini_wrapper_consumers = []
        for specialist, row in sorted(rows.items()):
            if "gemini" not in validator.routed_lanes(row):
                continue
            entry = entries[(specialist, "gemini")]
            wrapper = next(
                (
                    ref
                    for ref in entry["tools"]
                    if ref.identifier == "arxiv_search"
                ),
                None,
            )
            if wrapper is None:
                continue
            gemini_wrapper_consumers.append(specialist)
            self.assertEqual(wrapper.availability, "mcp-operation")
            self.assertEqual(wrapper.provided_by, "chrono-research-arsenal")
            self.assertIn(
                "chrono-research-arsenal",
                {ref.identifier for ref in entry["mcps"]},
            )
        self.assertGreater(
            len(gemini_wrapper_consumers),
            0,
            "no current Gemini-routed specialist consumes arxiv_search",
        )

    def test_four_lane_source_without_retired_arsenal_operations(self) -> None:
        self.assertEqual(set(LANES), {"gpt-codex", "claude", "gemini", "grok"})
        payload = json.loads((ROOT / SOURCE_RELATIVE).read_text(encoding="utf-8"))
        retired = {"xai_search", "perplexity_search", "firecrawl_scrape",
                   "firecrawl_crawl", "firecrawl_parse"}
        payload["entries"] = [entry for entry in payload["entries"] if entry["lane"] in LANES]
        for server in payload["servers"]:
            server["provides"] = [name for name in server["provides"] if name not in retired]
        for entry in payload["entries"]:
            entry["tools"] = [ref for ref in entry["tools"] if ref["id"] not in retired]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / SOURCE_RELATIVE.name
            source.write_text(json.dumps(payload), encoding="utf-8")
            entries, _ = load_source(ROOT, source)
            self.assertTrue(entries)
            self.assertFalse(any(ref.identifier in retired
                                 for entry in entries.values() for ref in entry["tools"]))
            arxiv = next(ref for entry in entries.values() for ref in entry["tools"]
                         if ref.identifier == "arxiv_search")
            self.assertEqual(arxiv.provided_by, "chrono-research-arsenal")
            # Retiring other operations must not bypass the reciprocal provider check.
            server = next(server for server in payload["servers"]
                          if server["id"] == arxiv.provided_by)
            server["provides"].remove("arxiv_search")
            source.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(CapabilitySourceError, "not reciprocal"):
                load_source(ROOT, source)

    def test_pending_and_failed_capabilities_are_tracked_but_not_projected(self) -> None:
        entries, _payload = load_source(ROOT)
        analyst = entries[("large-context-analyst", "claude")]
        ultra = next(ref for ref in analyst["skills"] if ref.identifier == "ultra-research")
        self.assertEqual(ultra.availability, "uninstalled")
        self.assertEqual(ultra.evidence, "pending-reprobe")
        self.assertNotIn(
            "ultra-research",
            available_arrays(entries, "large-context-analyst", "claude")["skills"],
        )

        for specialist in (
            "experimental-attacker",
            "exploit-developer",
            "security-analyst",
            "smart-contract-engineer",
        ):
            for lane in ("claude", "gpt-codex"):
                refs = {
                    ref.identifier: (ref.availability, ref.evidence)
                    for ref in entries[(specialist, lane)]["mcps"]
                    if ref.identifier.startswith("guarded-")
                }
                self.assertEqual(
                    refs,
                    {
                        identifier: ("superseded", "superseded")
                        for identifier in (
                            "guarded-semgrep", "guarded-slither", "guarded-solodit"
                        )
                    },
                )
                projected = available_arrays(entries, specialist, lane)["mcps"]
                self.assertIn("chrono-vault", projected)
                self.assertTrue(
                    set(refs).isdisjoint(projected),
                    f"{specialist}:{lane}: guarded MCPs require operator installation",
                )

        self.assertNotIn(
            "playwright",
            available_arrays(entries, "frontend-engineer", "gpt-codex")["tools"],
        )

    def test_scraping_browser_is_a_required_mcp_on_claude_and_codex(self) -> None:
        entries, _payload = load_source(ROOT)
        for lane in ("claude", "gpt-codex"):
            entry = entries[("scraping-engineer", lane)]
            browser = next(
                ref for ref in entry["mcps"] if ref.identifier == "chrome-devtools"
            )
            self.assertEqual(
                browser,
                CapabilityRef(
                    "chrome-devtools", "required", "available", "lane-inventory"
                ),
            )
            projected = available_arrays(entries, "scraping-engineer", lane)
            self.assertIn("chrome-devtools", projected["mcps"])
            self.assertNotIn("chrome-devtools", projected["tools"])
        self.assertEqual(entries[("scraping-engineer", "gpt-codex")]["coverage"], "full")


if __name__ == "__main__":
    unittest.main()
