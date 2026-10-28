from __future__ import annotations

import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = "scripts/python/validate_release_version.py"
HOOK = "scripts/hooks/pre-commit"
INSTALLER = "docs/install/install-pre-commit-hook.sh"
DOCUMENTS = ("CHANGELOG.md", "CLAUDE.md", "README.md", "SECURITY.md")
HISTORY = (
    "docs/2026-09-01-v1.1.5-hardening-outcomes.md",
    "docs/superpowers/plans/2026-08-31-vibesquad-hardening.md",
)


class ReleaseVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="TASK-46dc0813-release-version-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Git fixtures must not consult the operator's repository/configuration.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            PYTHONDONTWRITEBYTECODE="1",
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
        )
        self._seed(self.root)
        # Read the concrete fixture's heading independently of the implementation;
        # controls continue to work after a release without another version copy.
        self.release_heading = next(
            line for line in (self.root / "CHANGELOG.md").read_text().splitlines()
            if line.casefold().startswith("## v")
        )
        self.version = next(
            line.split()[1].casefold()
            for line in (self.root / "CHANGELOG.md").read_text().splitlines()
            if line.casefold().startswith("## v")
        )
        self.drift = "v99.0.0" if self.version != "v99.0.0" else "v98.0.0"
        self.claims = (
            ("CLAUDE.md current-release passage", "CLAUDE.md",
             f"- **`{self.version.upper()}` is the current release version.**"),
            ("README.md badge image URL", "README.md",
             f"![version](https://img.shields.io/badge/version-{self.version}-blue)"),
            ("README.md current-release prose", "README.md",
             f"**{self.version}** is the current release;"),
            ("SECURITY.md current-release passage", "SECURITY.md",
             f"The current release is **{self.version}**;"),
        )

    def _seed(self, root: Path, *, omit: tuple[str, ...] = ()) -> None:
        for name in (*DOCUMENTS, VALIDATOR, HOOK, INSTALLER):
            if name in omit:
                continue
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / name, target)

    def _run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, cwd=cwd or self.root, env=self.env,
            capture_output=True, text=True, check=False, timeout=15,
        )

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self._run("git", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _init_index(self, *, omit: tuple[str, ...] = ()) -> None:
        self._git("-c", "init.templateDir=", "init", "--quiet")
        self._git("add", "--", *(name for name in DOCUMENTS if name not in omit))

    def _validate(self, *, root: Path | None = None, staged: bool = False):
        return self._run(
            sys.executable, "-I", "-B", str(REPO_ROOT / VALIDATOR),
            "--root", str(root or self.root), *(["--staged"] if staged else []),
        )

    def _hook(self):
        return self._run(sys.executable, "-I", "-B", str(self.root / HOOK))

    def _replace(self, path: str, old: str, new: str) -> None:
        target = self.root / path
        text = target.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"fixture must identify one claim in {path}")
        target.write_text(text.replace(old, new), encoding="utf-8")

    def _drifted(self, claim: str) -> str:
        return claim.replace(self.version, self.drift).replace(self.version.upper(), self.drift.upper())

    def _assert_pass(self, result) -> None:
        output = result.stdout + result.stderr  # Git forwards hook stdout to stderr.
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("release-version: PASS", output)
        self.assertIn("4 sites", output)

    def _assert_failure(self, result, site: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(site, result.stderr)
        self.assertNotIn("release-version: PASS", result.stdout + result.stderr)

    def _assert_drift(self, index: int) -> None:
        site, path, claim = self.claims[index]
        self._replace(path, claim, self._drifted(claim))
        result = self._validate()
        self._assert_failure(result, site)
        self.assertIn(f"found {self.drift}; expected {self.version}", result.stderr)

    def test_positive_control_current_tree(self) -> None:
        self._assert_pass(self._validate(root=REPO_ROOT))

    def test_negative_control_root_instruction(self) -> None:
        self._assert_drift(0)

    def test_negative_control_readme_badge(self) -> None:
        self._assert_drift(1)

    def test_negative_control_readme_prose(self) -> None:
        self._assert_drift(2)

    def test_negative_control_security(self) -> None:
        self._assert_drift(3)

    @unittest.skipUnless(
        all((REPO_ROOT / name).exists() for name in HISTORY),
        "dated historical records are withheld from the public export; this control "
        "needs them present, so it degrades to a skip rather than failing a published clone",
    )
    def test_exclusion_control_dated_records(self) -> None:
        for name in HISTORY:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / name, target)
            self.assertIn("v1.1.5", target.read_text(encoding="utf-8"))
        self._assert_pass(self._validate())
        # A passing historical fixture still detects a live claim drifting.
        self._assert_drift(1)

    def test_case_insensitive_comparison_at_all_four_sites(self) -> None:
        for _, path, claim in self.claims:
            swapped = claim.replace(self.version.upper(), self.version) if path == "CLAUDE.md" else claim.replace(self.version, self.version.upper())
            self._replace(path, claim, swapped)
        self._replace("CHANGELOG.md", self.release_heading, self.release_heading.replace(self.version, self.version.upper()))
        self._assert_pass(self._validate())

    def test_missing_claim_fails_at_each_site(self) -> None:
        for site, path, claim in self.claims:
            with self.subTest(site=site):
                original = (self.root / path).read_text(encoding="utf-8")
                self._replace(path, claim, "Current release claim deliberately omitted.")
                self._assert_failure(self._validate(), site)
                (self.root / path).write_text(original, encoding="utf-8")

    def test_duplicate_claim_fails_at_each_site(self) -> None:
        for site, path, claim in self.claims:
            with self.subTest(site=site):
                original = (self.root / path).read_text(encoding="utf-8")
                self._replace(path, claim, claim + "\n" + claim)
                result = self._validate()
                self._assert_failure(result, site)
                self.assertIn("found 2", result.stderr)
                (self.root / path).write_text(original, encoding="utf-8")

    def test_prerelease_suffix_does_not_match_released_version(self) -> None:
        for site, path, claim in self.claims:
            with self.subTest(site=site):
                original = (self.root / path).read_text(encoding="utf-8")
                altered = claim.replace(self.version, self.version + "-rc.1").replace(self.version.upper(), self.version.upper() + "-rc.1")
                self._replace(path, claim, altered)
                self._assert_failure(self._validate(), site)
                (self.root / path).write_text(original, encoding="utf-8")

    def test_unreleased_is_skipped_but_top_release_wins_over_later_headers(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        original = changelog.read_text(encoding="utf-8")
        changelog.write_text("## [Unreleased]\n\n" + original + "\n## v999.0.0\n", encoding="utf-8")
        self._assert_pass(self._validate())

    def test_changelog_header_grammar_accepts_common_forms(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        original = changelog.read_text(encoding="utf-8")
        for header in (
            "## [Unreleased]",
            "## Unreleased",
            f"## {self.version} - 2026-09-08",
            "## [Unreleased] - TBD",
            "## Unreleased (next)",
            f"## {self.version} (2026-09-08)",
            f"## {self.version} — 2026-09-08",
            "## [uNrElEaSeD] (next)",
            "## uNrElEaSeD - TBD",
            "## [Unreleased]\t-\tTBD",
            f"## {self.version.upper()}\t(2026-09-08)\t",
            f"## {self.version}",
        ):
            with self.subTest(header=header):
                changelog.write_text(header + "\n\n" + original, encoding="utf-8")
                result = self._validate()
                self._assert_pass(result)
                self.assertIn(f"({self.version}; 4 sites;", result.stdout)

    def test_changelog_header_grammar_rejects_malformed_forms(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        original = changelog.read_text(encoding="utf-8")
        for header in (
            "## v1.1.7 (unreleased)",
            "## release-version-missing",
            "## Unreleased-notes",
            "## [Unreleased]-TBD",
            "## [Unreleased",
            "## Unreleased]",
            "## [Unreleased](next)",
            f"## {self.version} (2026-9-08)",
            f"## {self.version} (2026-09-08",
            f"## {self.version} 2026-09-08)",
            f"## {self.version} 2026-09-08",
            f"## {self.version} - TBD",
            f"## {self.version} (2026-09-08) extra",
            "## v1.1 (2026-09-08)",
            "## \t",
        ):
            with self.subTest(header=header):
                # A valid historical header must never hide a malformed top one.
                changelog.write_text(header + "\n\n" + original, encoding="utf-8")
                result = self._validate()
                self._assert_failure(result, "CHANGELOG.md source of truth")
                self.assertEqual(result.returncode, 1)
                self.assertIn("top released header must be", result.stderr)

    def test_annotated_unreleased_headers_still_require_a_release(self) -> None:
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased] - TBD\n\n## Unreleased (next)\n",
            encoding="utf-8",
        )
        self._assert_failure(self._validate(), "missing released level-two header")

    def test_parenthesized_top_release_still_detects_drift_at_all_four_sites(self) -> None:
        changelog = self.root / "CHANGELOG.md"
        original = changelog.read_text(encoding="utf-8")
        changelog.write_text(
            f"## [Unreleased] - TBD\n\n## {self.drift} (2026-09-08)\n\n" + original,
            encoding="utf-8",
        )
        result = self._validate()
        for site, _, _ in self.claims:
            self._assert_failure(result, site)
        self.assertIn(f"expected {self.drift}", result.stderr)

    def test_staged_header_grammar_keeps_index_as_source_of_truth(self) -> None:
        self._init_index()
        changelog = self.root / "CHANGELOG.md"
        original = changelog.read_text(encoding="utf-8")
        accepted = f"## [Unreleased] - TBD\n\n## {self.version} (2026-09-08)\n\n" + original
        changelog.write_text(accepted, encoding="utf-8")
        self._git("add", "--", "CHANGELOG.md")
        changelog.write_text("## release-version-missing\n\n" + original, encoding="utf-8")
        self._assert_pass(self._validate(staged=True))
        self._assert_failure(self._validate(), "CHANGELOG.md source of truth")
        self._git("add", "--", "CHANGELOG.md")
        changelog.write_text(accepted, encoding="utf-8")
        self._assert_failure(self._validate(staged=True), "CHANGELOG.md source of truth")

    def test_malformed_top_release_does_not_fall_back_to_history(self) -> None:
        self._replace("CHANGELOG.md", self.release_heading, "## release-version-missing")
        self._assert_failure(self._validate(), "CHANGELOG.md source of truth")

    def test_no_released_header_fails(self) -> None:
        (self.root / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n", encoding="utf-8")
        self._assert_failure(self._validate(), "missing released level-two header")

    def test_missing_input_files_fail_closed(self) -> None:
        for index, name in enumerate(DOCUMENTS):
            with self.subTest(path=name):
                root = self.root / f"missing-input-{index}"
                self._seed(root, omit=(name,))
                self._assert_failure(self._validate(root=root), name)

    def test_invalid_utf8_fails_closed(self) -> None:
        (self.root / "README.md").write_bytes(b"\xff")
        self._assert_failure(self._validate(), "README.md")

    def test_hook_blocks_staged_drift_and_ignores_unstaged_drift_at_each_site(self) -> None:
        self._init_index()
        self._assert_pass(self._hook())
        for site, path, claim in self.claims:
            with self.subTest(site=site):
                original = (self.root / path).read_text(encoding="utf-8")
                self._replace(path, claim, self._drifted(claim))
                self._git("add", "--", path)
                (self.root / path).write_text(original, encoding="utf-8")
                self._assert_failure(self._hook(), site)
                self._git("add", "--", path)
                self._replace(path, claim, self._drifted(claim))
                self._assert_pass(self._hook())
                (self.root / path).write_text(original, encoding="utf-8")

    def test_staged_changelog_is_source_of_truth(self) -> None:
        self._init_index()
        original = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        self._replace("CHANGELOG.md", self.release_heading, self.release_heading.replace(self.version, self.drift))
        self._git("add", "--", "CHANGELOG.md")
        (self.root / "CHANGELOG.md").write_text(original, encoding="utf-8")
        result = self._validate(staged=True)
        for site, _, _ in self.claims:
            self._assert_failure(result, site)
        self.assertIn(f"expected {self.drift}", result.stderr)

    def test_hook_missing_staged_document_fails_with_correct_worktree(self) -> None:
        self._init_index(omit=("SECURITY.md",))
        self._assert_failure(self._hook(), "SECURITY.md")

    def test_staged_mode_outside_git_fails_closed(self) -> None:
        self._assert_failure(self._validate(staged=True), "unable to read staged blob")

    def test_existing_leak_guard_still_blocks_restricted_frontmatter(self) -> None:
        self._init_index()
        (self.root / "private-note.md").write_text("---\nsensitivity: restricted\n---\nfixture\n", encoding="utf-8")
        self._git("add", "--", "private-note.md")
        self._assert_failure(self._hook(), "restricted sensitivity frontmatter")

    def test_existing_leak_guard_still_blocks_private_path(self) -> None:
        self._init_index()
        private = self.root / "_state/bounty/fixture.txt"
        private.parent.mkdir(parents=True)
        private.write_text("fixture\n", encoding="utf-8")
        self._git("add", "--", "_state/bounty/fixture.txt")
        self._assert_failure(self._hook(), "private _state/bounty path")

    def _install(self) -> Path:
        # Reuse the hook suite's bounded unrelated-validator spies. Keep the
        # real hook, wrappers, release validator and reviewed dependency closure.
        fixtures = runpy.run_path(str(REPO_ROOT / "tests/hooks/test_pre_commit.py"))
        definitions = fixtures["HOOK_DEFINITIONS"]
        for name in (*definitions["SNAPSHOT_INPUTS"], *definitions["MOAT_INPUTS"]):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO_ROOT / name, target)
        self.env["HOOK_TEST_LOG"] = str(self.root / "hook-test-calls.jsonl")
        self.env["PRE_COMMIT_PYTHON"] = sys.executable
        for name in ("validate_capabilities", "validate_specialists", "validate_capability_homes", "validate_skill_wiring"):
            (self.root / f"scripts/python/{name}.py").write_text(fixtures["VALIDATOR_SPY"], encoding="utf-8")
        typescript = Path(os.environ.get("PRE_COMMIT_TEST_TYPESCRIPT_ROOT", str(REPO_ROOT / "moat/node_modules/typescript")))
        for name in ("package.json", "lib/typescript.js"):
            if (typescript / name).is_file():
                target = self.root / "moat/node_modules/typescript" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(typescript / name, target)
        self._init_index()
        result = self._run("/bin/bash", str(self.root / INSTALLER))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hooks = self.root / ".git/hooks"
        companion = hooks / "vibe-squad-validate-release-version.py"
        self.assertFalse(companion.is_symlink())
        self.assertEqual(companion.read_bytes(), (self.root / VALIDATOR).read_bytes())
        return hooks

    def test_installed_hook_uses_copied_validator_and_blocks_drift(self) -> None:
        hooks = self._install()
        self._assert_pass(self._run(str(hooks / "pre-commit")))
        # The installed hook must keep using its reviewed snapshot after a
        # checkout changes the source script. This fixture only returns a code.
        (self.root / VALIDATOR).write_text("raise SystemExit(91)\n", encoding="utf-8")
        self._assert_pass(self._run(str(hooks / "pre-commit")))
        site, path, claim = self.claims[1]
        self._replace(path, claim, self._drifted(claim))
        self._git("add", "--", path)
        self._assert_failure(self._run(str(hooks / "pre-commit")), site)

    def test_installed_hook_without_companion_fails_closed(self) -> None:
        self._init_index()
        hooks = self.root / ".git/hooks"
        hooks.mkdir()
        snapshot = hooks / "vibe-squad-pre-commit"
        shutil.copyfile(self.root / HOOK, snapshot)
        result = self._run(sys.executable, "-I", "-B", str(snapshot))
        self._assert_failure(result, "pre-commit release-version guard: COMMIT BLOCKED")

    def test_installer_requires_validator_source(self) -> None:
        root = self.root / "missing-validator"
        self._seed(root, omit=(VALIDATOR,))
        result = self._run("git", "-c", "init.templateDir=", "init", "--quiet", cwd=root)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._run("/bin/bash", str(root / INSTALLER), cwd=root)
        self._assert_failure(result, "missing reviewed release validator")
        self.assertFalse((root / ".git/hooks/pre-commit").exists())

    def test_installed_hook_allows_real_commit_and_rejects_drifting_commit(self) -> None:
        self._install()
        self._git("config", "user.name", "Release guard fixture")
        self._git("config", "user.email", "release-guard@example.invalid")
        self._assert_pass(self._git("commit", "--quiet", "-m", "consistent release"))
        before = self._git("rev-parse", "HEAD").stdout.strip()
        site, path, claim = self.claims[3]
        self._replace(path, claim, self._drifted(claim))
        self._git("add", "--", path)
        result = self._run("git", "commit", "--quiet", "-m", "drifting release")
        self._assert_failure(result, site)
        self.assertEqual(self._git("rev-parse", "HEAD").stdout.strip(), before)


if __name__ == "__main__":
    unittest.main()
