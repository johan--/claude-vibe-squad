#!/usr/bin/env python3
"""Phase 0: the reaper must not destroy untracked work.

`bin/prune-board-worktrees.sh` classifies a terminal worktree as `prunable` when
its DECLARED return_artifact was promoted, then force-removes it. Preservation
runs only over the `rescue` list -- so a prunable worktree is deleted without
anyone ever looking for untracked residue inside it.

That residue is exactly where bounty proof-of-concept material lives: untracked,
often not markdown, and the only copy. The operator has lost PoCs this way.

Two further defects compound it:
  - the rescue copier walks `wt.rglob("*.md")` and skips anything whose path
    lacks an `_state` part, so a non-markdown PoC is never copied even when a
    worktree IS rescued;
  - a failed `git worktree remove` falls through to `shutil.rmtree(...,
    ignore_errors=True)`, converting a refusal to delete into a deletion.

Preservation tests use `--preserve`; cleanup tests use `--apply` only inside
their own repository and private scratch root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "bin" / "prune-board-worktrees.sh"
ATTEMPT = "d-00000000000000000000000000000001"
TASK = "TASK-2026-09-01-0000-preservetest"


class PrunePreservesUntrackedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="prune-preserve-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

        # A REAL git repo with a REAL worktree: the preservation census is
        # `git status --porcelain`, so a plain directory would fail the census
        # and (correctly) be retained rather than preserved -- which would test
        # the fail-safe path instead of the one that matters.
        def git(*args, cwd=None):
            subprocess.run(["git", *args], cwd=cwd or self.root, check=True,
                           capture_output=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t"); git("config", "user.name", "t")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        # A real ignore rule: without one, the collapsed-ignored-directory case
        # -- the bug that deleted whole directories while reporting success --
        # is unreachable and the test proves nothing about it.
        (self.root / ".gitignore").write_text("scratch/\n", encoding="utf-8")
        git("add", "seed.txt", ".gitignore"); git("commit", "-qm", "seed")
        self._git = git

        # The script resolves everything from cwd and imports dispatch_log from
        # <cwd>/scripts/python, so the fixture mirrors that shape.
        (self.root / "scripts").mkdir(parents=True)
        os.symlink(REPO_ROOT / "scripts" / "python", self.root / "scripts" / "python")

        self.state = self.root / "_state"
        (self.state / "board-dispatch").mkdir(parents=True)
        self.worktree = self.state / "board-worktrees" / ATTEMPT
        (self.state / "board-worktrees").mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "-q", "-b", ATTEMPT, str(self.worktree))
        self.rescue_dir = self.state / "rescued-worker-artifacts"

        # A promoted declared artifact: this is what makes the worktree
        # "prunable" and therefore eligible for silent removal.
        artifact = self.root / "departments" / "coding" / "outbox"
        artifact.mkdir(parents=True)
        (artifact / f"{TASK}-response.md").write_text("real result\n", encoding="utf-8")

        (self.state / "board-dispatch" / f"{TASK}.{ATTEMPT}.dispatch.json").write_text(
            json.dumps({"task_id": TASK}), encoding="utf-8"
        )
        (self.state / "active-tasks.json").write_text(
            json.dumps(
                {
                    TASK: {
                        "status": "complete",
                        "return_artifact": f"departments/coding/outbox/{TASK}-response.md",
                    }
                }
            ),
            encoding="utf-8",
        )

    def _run_preserve(self) -> subprocess.CompletedProcess:
        env = dict(os.environ, VAULT_ROOT=str(self.root), SQUAD_TEST_ISOLATION="1",
                   BOARD_SCRATCH_ROOT=str(self.root / "_scratch"))
        return subprocess.run(
            ["bash", str(SCRIPT), "--preserve"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _preserved_names(self) -> set[str]:
        if not self.rescue_dir.exists():
            return set()
        return {p.name for p in self.rescue_dir.rglob("*") if p.is_file()}

    def _preserved_bytes(self, relative: str) -> bytes | None:
        """Bytes at the exact preserved PATH, not merely a matching basename."""
        hit = list(self.rescue_dir.rglob(relative))
        return hit[0].read_bytes() if hit else None

    def _run_apply(self) -> subprocess.CompletedProcess:
        env = dict(os.environ, VAULT_ROOT=str(self.root), SQUAD_TEST_ISOLATION="1",
                   BOARD_SCRATCH_ROOT=str(self.root / "_scratch"))
        return subprocess.run(
            ["bash", str(SCRIPT), "--apply"],
            cwd=self.root, env=env, capture_output=True, text=True, check=False,
        )

    def test_bulk_is_omitted_and_manifest_records_sizes(self):
        (self.worktree / "notes.md").write_bytes(b"keep document")
        for relative, data in (("dist/bundle.js", b"1234"),
                               ("nested/node_modules/pkg/code.js", b"1234567")):
            path = self.worktree / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
        # Same-named regular files are work product, not bulk directories.
        (self.worktree / "nested/dist").write_bytes(b"keep regular file")
        result = self._run_preserve()
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = self.rescue_dir / TASK / ATTEMPT
        self.assertEqual((saved / "notes.md").read_bytes(), b"keep document")
        self.assertEqual((saved / "nested/dist").read_bytes(), b"keep regular file")
        self.assertFalse((saved / "dist").exists())
        self.assertFalse((saved / "nested/node_modules").exists())
        manifest = self.rescue_dir / TASK / f"{ATTEMPT}.rescue-manifest.json"
        self.assertEqual(json.loads(manifest.read_text())["skipped_bulk"], [
            {"path": "dist", "size_bytes": 4},
            {"path": "nested/node_modules", "size_bytes": 7},
        ])
        result = self._run_apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.worktree.exists())
        self.assertTrue(manifest.exists())
        self.assertEqual((saved / "notes.md").read_bytes(), b"keep document")

    def test_untracked_non_markdown_poc_is_preserved(self) -> None:
        """The failure that costs real work: a PoC in a promoted worktree."""
        (self.worktree / "poc.py").write_text("# exploit\n", encoding="utf-8")
        result = self._run_preserve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "poc.py",
            self._preserved_names(),
            "an untracked non-markdown file in a PRUNABLE worktree was not "
            f"preserved; the worktree is force-removed next.\n{result.stdout}",
        )

    def test_scratch_sweep_preserves_foreign_and_live_roots_but_removes_owned_orphan(self):
        from test_disk_hygiene_local_config import provision_config

        provision_config(self.root)
        scratch = self.root / "_scratch"
        names = ("d-foreign", "d-11111111111111111111111111111111", "d-22222222222222222222222222222222")
        for name in names:
            (scratch / name).mkdir(parents=True)
            (scratch / name / "sentinel").write_text(name)
        old = time.time() - 25 * 3600
        for entry in scratch.rglob("*"):
            os.utime(entry, (old, old))
        registry_path = self.state / "active-tasks.json"
        registry = json.loads(registry_path.read_text())
        for name, status in zip(names[1:], ("in-flight", "complete")):
            task = f"TASK-scratch-{name}"
            registry[task] = {"status": status}
            (self.state / "board-dispatch" / f"{task}.{name}.dispatch.json").write_text(json.dumps({"task_id": task}))
        registry_path.write_text(json.dumps(registry))
        # The sandbox cannot census host processes; keep this fixture's census idle.
        fakebin = self.root / "fixturebin"
        fakebin.mkdir()
        (fakebin / "ps").write_text("#!/bin/sh\necho fixture-idle-process\n")
        (fakebin / "ps").chmod(0o755)
        with mock.patch.dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ["PATH"]):
            result = self._run_apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((scratch / names[0] / "sentinel").is_file())
        self.assertTrue((scratch / names[1] / "sentinel").is_file())
        self.assertFalse((scratch / names[2]).exists())
        self.assertIn("scratch: swept 1 orphaned attempt root(s)", result.stdout)

    def test_fixture_cannot_sweep_an_external_scratch_root(self):
        with tempfile.TemporaryDirectory(prefix="sibling-live-scratch-") as sibling:
            root = Path(sibling)
            attempt = root / ATTEMPT
            attempt.mkdir()
            sentinel = attempt / "live-sibling"
            sentinel.write_text("must survive")
            # This name IS owned and terminal in our fixture, so ownership-only
            # would delete it after pruning. Root confinement must stop that.
            env = dict(os.environ, VAULT_ROOT=str(self.root), SQUAD_TEST_ISOLATION="1",
                       BOARD_SCRATCH_ROOT=str(root))
            result = subprocess.run(["bash", str(SCRIPT), "--apply"], cwd=self.root,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sentinel.read_text(), "must survive")

    def test_fixture_default_scratch_root_is_disabled(self):
        env = dict(os.environ, VAULT_ROOT=str(self.root), SQUAD_TEST_ISOLATION="1")
        env.pop("BOARD_SCRATCH_ROOT", None)
        result = subprocess.run(["bash", str(SCRIPT), "--apply"], cwd=self.root,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("scratch: swept", result.stdout)

    def test_in_tree_markdown_outside_state_is_preserved(self) -> None:
        """The rescue filter skips any path without an `_state` part."""
        p = self.worktree / "departments" / "coding" / "outbox"
        p.mkdir(parents=True)
        (p / f"{TASK}-response.md").write_text("worker output\n", encoding="utf-8")
        result = self._run_preserve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            f"{TASK}-response.md",
            self._preserved_names(),
            f"in-tree markdown outside _state was not preserved.\n{result.stdout}",
        )

    def test_contents_of_an_ignored_directory_are_preserved(self) -> None:
        """Guards the `--ignored=matching` -> `traditional` change.

        `matching` collapses an ignored directory to one `scratch/` entry and
        omits its contents; `traditional` lists the files. Mutating the flag
        back makes this fail.
        """
        scratch = self.worktree / "scratch"
        scratch.mkdir()
        (scratch / "payload.bin").write_bytes(b"\x00PAYLOAD\xff")
        result = self._run_preserve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._preserved_bytes("payload.bin"), b"\x00PAYLOAD\xff",
            f"ignored-directory contents were not preserved.\n{result.stdout}",
        )

    def test_contents_of_a_nested_repository_are_preserved(self) -> None:
        """A submodule/nested repo reports as a bare `nested/` DIRECTORY entry.

        Measured: even under --ignored=traditional, git emits `?? nested/`
        rather than its files. Without directory expansion, is_file() discards
        that entry, the census reports success, and --force deletes the repo
        and everything in it. This is the case expansion exists for -- an
        earlier version of this test used an ignored directory instead and
        passed with expansion disabled, proving nothing.
        """
        nested = self.worktree / "nested"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=nested,
                       check=True, capture_output=True)
        (nested / "poc.py").write_bytes(b"NESTED-POC")
        result = self._run_preserve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._preserved_bytes("poc.py"), b"NESTED-POC",
            "contents of a nested repository were not preserved; the whole "
            f"directory is deleted next.\n{result.stdout}",
        )

    def test_preserved_copy_is_byte_exact_at_its_relative_path(self) -> None:
        body = b"line1\nline2\n\x00binary\n"
        (self.worktree / "poc.py").write_bytes(body)
        result = self._run_preserve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._preserved_bytes("poc.py"), body,
            f"preserved copy differs from the source.\n{result.stdout}",
        )

    def test_apply_retains_a_worktree_whose_residue_cannot_be_preserved(self) -> None:
        """A destination holding DIFFERENT bytes must block removal.

        The previous version treated `dest.exists()` as success without reading
        it, so a copy truncated by a full disk was skipped on the next run and
        the intact source deleted.
        """
        (self.worktree / "poc.py").write_bytes(b"REAL")
        planted = self.rescue_dir / TASK / ATTEMPT / "poc.py"
        planted.parent.mkdir(parents=True)
        planted.write_bytes(b"TRUNCATED")
        result = self._run_apply()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(
            self.worktree.exists(),
            f"worktree was removed despite unpreservable residue.\n{result.stdout}",
        )
        self.assertEqual(planted.read_bytes(), b"TRUNCATED",
                         "the mismatched destination was silently overwritten")

    def test_removal_never_falls_back_to_rmtree(self) -> None:
        """A refused `git worktree remove` must retain, never path-delete."""
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("for wt, task, _ in prunable:")
        end = source.index("# Sweep orphaned per-attempt build scratch", start)
        # Strip comments before scanning. The removal block deliberately NAMES
        # the old fallback in a comment explaining why it was removed, and a
        # substring check would match that explanation -- the same trap as
        # test_lane_agy_repoint.py:83, which pins a function by counting
        # occurrences in source text and is satisfied by the `def` line alone.
        removal_block = "\n".join(
            line for line in source[start:end].splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotIn(
            "shutil.rmtree",
            removal_block,
            "the rmtree fallback converts a failed git removal into an "
            "unconditional deletion of the worktree and everything in it. "
            "(The scratch/codex-home sweep may use rmtree -- that is build "
            "cache by construction; this assertion covers only worktrees.)",
        )


if __name__ == "__main__":
    unittest.main()


class BlindCensusIsNotAnEmptyCensusTests(PrunePreservesUntrackedTests):
    """git reports "I could not look here" as a WARNING with exit 0.

    Measured:

        $ git status --porcelain=v1 -z -uall --ignored=traditional
        warning: could not open directory 'locked/': Permission denied
        rc=0                          # and NO entry emitted for locked/

    So `residue()` returned a list with the whole subtree missing, `preserve()`
    reported (True, 0) -- "safe, nothing to preserve" -- and the worktree went
    to removal with a green receipt saying `preserved 0 residue file(s)`.

    A census that ran BLIND over a subtree is the same fact as a census that
    did not run, wearing a success code. The only reason this has not cost a
    PoC is that a mode-000 directory also blocks `git worktree remove` -- the
    data survived by accident of POSIX, not by design. Any cause where git
    cannot readdir at census time but the tree is removable moments later
    (transient EIO, fd exhaustion, a mount that reappears) loses it silently.

    Catches: reverting to `if r.returncode != 0` alone.
    """

    def test_an_unreadable_subtree_makes_the_census_untrusted(self) -> None:
        locked = self.worktree / "locked"
        locked.mkdir()
        (locked / "poc.py").write_bytes(b"SECRET-POC")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)
        result = self._run_apply()
        self.assertEqual(result.returncode, 1, result.stderr)
        # Assert on WHY it was retained, not merely THAT it was. A mode-000
        # directory also blocks `git worktree remove`, so the worktree survives
        # either way and an existence check passes against the unfixed code --
        # which is exactly the accident this test exists to stop relying on.
        # "preservation incomplete" is the by-design path; "git refused
        # removal" is the accident.
        self.assertIn(
            "RETAINED (preservation incomplete)", result.stdout,
            "the census read past an unreadable subtree and reported the "
            "worktree safe to remove; it survived only because POSIX also "
            f"blocked the delete.\n{result.stdout}",
        )

    # --- classification: rescue must describe the WORKTREE, not the registry ---
    #
    # `promoted()` asks whether the declared return_artifact sits at its
    # declared path. That is a question about the registry, and it was the ONLY
    # input to the prunable/rescue decision -- so a cancelled task (which never
    # had an artifact) and a canary (which never produced one) were flagged
    # NEEDS-RESCUE forever and the nightly could never clear them. Measured on
    # the maintainer's tree: 9 worktrees, 175 MB, prunable=0 keep-live=0
    # NEEDS-RESCUE=9, every one of them holding zero residue. A 100% false
    # positive rate, growing by one per cancelled or canary task.
    #
    # The cost is not the disk. It is that a REAL rescue -- untracked bounty
    # PoC material, which is the only copy -- arrives as the tenth line in a
    # list of nine permanent false alarms.

    def _run_report(self) -> subprocess.CompletedProcess:
        env = dict(os.environ, VAULT_ROOT=str(self.root))
        return subprocess.run(
            ["bash", str(SCRIPT)], cwd=self.root, env=env,
            capture_output=True, text=True, check=False,
        )

    def _unpromote(self) -> None:
        """Terminal task whose declared artifact is not at its declared path."""
        (self.root / "departments" / "coding" / "outbox"
         / f"{TASK}-response.md").unlink()

    def test_an_empty_worktree_with_a_missing_artifact_is_prunable(self) -> None:
        """Nothing inside means nothing to rescue, whatever the registry says."""
        self._unpromote()
        result = self._run_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prunable=1", result.stdout, result.stdout)
        self.assertIn("NEEDS-RESCUE=0", result.stdout, result.stdout)

    def test_residue_still_rescues_when_the_artifact_is_missing(self) -> None:
        """Regression guard: the fix must not prune a worktree with work in it.

        Without this, 'empty means prunable' could be widened into 'terminal
        means prunable' and the reaper would delete the only copy of a PoC.
        """
        self._unpromote()
        (self.worktree / "poc.bin").write_bytes(b"ONLY-COPY")
        result = self._run_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NEEDS-RESCUE=1", result.stdout, result.stdout)
        self.assertIn("prunable=0", result.stdout, result.stdout)

    def test_an_untrusted_census_is_never_prunable(self) -> None:
        """Fail closed. A census that could not run is not evidence of empty.

        This is the same defect the residue() census already guards: git
        reports an unreadable directory as a WARNING with exit 0 and emits no
        entry for it, so a blind scan and an empty worktree look identical.
        Reusing residue() for classification inherits that guard; asserting it
        here stops a later 'simplification' to a bare `git status` check.
        """
        self._unpromote()
        locked = self.worktree / "locked"
        locked.mkdir()
        (locked / "poc.py").write_bytes(b"SECRET-POC")
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o755)
        result = self._run_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NEEDS-RESCUE=1", result.stdout, result.stdout)
        self.assertIn("prunable=0", result.stdout, result.stdout)

    def test_system_generated_residue_alone_does_not_block_pruning(self) -> None:
        """Interpreter cache and launch scaffolding are not worker work product.

        Measured on the maintainer's tree: 667 of 673 residue files across nine
        worktrees were these, so every terminal worktree looked like it held
        work and prunable stayed permanently 0.
        """
        self._unpromote()
        (self.worktree / "allowed.txt").write_text("scope\n", encoding="utf-8")
        (self.worktree / ".trusted-task-overlay.md").write_text("x\n", encoding="utf-8")
        cache = self.worktree / "scripts" / "python" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.cpython-313.pyc").write_bytes(b"\x00bytecode")
        home = self.worktree / "home" / "Library" / "Caches"
        home.mkdir(parents=True)
        (home / "cached.pyc").write_bytes(b"\x00bytecode")
        result = self._run_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prunable=1", result.stdout, result.stdout)
        self.assertIn("NEEDS-RESCUE=0", result.stdout, result.stdout)

    def test_real_residue_beside_system_residue_still_rescues(self) -> None:
        """The exclusion must not swallow work that sits next to cache.

        Without this, widening the list would silently start deleting PoCs that
        happen to share a worktree with a __pycache__ directory -- which every
        worktree has.
        """
        self._unpromote()
        cache = self.worktree / "scripts" / "python" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "mod.cpython-313.pyc").write_bytes(b"\x00bytecode")
        (self.worktree / "poc.bin").write_bytes(b"ONLY-COPY")
        result = self._run_report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NEEDS-RESCUE=1", result.stdout, result.stdout)
        self.assertIn("prunable=0", result.stdout, result.stdout)

    def test_preservation_still_copies_system_residue_when_rescuing(self) -> None:
        """The exclusion is CLASSIFICATION only.

        Non-bulk system files still copy. The explicit bulk policy is tested
        separately; classification alone must not exclude other files.
        """
        self._unpromote()
        (self.worktree / ".trusted-task-overlay.md").write_bytes(b"launch context")
        (self.worktree / "poc.bin").write_bytes(b"ONLY-COPY")
        result = self._run_preserve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._preserved_bytes("poc.bin"), b"ONLY-COPY")
        self.assertEqual(
            self._preserved_bytes(".trusted-task-overlay.md"), b"launch context",
            "preservation must copy non-bulk system residue too; classification only "
            "decides WHETHER to rescue, never WHAT to copy",
        )

    def test_a_readable_worktree_is_still_prunable(self) -> None:
        """Control: the fix must not retain every worktree.

        Without this, 'return None on any stderr' would look correct while
        making the reaper useless -- the over-tight-gate failure.
        """
        (self.worktree / "poc.py").write_bytes(b"REAL")
        result = self._run_apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._preserved_bytes("poc.py"), b"REAL",
            f"a clean worktree was not preserved+pruned normally.\n{result.stdout}",
        )
