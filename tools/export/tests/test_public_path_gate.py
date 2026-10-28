from __future__ import annotations

import sys
import unittest
from pathlib import Path


EXPORT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPORT_DIR))

from path_policy import load_policy  # noqa: E402


POLICY_PATH = EXPORT_DIR / "policy" / "path-policy.json"
EXPECTED_PUBLIC_EXCEPTIONS = (
    "_state/.gitkeep",
    "assets/hero/vibe-squad-hero.svg",
    "bench/known-bugs/.gitignore",
    "bench/known-bugs/README.md",
    "bench/known-bugs/foundry.toml",
    "bench/known-bugs/negative-control/ReentrantBank.sol",
    "bench/known-bugs/negative-control/RoundingVault.sol",
    "bench/known-bugs/selftest.sh",
    "bench/known-bugs/semgrep/cei-violation.yaml",
    "bench/known-bugs/src/ReentrantBank.sol",
    "bench/known-bugs/src/RoundingVault.sol",
    "bench/known-bugs/test/Exploits.t.sol",
    "departments/*/active/.gitkeep",
    "departments/*/archive/.gitkeep",
    "departments/*/inbox/.gitkeep",
    "departments/*/outbox/.gitkeep",
    "docs/README.md",
    "docs/adding-a-specialist.md",
    "docs/architecture.md",
    "docs/board-mcp-surface.md",
    "docs/getting-started.md",
    "docs/git-hooks.md",
    "docs/model-runtime-map.md",
    "docs/private-config.md",
    "docs/production-readiness.md",
    "docs/publish-provenance.md",
    "docs/rollback-runbook.md",
    "docs/runbooks/notification-hooks-canary.md",
    "docs/standards/instruction-layer-standard-and-rubric.md",
    "docs/standards/settlement-guard-coverage.md",
    "docs/standards/tool-trigger-map.md",
    "docs/tooling/security-arsenal-guide.md",
    "tools/coverage-ledger/fixtures/bad-decline/gaps.md",
    "tools/coverage-ledger/fixtures/declined/gaps.md",
    "tools/coverage-ledger/fixtures/declines/bad-decline.tsv",
    "tools/coverage-ledger/fixtures/declines/declined.tsv",
    "tools/coverage-ledger/fixtures/expected.tsv",
    "tools/coverage-ledger/fixtures/mentions-only/DISPATCH-PLAN.md",
    "tools/coverage-ledger/fixtures/mentions-only/TOOLCHAIN.md",
    "tools/coverage-ledger/fixtures/real-runs/evidence/echidna-run.log",
    "tools/coverage-ledger/fixtures/real-runs/evidence/forge-suite.txt",
    "tools/coverage-ledger/fixtures/real-runs/evidence/halmos-pause.json",
    "tools/coverage-ledger/fixtures/real-runs/evidence/slither.stderr",
)


class PublicPathGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_policy(POLICY_PATH)

    def test_precedence_and_default_deny(self) -> None:
        self.assertEqual(self.policy.classify("docs/README.md"), "public")
        self.assertEqual(self.policy.classify("shared/synthetic/current.md"), "private")
        self.assertEqual(
            self.policy.classify("synthetic-surface/unmatched.txt"),
            "unknown",
        )

    def test_public_readme_and_synthetic_fixture(self) -> None:
        self.assertEqual(self.policy.classify("README.md"), "public")
        self.assertEqual(
            self.policy.classify("tools/export/tests/fixtures/synthetic.txt"),
            "public",
        )

    def test_public_exception_list_is_closed(self) -> None:
        self.assertEqual(
            tuple(sorted(self.policy.public_exceptions)),
            EXPECTED_PUBLIC_EXCEPTIONS,
        )

    def test_synthetic_run_output_is_denied_at_depth_in_file_form(self) -> None:
        self.assertEqual(
            self.policy.classify("tools/export/fixtures/synthetic/a/b/results.json"),
            "private",
        )

    def test_synthetic_run_output_is_denied_at_depth_in_directory_form(self) -> None:
        self.assertEqual(
            self.policy.classify(
                "tools/export/fixtures/synthetic/a/b/results/synthetic.json"
            ),
            "private",
        )

    def test_universal_secret_shapes_are_denied_on_a_synthetic_root(self) -> None:
        paths = (
            "synthetic-surface/a/b/.env",
            "synthetic-surface/a/b/.env.production",
            "synthetic-surface/synthetic.pem",
            "synthetic-surface/synthetic.key",
            "synthetic-surface/synthetic-secret.json",
            "synthetic-surface/synthetic.log",
            "synthetic-surface/synthetic.local.md",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.policy.classify(path), "private")

    def test_single_star_does_not_cross_a_path_separator(self) -> None:
        self.assertEqual(
            self.policy.classify("docs/synthetic-sub/synthetic.md"),
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
