"""Classifier and exit-status regressions without running a cleanup mode.

All files live in temporary fixtures. The shell entry point is report-only;
cleanup statements are exercised separately with preservation and Git mocked.
"""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "bin/prune-board-worktrees.sh"
PYTHON_SOURCE = SCRIPT.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
TREE = ast.parse(PYTHON_SOURCE)
CONTROLLER_MARKER = "Board dispatch was blocked by the controller:"
# September 23 audit cases; preserve their envelope shape without host paths.
BLOCKED_CASES = (
    ("0314-03193f16", "claude", "d-747dedf2842644b4850c415b4059d813", "code integration failed"),
    ("0322-0babe0a7", "claude", "d-63671be777104e04b25b4d3362259d8c", "code integration failed"),
    ("0335-6ff5577f", "gpt-codex", "d-1c7061c70373405992134bd0411c7eb0", "code integration failed"),
    ("0305-b71bb8d8", "gpt-codex", "d-3b5dd45e4d664b4d97d6869e0a5b172e", "CLI timed out"),
)


def envelope(task, lane, attempt, body, status="blocked", padding=""):
    return (
        f"---\nid: {task}-response\nin_response_to: {task}\n"
        f"from: {lane}\nto: chrono\ntype: RESULT\nstatus: {status}\n"
        f"return_artifact: departments/coding/outbox/{task}-response.md\n"
        f"delivery_attempt_id: {attempt}\ndelivery_generation: 1\n"
        f"{padding}---\n\n{body}\n"
    )


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prune-classifier-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = {}
        nodes = [
            node for node in TREE.body
            if (isinstance(node, ast.FunctionDef) and node.name == "promoted")
            or (isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith("BLOCKED_STUB")
                for target in node.targets
            ))
        ]
        namespace = {"pathlib": __import__("pathlib"), "registry": self.registry}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SCRIPT), "exec"), namespace)
        self.promoted = namespace["promoted"]

    def classify(self, body, task="TASK-fixture"):
        artifact = self.root / f"{task}-response.md"
        artifact.write_text(body)
        self.registry[task] = {"return_artifact": str(artifact)}
        return self.promoted(task)

    def test_four_controller_stubs_are_not_promoted(self):
        for suffix, lane, attempt, reason in BLOCKED_CASES:
            task = f"TASK-2026-09-23-{suffix}"
            with self.subTest(task=task):
                body = envelope(task, lane, attempt, f"{CONTROLLER_MARKER} fresh lane {reason}")
                ok, why = self.classify(body, task)
                self.assertFalse(ok)
                self.assertIn("stub at", why)

    def test_both_stub_markers_are_detected_after_long_frontmatter(self):
        for marker in (CONTROLLER_MARKER, "Board dispatch blocked"):
            with self.subTest(marker=marker):
                body = envelope("TASK-long", "claude", "d-1234", marker,
                                padding="diagnostic: " + "x" * 4096 + "\n")
                self.assertGreater(body.index(marker), 400)
                self.assertFalse(self.classify(body)[0])

    def test_legacy_stub_is_not_promoted(self):
        self.assertFalse(self.classify("BOARD DISPATCH BLOCKED: worker failed\n")[0])

    def test_genuine_response_is_promoted_even_with_long_frontmatter(self):
        body = envelope("TASK-real", "claude", "d-1234",
                        "Implemented the requested change; focused tests passed.",
                        status="complete", padding="diagnostic: " + "x" * 4096 + "\n")
        self.assertTrue(self.classify(body)[0])

    def test_worker_authored_blocked_result_is_not_a_controller_stub(self):
        body = envelope("TASK-real", "claude", "d-1234",
                        "Measured the failure; the operator must select the migration policy.")
        self.assertTrue(self.classify(body)[0])

    def test_missing_and_undeclared_artifact_controls(self):
        self.registry["TASK-missing"] = {"return_artifact": str(self.root / "absent.md")}
        self.assertFalse(self.promoted("TASK-missing")[0])
        self.assertEqual(self.promoted("TASK-undeclared"), (True, "no declared artifact"))

    def test_report_distinguishes_worker_residue_from_empty_stub_worktree(self):
        state = self.root / "_state"
        dispatch = state / "board-dispatch"
        dispatch.mkdir(parents=True)
        (self.root / "scripts").mkdir()
        (self.root / "scripts/python").symlink_to(REPO_ROOT / "scripts/python", target_is_directory=True)
        registry = {}
        for number, (suffix, lane, attempt, reason) in enumerate(BLOCKED_CASES):
            task = f"TASK-2026-09-23-{suffix}"
            self.classify(envelope(task, lane, attempt, f"{CONTROLLER_MARKER} {reason}"), task)
            registry[task] = dict(self.registry[task], status="closed")
            (dispatch / f"{task}.{attempt}.dispatch.json").write_text(json.dumps({"task_id": task}))
            wt = state / "board-worktrees" / attempt
            wt.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(wt)], check=True, capture_output=True)
            if number < 3:
                (wt / "worker-result.txt").write_text("only fixture copy\n")
        (state / "active-tasks.json").write_text(json.dumps(registry))
        env = dict(os.environ, VAULT_ROOT=str(self.root), SQUAD_TEST_ISOLATION="1",
                   BOARD_SCRATCH_ROOT=str(self.root / "scratch"), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(["bash", str(SCRIPT), "--report"], cwd=self.root,
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("prunable=1  keep-live=0  NEEDS-RESCUE=3", result.stdout)
        self.assertEqual(result.stdout.count("  RESCUE  "), 3)
        self.assertFalse((state / "rescued-worker-artifacts").exists())
        self.assertEqual(len(list((state / "board-worktrees").glob("d-*"))), 4)
        for artifact in (state / "board-worktrees").glob("*/worker-result.txt"):
            self.assertEqual(artifact.read_text(), "only fixture copy\n")


# Execute the production tail in a child process to observe its actual exit
# status. No shell cleanup flag is used; every mutation boundary is replaced.
CLEANUP_HARNESS = r'''
import ast, json, pathlib, sys, types
source = pathlib.Path(sys.argv[1]).read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
tree = ast.parse(source)
start = next(i for i, node in enumerate(tree.body)
             if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "saved" for t in node.targets))
cases = json.loads(sys.argv[2])
worktrees = pathlib.Path("worktrees")
worktrees.mkdir()
prunable = []
for name in cases:
    wt = worktrees / name
    wt.mkdir()
    (wt / "sentinel").write_text("retained fixture")
    prunable.append((wt, "TASK-" + name, "promoted"))
preserved = []
def preserve(wt, task, attempt, out, **kwargs):
    preserved.append(wt.name)
    print("PRESERVE", wt.name)
    return cases[wt.name] != "unsafe", 0
def git(args, **kwargs):
    assert set(preserved) == set(cases), "removal before all preservation checks"
    if args == ["git", "-C", str(repo), "worktree", "prune"]:
        return types.SimpleNamespace(returncode=0)
    assert args[:6] == ["git", "-C", str(repo), "worktree", "remove", "--force"], args
    name = pathlib.Path(args[6]).name
    assert cases[name] != "unsafe", "unsafe worktree reached removal"
    print("REMOVE", name)
    return types.SimpleNamespace(returncode=int(cases[name] == "refused"),
                                 stderr="fixture refusal: " + name)
def forbid(*args, **kwargs):
    raise AssertionError("unexpected filesystem mutation")
subprocess = types.SimpleNamespace(run=git)
shutil = types.SimpleNamespace(rmtree=forbid, copy2=forbid)
mode = "unit-test"
RESCUE_DIR = "rescue"
rescue, keep = [], []
scratch_root = None
owner, registry = {}, {}
repo = pathlib.Path("owner").resolve()
repos = {name: repo for name in cases}
repo_counts = {repo: {"prunable": len(cases), "keep-live": 0, "rescue": 0,
                     "removed": 0, "bytes": 0}}
def owning_repo(wt):
    if cases[wt.name] == "unresolved":
        return None, "fixture owner changed"
    return repo, ""
def attempt_settled(attempt):
    return True
def prepare_removal(wt):
    assert set(preserved) == set(cases), "chmod before all preservation checks"
    assert cases[wt.name] != "unsafe", "unsafe worktree reached chmod"
    if cases[wt.name] == "chmod-failed":
        raise OSError("fixture chmod denied")
    return 0
report = next(node for node in tree.body
              if isinstance(node, ast.FunctionDef) and node.name == "report_repos")
exec(compile(ast.Module(body=[report], type_ignores=[]), sys.argv[1], "exec"))
exec(compile(ast.Module(body=tree.body[start:], type_ignores=[]), sys.argv[1], "exec"))
'''


class PreservationTests(unittest.TestCase):
    """Fresh and existing rescue bytes must pass the same digest oracle."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prune-hash-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.wt = self.root / "worktree"
        self.wt.mkdir()
        self.src = self.wt / "only-result"
        self.src.write_bytes(b"irreplaceable fixture bytes")
        self.out = self.root / "rescued"
        self.dest = self.out / "TASK-test/d-1234/only-result"
        self.ns = {name: __import__(name) for name in ("os", "stat", "shutil", "hashlib", "pathlib", "json", "tempfile")}
        from disk_hygiene import RESCUE_BULK_NAMES
        self.ns["RESCUE_BULK_NAMES"] = RESCUE_BULK_NAMES
        self.ns["residue"] = lambda wt: [(self.src, "file")]
        nodes = [node for node in TREE.body if isinstance(node, ast.FunctionDef)
                 and node.name in ("_digest", "rescue_selection", "preserve")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SCRIPT), "exec"), self.ns)

    def preserve(self, **kwargs):
        return self.ns["preserve"](self.wt, "TASK-test", "d-1234", self.out, **kwargs)[0]

    def test_verified_fresh_copy_and_existing_copy_controls(self):
        self.assertTrue(self.preserve())
        self.assertEqual(self.dest.read_bytes(), self.src.read_bytes())
        self.assertTrue(self.preserve(verify_only=True))

    def test_bulk_manifest_failure_or_changed_size_retains_source(self):
        bulk = self.wt / "dist/bundle"
        bulk.parent.mkdir()
        bulk.write_bytes(b"build output")
        self.ns["residue"] = lambda wt: [(self.src, "file"), (bulk, "file")]
        with mock.patch.object(self.ns["os"], "replace", side_effect=OSError("disk full")):
            self.assertFalse(self.preserve())
        self.assertTrue(self.src.exists())
        self.assertTrue(bulk.exists())
        self.assertTrue(self.preserve())
        self.assertTrue(self.preserve(verify_only=True))
        bulk.write_bytes(b"different size")
        self.assertFalse(self.preserve(verify_only=True))
        self.assertTrue(self.src.exists())

    def test_manifest_symlink_is_not_overwritten(self):
        bulk = self.wt / "dist/bundle"
        bulk.parent.mkdir()
        bulk.write_bytes(b"build output")
        self.ns["residue"] = lambda wt: [(self.src, "file"), (bulk, "file")]
        manifest = self.out / "TASK-test/d-1234.rescue-manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.symlink_to(self.src)
        self.assertFalse(self.preserve())
        self.assertEqual(self.src.read_bytes(), b"irreplaceable fixture bytes")

    def test_missing_or_mismatched_new_copy_is_not_preserved(self):
        for effect in (lambda *a, **k: None,
                       lambda src, dest, **k: dest.write_bytes(b"truncated")):
            with mock.patch.object(self.ns["shutil"], "copy2", side_effect=effect):
                self.assertFalse(self.preserve())
            self.assertTrue(self.src.exists())
        self.dest.write_bytes(self.src.read_bytes())
        self.assertTrue(self.preserve())

    def test_missing_copy_at_final_boundary_is_retained_without_recopy(self):
        self.assertFalse(self.preserve(verify_only=True))
        self.assertTrue(self.preserve())
        self.dest.unlink()  # this test's own disposable copy
        self.assertFalse(self.preserve(verify_only=True))
        self.assertTrue(self.src.exists())

    def test_existing_wrong_bytes_symlink_and_hardlink_are_not_rescues(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(b"x" * self.src.stat().st_size)
        self.assertFalse(self.preserve())
        self.dest.unlink()
        self.dest.symlink_to(self.src)
        self.assertFalse(self.preserve())
        self.dest.unlink()
        self.dest.hardlink_to(self.src)
        self.assertFalse(self.preserve())


class CleanupStatusTests(unittest.TestCase):
    def run_tail(self, cases):
        with tempfile.TemporaryDirectory(prefix="prune-status-") as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-c", CLEANUP_HARNESS, str(SCRIPT), json.dumps(cases)],
                cwd=directory, capture_output=True, text=True,
            )
            self.assertEqual(result.stderr, "")
            for name in cases:
                self.assertEqual((Path(directory) / "worktrees" / name / "sentinel").read_text(),
                                 "retained fixture")
            return result

    def test_all_refusals_are_reported_before_nonzero_exit(self):
        result = self.run_tail({"d-1": "refused", "d-2": "refused"})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("RETAINED (git refused removal)"), 2)
        self.assertIn("fixture refusal: d-1", result.stdout)
        self.assertIn("fixture refusal: d-2", result.stdout)
        self.assertIn("pruned 0;", result.stdout)

    def test_unsafe_preservation_is_reported_and_never_removed(self):
        result = self.run_tail({"d-1": "unsafe", "d-2": "unsafe"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("RETAINED (preservation incomplete): d-1, d-2", result.stdout)
        self.assertNotIn("REMOVE", result.stdout)
        self.assertIn("pruned 0;", result.stdout)

    def test_partial_success_still_reports_all_retained_items(self):
        result = self.run_tail({"d-1": "unsafe", "d-2": "refused", "d-3": "success"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("RETAINED (preservation incomplete): d-1", result.stdout)
        self.assertIn("RETAINED (git refused removal)  d-2", result.stdout)
        self.assertIn("pruned 1;", result.stdout)

    def test_successful_removal_control_exits_zero(self):
        result = self.run_tail({"d-1": "success", "d-2": "success"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("pruned 2;", result.stdout)
        self.assertNotIn("RETAINED", result.stdout)

    def test_no_candidates_control_exits_zero(self):
        result = self.run_tail({})
        self.assertEqual(result.returncode, 0)
        self.assertIn("pruned 0;", result.stdout)

    def test_owner_change_and_permission_failure_retain_without_removal(self):
        result = self.run_tail({"d-1": "unresolved", "d-2": "chmod-failed"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("fixture owner changed", result.stdout)
        self.assertIn("fixture chmod denied", result.stdout)
        self.assertNotIn("REMOVE", result.stdout)
        self.assertIn("pruned 0;", result.stdout)


if __name__ == "__main__":
    unittest.main()
