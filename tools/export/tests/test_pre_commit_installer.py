"""Regression controls for the worktree-independent pre-commit installer."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "docs/install/install-pre-commit-hook.sh"
GUARD = REPO_ROOT / "scripts/hooks/pre-commit"
DOCTOR = REPO_ROOT / "bin/doctor.sh"
EXPORT_DIR = REPO_ROOT / "tools/export"
sys.path.insert(0, str(EXPORT_DIR))

from path_policy import load_policy  # noqa: E402


POLICY_PATH = EXPORT_DIR / "policy/path-policy.json"
CONFIG_COMMAND = re.compile(
    r"git\s+config(?P<options>[^\n`]*?)\bcore\.hooksPath\b(?P<value>[^\n`]*)",
    re.IGNORECASE,
)
SYMLINK_COMMAND = re.compile(
    r"\bln\s+-(?P<flags>[A-Za-z]*s[A-Za-z]*)\s+"
    r"(?P<source>[^\s`]+)\s+(?P<destination>[^\s`]+)",
)
QUERY_OR_REMOVAL_OPTIONS = (
    "--get",
    "--get-all",
    "--get-regexp",
    "--list",
    "--show-origin",
    "--unset",
    "--unset-all",
)


@unittest.skipUnless(
    (REPO_ROOT / "shared/registries/skill-tool-registry.tsv").is_file(),
    "private integration: full validator closure requires the withheld skill-tool "
    "registry and capability baseline history; public snapshot controls run in tests/hooks",
)
class PreCommitInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "clone"
        self.root.mkdir()
        self.global_config = Path(self.temporary.name) / "global.gitconfig"
        self.global_config.write_text("", encoding="utf-8")
        self.env = os.environ.copy()
        self.env["GIT_CONFIG_GLOBAL"] = str(self.global_config)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        # These two declared Claude skills are installed by host plugins, not
        # tracked in the repository. Model their presence in a disposable HOME
        # so the real live-capability validator never borrows the operator's
        # plugin cache (and still runs when the guard strips the CI flag).
        self.home = Path(self.temporary.name) / "home"
        self.env["HOME"] = str(self.home)
        for skill in ("figma-code-connect", "frontend-design"):
            manifest = self.home / ".claude/plugins/cache" / skill / "SKILL.md"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(f"---\nname: {skill}\n---\nInstaller fixture only.\n")
        # The live validator also checks declared host-PATH capabilities. Give
        # it executable presence fixtures, not the developer's installed tools.
        commands = self.home / ".local/bin"
        commands.mkdir(parents=True)
        # Even os.defpath exposes host tools such as pip/perf on Linux. Link
        # only the real commands used by Git, the installer and doctor; all
        # other declared host-PATH tools below are presence-only fixtures.
        self.env["PATH"] = str(commands)
        for name in ("bash", "cat", "chmod", "dirname", "git", "grep", "install",
                     "mkdir", "mktemp", "mv", "readlink", "rm", "sh"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, f"required installer test command is missing: {name}")
            (commands / name).symlink_to(executable)
        (commands / "python3").symlink_to(sys.executable)
        # Presence-only fixtures must never execute: this test exercises Git
        # hooks, not providers. Keep the marker check active on failure too.
        marker = self.home / "unexpected-tool-execution"
        self.env["INSTALLER_TEST_TOOL_MARKER"] = str(marker)
        self.addCleanup(lambda: self.assertFalse(marker.exists(), "a capability fixture was executed"))
        source = json.loads((REPO_ROOT / "model-lanes/specialist-lane-capabilities.v1.json").read_text())
        for name in sorted({ref["id"] for entry in source["entries"] for ref in entry["tools"]
                            if ref["availability"] == "available" and ref["evidence"] == "host-PATH"}):
            if shutil.which(name, path=self.env["PATH"]) is None:
                stub = commands / name
                stub.write_text('#!/bin/sh\n: > "$INSTALLER_TEST_TOOL_MARKER"\nexit 97\n')
                stub.chmod(0o755)
        self._git("init", "-q")
        self._git("config", "user.name", "Hook Test")
        self._git("config", "user.email", "hook-test@example.invalid")

        # Keep fixture inputs tied to the installer and guard declarations.
        sources = re.findall(r"^source_\w+=.*$", INSTALLER.read_text(encoding="utf-8"), re.MULTILINE)
        if not sources:
            raise ValueError(f"no reviewed source declarations found in {INSTALLER}")
        paths = [INSTALLER.relative_to(REPO_ROOT).as_posix()]
        for declaration in sources:
            match = re.fullmatch(r'source_\w+="\$\{repo_root\}/([^"$`]+)"', declaration)
            if match is None:
                raise ValueError(f"unsupported installer source declaration: {declaration}")
            paths.append(match.group(1))
        paths.extend(runpy.run_path(str(GUARD))["SNAPSHOT_INPUTS"])
        # Follow validator declarations across the full reviewed execution closure.
        scanned = set()
        modules = {}
        original_path = sys.path[:]
        try:
            sys.path.insert(0, str(REPO_ROOT / "scripts/python"))
            for relative in paths:
                if relative in scanned:
                    continue
                scanned.add(relative)
                if relative.endswith(".py"):
                    source = REPO_ROOT / relative
                    if not source.is_file():
                        raise ValueError(f"missing reviewed installer fixture input: {source}")
                    modules[source.stem] = runpy.run_path(str(source))
                    paths.extend(modules[source.stem].get("INPUT_PATHS", ()))
        finally:
            sys.path[:] = original_path

        # Runtime-selected files are not static inputs. Reuse the validators'
        # discovery and policy templates so this fixture follows the same rows.
        specialists = modules["validate_specialists"]["Validator"](REPO_ROOT)
        paths.extend(path.relative_to(REPO_ROOT).as_posix()
                     for path in specialists.specialist_files)
        homes = modules["validate_capability_homes"]
        self.capability_homes = homes
        rows = homes["runtime_rows"](REPO_ROOT)
        adapters, issues = homes["load_adapters"](REPO_ROOT, rows)
        if issues:
            raise ValueError(f"invalid installer fixture adapters: {issues}")
        paths.extend(adapter["adapter"] for adapter in adapters.values())
        for policy in specialists.policy_rows("adapter_template"):
            if policy[3] == "main_yaml_registration":
                paths.append(f"model-lanes/{policy[1]}/main.yaml")

        # Installed-skill discovery also reads repository SKILL.md manifests.
        # Preserve their tracked paths so the real validator sees the same
        # lane-local evidence, without borrowing skills from the host home.
        skill_manifests = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", ":(glob)**/SKILL.md"],
            capture_output=True, check=True, env=self.env,
        ).stdout.split(b"\0")
        paths.extend(os.fsdecode(path) for path in skill_manifests if path)

        # The parity baseline is a Git object, not a repository file. Borrow the
        # source object store read-only instead of inventing a fixture baseline.
        baseline = homes["load_policy"](REPO_ROOT)["baseline_ref"]
        homes["require_baseline_commit"](REPO_ROOT, baseline)
        objects = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--path-format=absolute",
             "--git-path", "objects"],
            capture_output=True, text=True, check=True, env=self.env,
        ).stdout.strip()
        alternates = self.root / ".git/objects/info/alternates"
        alternates.write_text(objects + "\n", encoding="utf-8")
        for relative in dict.fromkeys(paths):
            source = REPO_ROOT / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError(f"non-relative installer fixture input: {relative}")
            if not source.is_file():
                raise ValueError(f"missing reviewed installer fixture input: {source}")
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self._git("add", ".")
        self._git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "baseline")

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def _install(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "docs/install/install-pre-commit-hook.sh")],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def _doctor_hook_check(self) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env["VAULT_ROOT"] = str(self.root)
        return subprocess.run(
            ["bash", str(DOCTOR), "--check-pre-commit-hook"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_fixture_preserves_repository_installed_skill_discovery(self) -> None:
        # Host-installed skills must not hide missing repository fixture inputs.
        with mock.patch.object(Path, "home", return_value=Path(self.temporary.name) / "empty-home"):
            discover = self.capability_homes["actual_skill_names"]
            for lane in self.capability_homes["LANES"]:
                with self.subTest(lane=lane):
                    self.assertEqual(discover(self.root, lane), discover(REPO_ROOT, lane))

    def test_fixture_path_matches_declared_host_inventory(self) -> None:
        commands = self.home / ".local/bin"
        self.assertEqual(self.env["PATH"].split(os.pathsep), [str(commands)])
        source = json.loads((REPO_ROOT / "model-lanes/specialist-lane-capabilities.v1.json").read_text())
        for entry in source["entries"]:
            for ref in entry["tools"]:
                with self.subTest(tool=ref["id"], availability=ref["availability"]):
                    found = shutil.which(ref["id"], path=self.env["PATH"])
                    if ref["availability"] == "available" and ref["evidence"] == "host-PATH":
                        self.assertEqual(found, str(commands / ref["id"]))
                    elif ref["availability"] in {"uninstalled", "platform-unavailable"}:
                        self.assertIsNone(found)

    def test_installed_guard_refuses_unexpected_host_tools(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        # Positive control: the real validator must detect the same unexpected
        # executables that leaked in from the Linux runner's system PATH.
        for name in ("pip", "perf"):
            stub = self.home / ".local/bin" / name
            stub.write_text('#!/bin/sh\n: > "$INSTALLER_TEST_TOOL_MARKER"\nexit 97\n')
            stub.chmod(0o755)
        blocked = subprocess.run(
            ["git", "-C", str(self.root), "commit", "--allow-empty", "-m", "must block"],
            capture_output=True, text=True, env=self.env,
        )
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        output = blocked.stdout + blocked.stderr
        for identifier in ("mac-ops:claude:pip", "performance-optimizer:claude:perf",
                           "performance-optimizer:gpt-codex:perf"):
            self.assertIn(identifier, output)
        self.assertIn("marked uninstalled but is now present on PATH", output)
        self.assertIn("marked platform-unavailable but is present on PATH", output)
        self.assertIn("live capability validation failed", output)

    def test_hostile_tracked_hook_cannot_replace_installed_guard(self) -> None:
        self._git("config", "core.hooksPath", ".githooks")
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        hooks_path = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "config",
                "--local",
                "--get-all",
                "core.hooksPath",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(hooks_path.returncode, 1)
        self.assertEqual(hooks_path.stdout, "")

        marker = Path(self.temporary.name) / "hostile-hook-executed"
        hostile_hook = self.root / ".githooks/pre-commit"
        hostile_hook.parent.mkdir()
        hostile_hook.write_text(
            f"#!/bin/sh\nprintf executed > {marker!s}\n", encoding="utf-8"
        )
        hostile_hook.chmod(0o755)
        self._git("add", ".githooks/pre-commit")
        committed = self._git("commit", "-qm", "hostile branch hook")
        self.assertEqual(committed.returncode, 0)
        self.assertFalse(marker.exists())

        private_path = self.root / "_state/bounty/secret.md"
        private_path.parent.mkdir(parents=True)
        private_path.write_text("private fixture\n", encoding="utf-8")
        self._git("add", "-f", "_state/bounty/secret.md")
        blocked = subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "must block"],
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("COMMIT BLOCKED", blocked.stderr)
        self.assertFalse(marker.exists())

    def test_installed_guard_refuses_missing_host_skill_fixtures(self) -> None:
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        empty_home = Path(self.temporary.name) / "empty-home"
        empty_home.mkdir()
        blocked = subprocess.run(
            ["git", "-C", str(self.root), "commit", "--allow-empty", "-m", "must block"],
            capture_output=True, text=True,
            env={**self.env, "HOME": str(empty_home)},
        )
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        for skill in ("figma-code-connect", "frontend-design"):
            self.assertIn(f"ui-engineer:claude:{skill}", blocked.stdout + blocked.stderr)
        self.assertIn("live capability validation failed", blocked.stdout + blocked.stderr)

    def test_unrelated_existing_hook_is_not_overwritten(self) -> None:
        hooks_dir = Path(
            self._git(
                "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        ) / "hooks"
        existing = hooks_dir / "pre-commit"
        existing.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        before = existing.read_bytes()

        result = self._install()

        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to replace existing hook", result.stderr)
        self.assertEqual(existing.read_bytes(), before)

    def test_effective_global_hooks_path_is_rejected(self) -> None:
        self._git("config", "--global", "core.hooksPath", ".githooks")

        result = self._install()

        self.assertEqual(result.returncode, 1)
        self.assertIn("installed guard is inactive", result.stderr)
        self.assertIn("global.gitconfig", result.stderr)
        self.assertIn(".githooks", result.stderr)
        self.assertEqual(self._git("config", "--get", "core.hooksPath").stdout.strip(), ".githooks")

    def test_doctor_objects_to_broken_hook_then_accepts_installer(self) -> None:
        hooks_dir = Path(
            self._git(
                "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        ) / "hooks"
        existing = hooks_dir / "pre-commit"
        existing.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        existing.chmod(0o755)

        broken = self._doctor_hook_check()
        self.assertEqual(broken.returncode, 1)
        self.assertIn("BROKEN", broken.stderr)
        self.assertIn("not the managed Vibe Squad entrypoint", broken.stderr)

        existing.unlink()
        installed = self._install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        healthy = self._doctor_hook_check()
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        self.assertIn("OK: managed guard is active outside the worktree", healthy.stdout)


class PublishedHookInstructionTests(unittest.TestCase):
    @staticmethod
    def _is_instruction_surface(path: str) -> bool:
        suffix = Path(path).suffix.casefold()
        return suffix in {".md", ".rst", ".txt"} or path.startswith(
            (".githooks/", "scripts/hooks/")
        )

    @staticmethod
    def _violations(path: str, text: str) -> list[str]:
        violations: list[str] = []
        for match in CONFIG_COMMAND.finditer(text):
            options = match.group("options")
            if any(option in options for option in QUERY_OR_REMOVAL_OPTIONS):
                continue
            value_tail = match.group("value").lstrip(" =\t")
            if not value_tail:
                continue
            value = value_tail.split()[0].strip("'\"),;:")
            outside_worktree = (
                value.startswith(("/", "~/", "$HOME/", "${HOME}/"))
                and value != "/dev/null"
            )
            if not outside_worktree:
                line = text.count("\n", 0, match.start()) + 1
                violations.append(
                    f"{path}:{line}: configures core.hooksPath as {value!r}"
                )

        for match in SYMLINK_COMMAND.finditer(text):
            source = match.group("source").strip("'\"),;:")
            destination = match.group("destination").strip("'\"),;:")
            if ".git/hooks/" not in destination:
                continue
            line = text.count("\n", 0, match.start()) + 1
            violations.append(
                f"{path}:{line}: symlinks Git hook {destination!r} to worktree path {source!r}"
            )
        return violations

    def test_detector_has_positive_and_negative_controls(self) -> None:
        vulnerable = """\
git config core.hooksPath .githooks
ln -sf ../../scripts/hooks/pre-commit .git/hooks/pre-commit
"""
        safe = """\
git config --show-origin --get-all core.hooksPath
git config --unset-all core.hooksPath
bash docs/install/install-pre-commit-hook.sh
"""
        self.assertEqual(len(self._violations("fixture.md", vulnerable)), 2)
        self.assertEqual(self._violations("fixture.md", safe), [])

    def test_no_published_instruction_loads_hook_code_from_worktree(self) -> None:
        policy = load_policy(POLICY_PATH)
        tracked = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        violations: list[str] = []
        for raw_path in tracked:
            if not raw_path:
                continue
            path = os.fsdecode(raw_path)
            if policy.classify(path) != "public" or not self._is_instruction_surface(path):
                continue
            candidate = REPO_ROOT / path
            try:
                text = candidate.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            violations.extend(self._violations(path, text))

        self.assertEqual(
            violations,
            [],
            "published instructions must install a copied hook outside the worktree:\n"
            + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
