"""Locator shape, real registry writes, and sender dry-run controls."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from artifact_locator import ArtifactLocator
from dispatch_checkout import normal_checkout_root
from ci_host_independence import hermetic_lane_cli_environment


TASK_ID = "TASK-2026-09-22-1221-locator-test"


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments], check=True,
        capture_output=True, text=True, timeout=10,
    )


def init_product(root: Path) -> None:
    root.mkdir()
    git(root, "init", "-q", "-b", "release/product")
    git(root, "-c", "user.name=Locator Test", "-c", "user.email=locator@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "fixture")


class ArtifactLocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.complete = {
            "work_repo_root": "/repos/product",
            "work_base_branch": "release/product",
            "board_ref": f"board/{TASK_ID}",
            "deliverable_relpath": "reports/final report.md",
            "deliverable_sha256": "ab" * 32,
        }

    def test_complete_locator_round_trips_and_binds_task(self) -> None:
        locator = ArtifactLocator.from_registry({**self.complete, "status": "needs-review"})
        self.assertEqual(json.loads(json.dumps(locator.to_dict())), self.complete)
        self.assertIs(locator.require_deliverable(TASK_ID), locator)
        with self.assertRaisesRegex(ValueError, "task id"):
            locator.require_deliverable("TASK-different")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            locator.board_ref = "board/TASK-different"

    def test_squad_legacy_and_registration_states_are_not_deliverables(self) -> None:
        for entry in ({}, {name: None for name in self.complete}, {
            name: self.complete[name] for name in ("work_repo_root", "work_base_branch")
        }):
            with self.subTest(entry=entry):
                locator = ArtifactLocator.from_registry(entry)
                self.assertEqual(set(locator.to_dict()), set(self.complete))
                self.assertIsNone(locator.board_ref)
                with self.assertRaisesRegex(ValueError, "complete"):
                    locator.require_deliverable(TASK_ID)

    def test_published_locator_is_immutable_and_retries_are_idempotent(self) -> None:
        published = ArtifactLocator.from_registry(self.complete)
        pending = ArtifactLocator(
            work_repo_root=published.work_repo_root,
            work_base_branch=published.work_base_branch,
        )
        self.assertIs(published.publish_over(pending), published)
        retry = ArtifactLocator.from_registry(self.complete)
        self.assertIs(retry.publish_over(published), retry)
        changed = {
            "work_repo_root": "/repos/another-product",
            "work_base_branch": "release/another",
            "board_ref": "board/TASK-another",
            "deliverable_relpath": "reports/another.md",
            "deliverable_sha256": "cd" * 32,
        }
        for field, value in changed.items():
            with self.subTest(field=field):
                replacement = ArtifactLocator.from_registry({**self.complete, field: value})
                with self.assertRaisesRegex(ValueError, "cannot be replaced"):
                    replacement.publish_over(published)
        for replacement in (pending, ArtifactLocator()):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(ValueError, "cannot be replaced"):
                    replacement.publish_over(published)

    def test_length_ceiling_is_inclusive(self) -> None:
        for field, value in self.complete.items():
            if field == "deliverable_sha256":
                continue  # The digest has its own exact 64-character requirement.
            with self.subTest(field=field):
                at_limit = value + "x" * (4096 - len(value))
                locator = ArtifactLocator.from_registry({**self.complete, field: at_limit})
                self.assertEqual(getattr(locator, field), at_limit)

    def test_other_literal_names_remain_accepted(self) -> None:
        for branch in ("@main", "main@", "++weird"):
            with self.subTest(branch=branch):
                ArtifactLocator.from_registry({**self.complete, "work_base_branch": branch})
        ArtifactLocator.from_registry({**self.complete, "work_repo_root": "/repos/\u202eproduct"})

    def test_partial_ownership_or_deliverable_is_rejected(self) -> None:
        for name in self.complete:
            for missing in (True, False):
                with self.subTest(field=name, missing=missing):
                    entry = dict(self.complete)
                    if missing:
                        entry.pop(name)
                    else:
                        entry[name] = None
                    with self.assertRaises(ValueError):
                        ArtifactLocator.from_registry(entry)
        with self.assertRaises(ValueError):
            ArtifactLocator(work_repo_root="/repos/product")
        with self.assertRaises(ValueError):
            ArtifactLocator(work_base_branch="main")

    def test_malformed_fields_are_rejected(self) -> None:
        invalid = {
            "work_repo_root": ["relative", "/repo/../escape", "/repo/./sub", "/repo/",
                               "//repo", "/repo//sub", "C:\\repo", "/repo\nsub"],
            "work_base_branch": ["-option", "@", "main~1", "main^", "a..b", "a@{0}",
                                 "a:b", "a?b", "a*b", "a[b", "a\\b", "a b", ".hidden",
                                 "a/.hidden", "main.lock", "main/", "main.", "a//b",
                                 "HEAD", "refs/heads/main", "a/@/b", "@/main", "main/@"],
            "board_ref": ["main", "refs/heads/board/TASK-test", "board/TASK-",
                          "board/other", "board/TASK-test/nested", "board/TASK-test~1"],
            "deliverable_relpath": ["/etc/passwd", "../outside", "a/../../outside", ".",
                                    "a/./b", "a//b", "a/", "C:/outside", "a\\b",
                                    "-rf", "--upload-pack=x", ".git/config",
                                    ".git/hooks/pre-commit", "a/.git/x", "a/.git"],
            "deliverable_sha256": ["a" * 63, "a" * 65, "A" * 64, "g" * 64],
        }
        for field, values in invalid.items():
            valid = self.complete[field]
            separators = [valid + char + "x" for char in "\x85\u2028\u2029"]
            over_limit = valid + "x" * (4097 - len(valid))
            for value in [*values, *separators, over_limit,
                          "", " padded ", "bad\x00value", 1, False, [], {}]:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        ArtifactLocator.from_registry({**self.complete, field: value})
        for entry in (None, [], "locator"):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                ArtifactLocator.from_registry(entry)


class RegistrationTests(unittest.TestCase):
    """Run the sender's exact registration block and the real locked reconciler.

    Admission and model launch are outside this fixture. Registry and Git writes
    are confined to its temporary directory; no live task is dispatched.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="artifact-registration-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name).resolve()
        self.vault = self.temp / "squad"
        (self.vault / "bin").mkdir(parents=True)
        (self.vault / "bin/registry-reconciler.sh").symlink_to(ROOT / "bin/registry-reconciler.sh")
        (self.vault / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
        (self.vault / "_state").mkdir()
        self.product = self.temp / "external product"
        init_product(self.product)
        sender = (ROOT / "bin/send-task.sh").read_text()
        start = sender.index('if REGISTRY_ENTRY_JSON="$(')
        end = sender.index('\nDELIVERY_ATTEMPT_ID=""', start)
        self.registration = sender[start:end]

    def register(self, fields: dict[str, str]) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "VAULT_ROOT": str(self.vault),
            "STATE_DIR": str(self.vault / "_state"),
            "SQUAD_TEST_ISOLATION": "1",
            "UV_CACHE_DIR": str(self.temp / "uv-cache"),
            "SQUAD_CODE_ROOT": str(ROOT),
            "SQUAD_BASE_BRANCH": "squad-branch-must-not-be-used",
            "TASK_ID": TASK_ID,
            "TASK_FRONTMATTER_JSON": json.dumps({"fields": fields}),
            "WRITE_SCOPE_JSON": "[]", "SPECIALIST": "backend-engineer",
            "TO_MODEL": "gpt-codex", "SOURCE_NAMESPACE": "coding",
            "REVIEW_MODEL": "none", "MANDATORY_REVIEW": "false",
            "REVIEW_TRIGGERS_JSON": "[]", "REVIEW_CLASS": "standard",
            "PARALLEL_SAFE": "false", "DIRECT_LANE_WORK_ALLOWED": "false",
            "RETURN_ARTIFACT": "reports/result.md", "SWARM_SPEC_SHA256": "",
            "CAPABILITY_SNAPSHOT_JSON": "", "AUTHOR_FAMILY": "openai",
            "VERIFICATION_CONTRACT_JSON": "", "VERIFICATION_CONTRACT_SHA256": "",
        }
        return subprocess.run(
            ["bash", "-c", 'set -euo pipefail\n'
             'die() { printf "%s\\n" "$*" >&2; exit 1; }\n'
             'info() { printf "%s\\n" "$*"; }\n' + self.registration],
            env=environment, capture_output=True, text=True, timeout=30,
        )

    def test_external_registration_persists_resolved_root_and_own_branch(self) -> None:
        alias = self.temp / "product-alias"
        alias.symlink_to(self.product, target_is_directory=True)
        result = self.register({"work_repo": f'"{alias}"'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entry = json.loads((self.vault / "_state/active-tasks.json").read_text())[TASK_ID]
        self.assertEqual(entry["work_repo_root"], str(self.product))
        self.assertEqual(entry["work_base_branch"], "release/product")
        self.assertIsNone(ArtifactLocator.from_registry(entry).board_ref)

    def test_squad_registration_persists_explicit_nulls(self) -> None:
        result = self.register({"work_repo_root": str(self.product), "work_base_branch": "spoofed"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entry = json.loads((self.vault / "_state/active-tasks.json").read_text())[TASK_ID]
        self.assertIsNone(entry["work_repo_root"])
        self.assertIsNone(entry["work_base_branch"])

    def test_invalid_external_repo_cannot_register_as_squad_task(self) -> None:
        result = self.register({"work_repo": "relative/path"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("work_repo must be an absolute path", result.stderr)
        self.assertFalse((self.vault / "_state/active-tasks.json").exists())


class SenderDryRunTests(unittest.TestCase):
    def test_external_and_squad_packets_pass_real_launch_preflight(self) -> None:
        checkout = normal_checkout_root(ROOT)
        # Exercise admission without borrowing a host installation or launching
        # a real model. The shared fixture also asserts that no CLI was run.
        environment = hermetic_lane_cli_environment(self, ("codex",))
        with tempfile.TemporaryDirectory(prefix="locator-dryrun-") as raw:
            temporary = Path(raw).resolve()
            product = temporary / "external product"
            init_product(product)
            for kind in ("external", "squad"):
                with self.subTest(kind=kind):
                    task_id = f"{TASK_ID}-{kind}"
                    artifact = f"departments/coding/outbox/{task_id}-response.md"
                    packet = temporary / f"{kind}.md"
                    fields = {
                        "id": task_id, "run_id": "none", "from": "chrono",
                        "mode": "modeless", "phase": "none", "type": "TASK",
                        "status": "new", "priority": "normal",
                        "write_scope": f"[{artifact}]", "authorized_delete_paths": "[]",
                        "reviews": "none", "return_artifact": artifact,
                        "compatibility_namespace": "coding", "specialist": "backend-engineer",
                        "to_model": "gpt-codex", "source_namespace": "coding",
                        "model_override_reason": "none", "review_model": "none",
                        "mandatory_review": "false", "review_triggers": "[]",
                        "parallel_safe": "false", "direct_lane_work_allowed": "false",
                        "operator_approved": "true",
                    }
                    if kind == "external":
                        fields["work_repo"] = f'"{product}"'
                    packet.write_text("---\n" + "\n".join(f"{k}: {v}" for k, v in fields.items())
                                      + "\n---\n\nLocator dry-run control.\n")
                    result = subprocess.run(
                        ["bash", str(ROOT / "bin/send-task.sh"), str(packet), "--dry-run"],
                        env={**environment, "VAULT_ROOT": str(checkout),
                             "SQUAD_BASE_BRANCH": "main", "PYTHONDONTWRITEBYTECODE": "1"},
                        capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("[DRY RUN] launch preflight PASSED:", result.stdout)
                    self.assertNotIn("LAUNCH PREFLIGHT NOT RUN", result.stderr)


if __name__ == "__main__":
    unittest.main()
