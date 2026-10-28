"""Exercise pruning only in disposable squad/external Git repositories."""

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "bin/prune-board-worktrees.sh"


class ExternalWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prune-external-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.real_git = shutil.which("git")
        self.root = self.make_repo("squad")
        self.external = self.make_repo("external repo")
        self.state = self.root / "_state"
        self.board = self.state / "board-worktrees"
        self.board.mkdir(parents=True)
        (self.state / "board-dispatch").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "scripts/python").symlink_to(REPO_ROOT / "scripts/python")
        self.registry = {}
        self.trace = self.base / "git-calls.jsonl"
        wrapper = self.base / "bin/git"
        wrapper.parent.mkdir()
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['PRUNE_TEST_TRACE'], 'a') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "if 'remove' in args and os.environ.get('PRUNE_TEST_RESCUED'):\n"
            "    assert pathlib.Path(os.environ['PRUNE_TEST_RESCUED']).is_file(), "
            "'removal preceded rescue'\n"
            "if args[-2:] == ['worktree', 'prune'] and os.environ.get('PRUNE_TEST_FAIL_PRUNE'):\n"
            "    sys.stderr.write('fixture prune failure\\n')\n"
            "    sys.exit(1)\n"
            "os.execv(os.environ['PRUNE_TEST_GIT'], ['git', *args])\n"
        )
        wrapper.chmod(0o755)

    def git(self, repo, *args, check=True):
        return subprocess.run(
            [self.real_git, "-C", str(repo), "-c", "core.hooksPath=/dev/null",
             "-c", "commit.gpgsign=false", *args],
            capture_output=True, text=True, check=check,
        )

    def make_repo(self, name):
        repo = self.base / name
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "config", "user.name", "Fixture")
        self.git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "seed.txt").write_text("tracked seed\n")
        (repo / ".gitignore").write_text("_state/\n")
        self.git(repo, "add", "seed.txt", ".gitignore")
        self.git(repo, "commit", "-qm", "fixture seed")
        return repo

    def add_worktree(self, repo, number, status="complete", promoted=True):
        attempt = f"d-{number:032x}"
        task = f"TASK-prune-external-{number}"
        wt = self.board / attempt
        self.git(repo, "worktree", "add", "-q", "--detach", str(wt))
        relative = f"departments/coding/outbox/{task}-response.md"
        self.registry[task] = {"status": status, "return_artifact": relative}
        (self.state / "active-tasks.json").write_text(json.dumps(self.registry))
        (self.state / "board-dispatch" / f"{task}.{attempt}.dispatch.json").write_text(
            json.dumps({"task_id": task})
        )
        if promoted:
            artifact = self.root / relative
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("promoted result\n")
        return wt, task, relative

    def run_script(self, *flags, **overrides):
        env = dict(
            os.environ, VAULT_ROOT=str(self.root), SQUAD_TEST_ISOLATION="1",
            BOARD_SCRATCH_ROOT=str(self.root / "scratch"),
            PYTHONDONTWRITEBYTECODE="1", PRUNE_TEST_GIT=self.real_git,
            PRUNE_TEST_TRACE=str(self.trace),
            PATH=str(self.base / "bin") + os.pathsep + os.environ["PATH"],
        )
        env.update(overrides)
        return subprocess.run(["bash", str(SCRIPT), *flags], cwd=self.root,
                              env=env, capture_output=True, text=True, timeout=30)

    def calls(self, operation):
        if not self.trace.exists():
            return []
        return [row for row in map(json.loads, self.trace.read_text().splitlines())
                if "worktree" in row and operation in row]

    def repo_line(self, result, repo):
        return next(line for line in result.stdout.splitlines()
                    if line.startswith(f"repo: {repo}  "))

    def test_external_dry_run_then_apply_keeps_live_and_rescues_first(self):
        old, _, _ = self.add_worktree(self.external, 1)
        live, _, _ = self.add_worktree(self.external, 2, status="in-flight")
        rescue, task, relative = self.add_worktree(self.external, 3, promoted=False)
        artifact = rescue / relative
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"only unpromoted result\n")
        saved = self.state / "rescued-worker-artifacts" / task / rescue.name / relative

        report = self.run_script("--dry-run")
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertIn("prunable=1  keep-live=1  NEEDS-RESCUE=1", report.stdout)
        self.assertIn(f"PRUNABLE  {old.name}", report.stdout)
        self.assertIn("prunable=1", self.repo_line(report, self.external))
        self.assertTrue(all(wt.is_dir() for wt in (old, live, rescue)))
        self.assertFalse(saved.exists())
        self.assertFalse(self.calls("remove"))
        self.assertFalse(self.calls("prune"))

        applied = self.run_script("--apply", PRUNE_TEST_RESCUED=str(saved))
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(live.is_dir())
        self.assertFalse(rescue.exists())
        self.assertEqual(saved.read_bytes(), b"only unpromoted result\n")
        self.assertCountEqual(self.calls("remove"), [
            ["-C", str(self.external), "worktree", "remove", "--force", str(old)],
            ["-C", str(self.external), "worktree", "remove", "--force", str(rescue)],
        ])
        self.assertEqual(self.calls("prune"), [["-C", str(self.external), "worktree", "prune"]])
        self.assertIn("removed=2", self.repo_line(applied, self.external))
        registered = self.git(self.external, "worktree", "list", "--porcelain").stdout
        self.assertNotIn(str(old), registered)
        self.assertIn(str(live), registered)
        self.assertNotIn(str(rescue), registered)

    def test_mixed_repositories_relative_gitdir_and_one_prune_per_repo(self):
        second = self.make_repo("second external")
        worktrees = [self.add_worktree(repo, i)[0]
                     for i, repo in enumerate((self.root, self.external, self.external, second), 1)]
        gitfile = worktrees[1] / ".git"
        gitdir = Path(gitfile.read_text().removeprefix("gitdir: ").strip())
        gitfile.write_text(f"gitdir: {os.path.relpath(gitdir, worktrees[1])}\n")
        for wt in worktrees:
            (wt / "seed.txt").write_bytes(b"x" * (2 * 1024 * 1024))
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(wt.exists() for wt in worktrees))
        for repo, count in ((self.root, 1), (self.external, 2), (second, 1)):
            line = self.repo_line(result, repo)
            self.assertIn(f"removed={count}", line)
            reclaimed = re.search(r"reclaimed=([0-9.]+) GiB", line)
            self.assertIsNotNone(reclaimed, line)
            self.assertGreater(float(reclaimed[1]), 0)
        self.assertEqual(sorted(self.calls("prune")), sorted(
            [["-C", str(repo), "worktree", "prune"] for repo in (self.root, self.external, second)]
        ))

    def test_read_only_directories_are_removable_without_following_symlinks(self):
        wt, task, _ = self.add_worktree(self.external, 1)
        live, _, _ = self.add_worktree(self.external, 2, status="in-flight")
        outside = self.base / "outside"
        outside.mkdir()
        outside_mode = stat.S_IMODE(outside.stat().st_mode)
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"outside must survive")
        sentinel.chmod(0o444)
        cache = wt / "module-cache"
        cache.mkdir()
        (cache / "readonly.bin").write_bytes(b"cache")
        (cache / "readonly.bin").chmod(0o444)
        (wt / "outside-link").symlink_to(outside, target_is_directory=True)
        (wt / "hardlink").hardlink_to(sentinel)
        cache.chmod(0o555)
        wt.chmod(0o555)
        live.chmod(0o555)
        self.addCleanup(live.chmod, 0o755)
        self.addCleanup(lambda: wt.chmod(0o755) if wt.exists() else None)
        self.addCleanup(lambda: cache.chmod(0o755) if cache.exists() else None)

        for flag in ("--dry-run", "--preserve"):
            report = self.run_script(flag)
            self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE(wt.stat().st_mode), 0o555)
            self.assertFalse(self.calls("remove"))
            self.assertFalse(self.calls("prune"))
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(wt.exists())
        self.assertEqual(sentinel.read_bytes(), b"outside must survive")
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)
        self.assertEqual(stat.S_IMODE(live.stat().st_mode), 0o555)
        saved = self.state / "rescued-worker-artifacts" / task / wt.name
        self.assertEqual((saved / "module-cache/readonly.bin").read_bytes(), b"cache")
        self.assertEqual(os.readlink(saved / "outside-link"), str(outside))

    def test_failed_preservation_never_changes_permissions_or_removes(self):
        wt, task, _ = self.add_worktree(self.external, 1)
        (wt / "result.txt").write_bytes(b"real result")
        saved = self.state / "rescued-worker-artifacts" / task / wt.name / "result.txt"
        saved.parent.mkdir(parents=True)
        saved.write_bytes(b"truncated")
        wt.chmod(0o555)
        self.addCleanup(wt.chmod, 0o755)
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("RETAINED (preservation incomplete)", result.stdout)
        self.assertTrue(wt.is_dir())
        self.assertEqual(stat.S_IMODE(wt.stat().st_mode), 0o555)
        self.assertFalse(self.calls("remove"))
        self.assertEqual(saved.read_bytes(), b"truncated")

    def test_locked_worktree_stays_registered_after_git_refuses(self):
        wt, _, _ = self.add_worktree(self.external, 1)
        self.git(self.external, "worktree", "lock", str(wt))
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(wt.is_dir())
        self.assertIn("RETAINED (git refused removal)", result.stdout)
        self.assertIn("removed=0", self.repo_line(result, self.external))
        self.assertIn("reclaimed=0.000000 GiB", self.repo_line(result, self.external))
        self.assertIn(str(wt), self.git(self.external, "worktree", "list", "--porcelain").stdout)

    def test_prune_failure_is_reported_with_nonzero_exit(self):
        wt, _, _ = self.add_worktree(self.external, 1)
        result = self.run_script("--apply", PRUNE_TEST_FAIL_PRUNE="1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(wt.exists())
        self.assertIn("fixture prune failure", result.stdout)
        self.assertIn(str(self.external), result.stdout)

    def test_broken_gitdir_keeps_the_tree_and_does_not_fall_back_to_squad(self):
        wt, _, _ = self.add_worktree(self.external, 1)
        (wt / ".git").write_text("gitdir: missing-admin-directory\n")
        wt.chmod(0o555)
        self.addCleanup(wt.chmod, 0o755)
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("owning repository unresolved", result.stdout)
        self.assertTrue(wt.is_dir())
        self.assertEqual(stat.S_IMODE(wt.stat().st_mode), 0o555)
        self.assertFalse(self.calls("remove"))
        self.assertFalse(self.calls("prune"))

    def test_symlink_board_entry_never_removes_its_target(self):
        wt, _, _ = self.add_worktree(self.external, 1)
        target = self.base / "moved-worktree"
        wt.rename(target)
        wt.symlink_to(target, target_is_directory=True)
        target.chmod(0o555)
        self.addCleanup(target.chmod, 0o755)
        result = self.run_script("--apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("owning repository unresolved", result.stdout)
        self.assertTrue(wt.is_symlink())
        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o555)
        self.assertFalse(self.calls("remove"))
        self.assertFalse(self.calls("prune"))


if __name__ == "__main__":
    unittest.main()
