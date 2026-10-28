"""External WORK repositories retain the squad CONFIG/mailbox boundary."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/python"))
import board_process_truth as bpt
import dispatch_context_builder as dcb
import dispatch_preflight as preflight
import launch_hygiene as hygiene
import worktree_isolation as wti
import test_dispatch_context_builder as context_tests

TASK = "TASK-2026-09-19-1815-external"
ATTEMPT = "d-" + "1" * 32
RESPONSE = f"departments/coding/outbox/{TASK}-response.md"


def git(root, *args):
    result = subprocess.run(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args], cwd=root,
        capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    return result.stdout.strip()


def init_repo(root, branch):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", branch)
    git(root, "config", "user.name", "External Work Test")
    git(root, "config", "user.email", "external@example.test")
    (root / "code.txt").write_text("base\n")
    git(root, "add", "code.txt")
    git(root, "commit", "-q", "-m", "base")
    return root


class WorkRepoAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="external-work-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root, self.packet = context_tests.DispatchContextBuilderTests()._fake_repo_for_lane(
            self.base, lane="codex", model="gpt-codex"
        )
        self.original_packet = self.packet.read_text()
        self.work = init_repo(self.base / "client", "client-main")
        self.env = mock.patch.dict(os.environ, {"SQUAD_BASE_BRANCH": "squad-only"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def bind(self, value):
        self.packet.write_text(self.original_packet.replace("---\n", f"---\nwork_repo: {value}\n", 1))

    def build(self):
        with mock.patch.dict(dcb.LANE_CLI_PATHS, {"codex": Path("/bin/sh")}):
            return dcb.build_context(self.root, self.packet, attempt_id=ATTEMPT,
                                     generation=1, now=1789867000, nonce="b" * 64)

    def refusal(self, value, message):
        self.bind(value)
        with self.assertRaisesRegex(dcb.DispatchContextError, "work_repo.*" + message):
            self.build()
        verdict = preflight.evaluate_packet(self.root, self.packet)
        self.assertEqual(verdict.decision, "deny")
        self.assertRegex(verdict.refusals[0]["message"], "work_repo.*" + message)

    def test_absent_field_context_json_matches_current_legacy_contract(self):
        # Pin the current no-work_repo contract, with only file digests stubbed
        # and the disposable squad-root prefix normalized. Compare the whole JSON.
        with mock.patch.object(dcb, "_sha256_file", return_value="a" * 64):
            actual = json.dumps(self.build(), sort_keys=True, indent=2)
        actual = actual.replace(str(self.root.resolve()), "__SQUAD_ROOT__") + "\n"
        fixture = Path(__file__).parent / "fixtures/external_work_repo_legacy_context.json"
        self.assertEqual(actual, fixture.read_text())
        self.assertIsNone(dcb.resolve_work_repo(self.root, {}))

    def test_present_field_binds_work_current_branch_and_keeps_squad_configuration(self):
        self.bind(self.work)
        context = self.build()
        authority = context["authority"]
        self.assertEqual(authority["work_repo_root"], str(self.work))
        self.assertEqual(authority["work_base_branch"], "client-main")
        self.assertEqual(authority["repo_root"], str(self.root))
        self.assertEqual(authority["pool_root"], str(self.root / "_state/board-worktrees"))
        for key in ("canonical_role_path", "lane_overlay_path"):
            self.assertTrue(Path(authority[key]).is_relative_to(self.root))
        self.assertIn(str(self.work), context["task_prompt"])
        self.assertEqual(preflight.evaluate_packet(self.root, self.packet).decision, "allow")
        self.assertEqual(wti.dispatch_work_repository(authority), (self.work, "client-main", True))

    def test_context_revalidates_work_repo_after_preflight(self):
        self.bind(self.work)
        self.assertEqual(preflight.evaluate_packet(self.root, self.packet).decision, "allow")
        git(self.work, "checkout", "--detach", "-q")
        with self.assertRaisesRegex(dcb.DispatchContextError, "work_repo.*detached"):
            self.build()

    def test_builder_check_ignore_uses_work_repo_with_squad_only_ignore_control(self):
        init_repo(self.root, "squad-only")
        (self.root / ".gitignore").write_text("squad-ignored.txt\n")
        self.original_packet = self.original_packet.replace(
            "write_scope: [_state/canary/]", "write_scope: [_state/canary/, squad-ignored.txt]"
        )
        self.packet.write_text(self.original_packet)
        with self.assertRaisesRegex(dcb.DispatchContextError, "git-ignored"):
            self.build()
        self.bind(self.work)
        self.assertIn("squad-ignored.txt", self.build()["authority"]["write_paths"])
        (self.work / ".gitignore").write_text("squad-ignored.txt\n")
        with self.assertRaisesRegex(dcb.DispatchContextError, "git-ignored"):
            self.build()

    def test_prepared_packet_check_ignore_uses_work_repo(self):
        init_repo(self.root, "squad-only")
        (self.root / ".gitignore").write_text("squad-ignored.txt\n")
        self.original_packet = self.original_packet.replace(
            "write_scope: [_state/canary/]", "write_scope: [_state/canary/, squad-ignored.txt]"
        )
        source = (ROOT / "bin/send-task.sh").read_text()
        block = source.split("validate_unpromoted_write_scope() {\n", 1)[1].split("\n}\n", 1)[0]
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts/python").symlink_to(ROOT / "scripts/python", target_is_directory=True)
        env = {**os.environ, "TASK_FILE": str(self.packet), "VAULT_ROOT": str(self.root)}
        self.packet.write_text(self.original_packet)
        control = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True)
        self.assertEqual(control.returncode, 1, control.stdout + control.stderr)
        self.bind(self.work)
        result = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("git-ignored write_scope", result.stderr)

    def test_prepared_packet_ignores_environment_import_override(self):
        self.bind(self.work)
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "scripts/python").symlink_to(ROOT / "scripts/python", target_is_directory=True)
        poison = self.base / "untrusted/dispatch_context_builder.py"
        poison.parent.mkdir()
        poison.write_text('raise RuntimeError("environment import override executed")\n')
        source = (ROOT / "bin/send-task.sh").read_text()
        block = source.split("validate_unpromoted_write_scope() {\n", 1)[1].split("\n}\n", 1)[0]
        env = {**os.environ, "TASK_FILE": str(self.packet), "VAULT_ROOT": str(self.root),
               "DISPATCH_CONTEXT_BUILDER": str(poison)}
        result = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Positive control: the fixture's override really would fail if imported.
        control = subprocess.run([sys.executable, "-B", str(poison)], capture_output=True, text=True)
        self.assertNotEqual(control.returncode, 0)
        self.assertIn("environment import override executed", control.stderr)

    def test_repository_roots_preamble_is_owned_only_by_context_builder(self):
        self.bind(self.work)
        context = self.build()
        prompt = context["task_prompt"]
        self.assertEqual(prompt.count("## Repository roots"), 1)
        source = (ROOT / "bin/board-supervisor.sh").read_text()
        block = source[source.index("lane_config_root ="):source.index("# --- V113-18:")]
        namespace = {"repo_path": self.root, "external_work_repo": True,
                     "handle": SimpleNamespace(worktree_root=self.base / "attempt"),
                     "execution_kind": "lane", "lane": "codex", "trusted_task_prompt": prompt}
        exec(compile(block, "supervisor-root-prompt", "exec"), namespace)
        self.assertEqual(namespace["trusted_task_prompt"].encode(), prompt.encode())
        self.assertEqual(namespace["worker_cwd"], self.base / "attempt")

    def test_relative_path_refused_by_preflight_and_builder(self):
        self.refusal("../client", "absolute")

    def test_nonexistent_path_refused_by_preflight_and_builder(self):
        self.refusal(self.base / "missing", "exist")

    def test_non_repository_refused_by_preflight_and_builder(self):
        empty = self.base / "empty"
        empty.mkdir()
        self.refusal(empty, "Git checkout")

    def test_linked_worktree_refused_by_preflight_and_builder(self):
        linked = self.base / "linked"
        git(self.work, "worktree", "add", "-q", "-b", "linked", str(linked))
        self.refusal(linked, "MAIN checkout")

    def test_detached_head_refused_by_preflight_and_builder(self):
        git(self.work, "checkout", "--detach", "-q")
        self.refusal(self.work, "detached HEAD")

    def test_path_inside_squad_root_refused_by_preflight_and_builder(self):
        nested = init_repo(self.root / "nested", "nested-main")
        self.refusal(nested, "inside the squad root")

    def test_squad_root_itself_refused_when_field_present(self):
        self.refusal(self.root, "inside the squad root")

    def test_symlink_cannot_hide_path_inside_squad_root(self):
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.refusal(alias, "inside the squad root")

    def test_subdirectory_of_checkout_refused(self):
        subdir = self.work / "subdir"
        subdir.mkdir()
        self.refusal(subdir, "checkout root")

    def test_present_empty_field_is_not_absence(self):
        self.refusal("", "absolute")


class ExternalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="external-integration-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.squad = init_repo(self.base / "squad", "squad-only")
        self.work = init_repo(self.base / "work", "client-main")
        self.authority = {
            "task_id": TASK, "attempt_id": ATTEMPT, "generation": 1,
            "repo_root": str(self.squad), "pool_root": str(self.squad / "_state/pool"),
            "work_repo_root": str(self.work), "work_base_branch": "client-main",
            "write_paths": ["code.txt", RESPONSE],
            "expected_result_path": RESPONSE, "expected_outbox_path": RESPONSE,
        }
        self.env = mock.patch.dict(os.environ, {"SQUAD_BASE_BRANCH": "squad-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        root, branch, external = wti.dispatch_work_repository(self.authority)
        self.pool = wti.WorktreePool(root, Path(self.authority["pool_root"]),
                                     base_branch=branch, external_work_repo=external)
        self.handle = self.pool.provision(TASK, ATTEMPT)
        self.before = git(self.work, "rev-parse", "refs/heads/client-main")

    def change(self, path="code.txt", content="worker\n"):
        target = self.handle.worktree_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self, *paths):
        git(self.handle.worktree_root, "add", "--", *paths)
        git(self.handle.worktree_root, "commit", "-q", "-m", "worker")

    def integrate(self, **kwargs):
        return wti.integrate_worktree_commits(self.handle, self.authority["write_paths"],
                                             exclude_paths=(RESPONSE,), **kwargs)

    def test_pool_repo_and_immutable_base_are_work_repo_despite_squad_environment(self):
        self.assertEqual(self.handle.repo_root, self.work)
        self.assertEqual(self.handle.base_branch, "client-main")
        self.assertEqual(self.handle.base_commit, self.before)
        self.assertTrue(self.handle.external_work_repo)
        self.assertEqual(git(self.handle.worktree_root, "rev-parse", "--git-common-dir"),
                         str(self.work / ".git"))

    def test_work_branch_is_not_shadowed_by_a_same_named_tag(self):
        git(self.work, "tag", "client-main", self.before)
        (self.work / "code.txt").write_text("advanced base\n")
        git(self.work, "add", "code.txt")
        git(self.work, "commit", "-q", "-m", "advance work branch")
        current = git(self.work, "rev-parse", "refs/heads/client-main")
        handle = self.pool.provision(TASK, "d-" + "3" * 32)
        self.assertNotEqual(current, self.before)
        self.assertEqual(handle.base_commit, current)

    def test_work_binding_is_not_rederived_when_checkout_branch_changes(self):
        git(self.work, "checkout", "-q", "-b", "different")
        self.assertEqual(wti.dispatch_work_repository(self.authority), (self.work, "client-main", True))

    def test_integration_creates_task_branch_without_moving_base_or_pushing(self):
        remote = self.base / "empty-remote.git"
        git(self.base, "init", "-q", "--bare", str(remote))
        git(self.work, "remote", "add", "origin", str(remote))
        self.change()
        self.change(RESPONSE, "Uncommitted response artifact.\n")
        with mock.patch.object(wti.subprocess, "run", wraps=subprocess.run) as calls:
            wti.commit_worker_residue(self.handle, self.authority["write_paths"],
                                      exclude_paths=(RESPONSE,))
            receipt = self.integrate()
        self.assertEqual(receipt.target_branch, f"board/{TASK}")
        self.assertEqual(receipt.target_after, receipt.integration_commit)
        self.assertEqual(git(self.work, "rev-parse", "refs/heads/client-main"), self.before)
        self.assertEqual(git(self.work, "branch", "--show-current"), "client-main")
        self.assertEqual((self.work / "code.txt").read_text(), "base\n")
        self.assertEqual(git(self.work, "show", f"board/{TASK}:code.txt"), "worker")
        self.assertNotIn(RESPONSE, git(self.work, "ls-tree", "-r", "--name-only", receipt.target_after).splitlines())
        self.assertEqual(git(remote, "for-each-ref"), "")
        self.assertTrue(calls.call_args_list)
        self.assertFalse(any("push" in call.args[0] for call in calls.call_args_list))

    def test_committed_response_artifact_is_refused(self):
        self.change(RESPONSE)
        self.commit(RESPONSE)
        with self.assertRaisesRegex(wti.WorktreeIsolationError, "bridge-owned"):
            self.integrate()
        self.assertEqual(git(self.work, "rev-parse", "client-main"), self.before)

    def test_out_of_scope_commit_is_refused_even_when_later_reverted(self):
        self.change("outside.txt")
        self.commit("outside.txt")
        git(self.handle.worktree_root, "revert", "--no-edit", "HEAD")
        with self.assertRaisesRegex(wti.WorktreeIsolationError, "outside.*scope"):
            self.integrate()

    def test_deletion_is_refused_without_authorized_delete_paths(self):
        git(self.handle.worktree_root, "rm", "code.txt")
        git(self.handle.worktree_root, "commit", "-q", "-m", "delete fixture file")
        with self.assertRaisesRegex(wti.WorktreeIsolationError, "delet"):
            self.integrate()

    def test_authorized_deletion_uses_same_manifest_guard(self):
        git(self.handle.worktree_root, "rm", "code.txt")
        git(self.handle.worktree_root, "commit", "-q", "-m", "authorized fixture deletion")
        receipt = self.integrate(authorized_delete_paths=("code.txt",))
        self.assertEqual(receipt.deleted_paths, ("code.txt",))
        self.assertEqual(git(self.work, "rev-parse", "client-main"), self.before)

    def test_merge_history_is_refused(self):
        git(self.work, "branch", "side", self.before)
        self.change()
        self.commit("code.txt")
        # A two-parent commit is rejected even if its tree stays in scope.
        tree = git(self.handle.worktree_root, "rev-parse", "HEAD^{tree}")
        head = git(self.handle.worktree_root, "rev-parse", "HEAD")
        merge = git(self.work, "commit-tree", tree, "-p", head, "-p", self.before, "-m", "merge")
        git(self.handle.worktree_root, "reset", "--hard", merge)
        with self.assertRaisesRegex(wti.WorktreeIsolationError, "linear|merge"):
            self.integrate()

    def test_structural_segment_is_refused(self):
        self.authority["write_paths"] = ["nested/.githooks/pre-commit", RESPONSE]
        self.change("nested/.githooks/pre-commit", "#!/bin/sh\nexit 0\n")
        self.commit("nested/.githooks/pre-commit")
        with self.assertRaisesRegex(wti.WorktreeIsolationError, "structural|git"):
            self.integrate()

    def test_existing_task_branch_is_not_overwritten(self):
        git(self.work, "branch", f"board/{TASK}", self.before)
        self.change()
        self.commit("code.txt")
        with mock.patch.object(wti, "_run_git", wraps=wti._run_git) as calls:
            with self.assertRaisesRegex(wti.WorktreeIsolationError, "atomic integration failed"):
                self.integrate()
        updates = [call.args[0] for call in calls.call_args_list if call.args[0][0] == "update-ref"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][-1], "0" * len(self.before))
        self.assertEqual(git(self.work, "rev-parse", f"board/{TASK}"), self.before)

    def test_zero_old_oid_allows_new_task_branch(self):
        self.change()
        self.commit("code.txt")
        with mock.patch.object(wti, "_run_git", wraps=wti._run_git) as calls:
            result = self.integrate()
        updates = [call.args[0] for call in calls.call_args_list if call.args[0][0] == "update-ref"]
        self.assertEqual(updates, [["update-ref", f"refs/heads/board/{TASK}",
                                    result.integration_commit, "0" * len(result.integration_commit)]])
        self.assertEqual(git(self.work, "rev-parse", f"board/{TASK}"), result.integration_commit)
        self.assertEqual(git(self.work, "rev-parse", "client-main"), self.before)

    def test_external_release_removes_only_integrated_attempt_ref(self):
        self.change()
        self.commit("code.txt")
        result = self.integrate()
        self.pool.release(self.handle)
        refs = git(self.work, "for-each-ref", "--format=%(refname) %(objectname)")
        self.assertEqual(refs, f"refs/heads/board/{TASK} {result.integration_commit}\n"
                              f"refs/heads/client-main {self.before}")
        self.assertFalse(self.handle.worktree_root.exists())
        self.assertEqual(git(self.work, "branch", "--show-current"), "client-main")

    def test_external_release_removes_empty_attempt_ref_without_board_branch(self):
        self.pool.release(self.handle)
        self.assertEqual(git(self.work, "for-each-ref", "--format=%(refname) %(objectname)"),
                         f"refs/heads/client-main {self.before}")

    def test_external_release_preserves_unmerged_recovery_ref(self):
        self.change()
        self.commit("code.txt")
        worker = git(self.handle.worktree_root, "rev-parse", "HEAD")
        self.pool.release(self.handle)
        self.assertEqual(git(self.work, "rev-parse", f"refs/heads/{self.handle.branch}"), worker)
        self.assertEqual(git(self.work, "rev-parse", "client-main"), self.before)

    def test_external_release_refuses_to_delete_concurrently_updated_ref(self):
        self.change()
        self.commit("code.txt")
        result = self.integrate()
        ref = f"refs/heads/{self.handle.branch}"
        original_run = wti._run_git
        raced = []

        def update_before_delete(args, **kwargs):
            if args[:4] == ["update-ref", "--no-deref", "-d", ref]:
                commit = git(self.work, "commit-tree", f"{result.worker_head}^{{tree}}",
                             "-p", result.worker_head, "-m", "concurrent recovery evidence")
                git(self.work, "update-ref", ref, commit, result.worker_head)
                raced.append(commit)
            return original_run(args, **kwargs)

        with mock.patch.object(wti, "_run_git", side_effect=update_before_delete):
            self.pool.release(self.handle)
        self.assertEqual(len(raced), 1)
        self.assertEqual(git(self.work, "rev-parse", ref), raced[0])
        self.assertEqual(git(self.work, "rev-parse", f"board/{TASK}"), result.integration_commit)

    def test_external_release_never_follows_attempt_symbolic_ref_into_base(self):
        ref = f"refs/heads/{self.handle.branch}"
        original_run = wti._run_git
        replaced = []

        def replace_with_symbolic_ref(args, **kwargs):
            if args == ["rev-parse", "--verify", ref]:
                git(self.work, "symbolic-ref", ref, "refs/heads/client-main")
                replaced.append(ref)
            return original_run(args, **kwargs)

        with mock.patch.object(wti, "_run_git", side_effect=replace_with_symbolic_ref):
            self.pool.release(self.handle)
        self.assertEqual(replaced, [ref])
        self.assertEqual(git(self.work, "for-each-ref", "--format=%(refname) %(objectname)"),
                         f"refs/heads/client-main {self.before}")

    def test_external_explicit_outputs_are_retained_on_disk_not_committed(self):
        self.change()
        self.change(RESPONSE, "Unpromoted mailbox bytes.\n")
        receipt = wti.preserve_terminal_evidence(self.authority)
        self.assertEqual(receipt.explicit_output_paths, (RESPONSE,))
        self.assertTrue(receipt.worktree_retained_required)
        self.assertEqual(receipt.preserved_paths, ("code.txt",))
        self.assertNotIn(RESPONSE, git(self.work, "ls-tree", "-r", "--name-only", receipt.evidence_commit))
        self.assertEqual((Path(receipt.worktree_location) / RESPONSE).read_text(), "Unpromoted mailbox bytes.\n")

    def test_promoted_external_outputs_are_not_reported_as_retained(self):
        self.change(RESPONSE, "Already promoted mailbox bytes.\n")
        target = self.squad / RESPONSE
        target.parent.mkdir(parents=True)
        target.write_text("Already promoted mailbox bytes.\n")
        receipt = wti.preserve_terminal_evidence(self.authority)
        self.assertEqual(receipt.explicit_output_paths, ())
        self.assertFalse(receipt.worktree_retained_required)
        self.assertEqual(receipt.evidence_commit, "")

    def test_cancel_preservation_uses_bound_work_branch(self):
        self.change()
        self.commit("code.txt")
        self.change(RESPONSE)
        result = bpt._terminal_evidence({"authority": self.authority})
        self.assertIn(result["status"], {"preserved", "preserved_existing"})
        self.assertEqual(git(self.work, "rev-parse", result["evidence_ref"]), result["evidence_commit"])
        self.assertEqual(git(self.work, "rev-parse", "client-main"), self.before)
        self.assertNotIn(RESPONSE, git(self.work, "ls-tree", "-r", "--name-only", result["evidence_commit"]))
        self.assertTrue(result["worktree_retained_required"])
        self.assertTrue((self.handle.worktree_root / RESPONSE).is_file())

    def test_cancel_shell_preserves_external_work_using_sealed_context(self):
        import test_cancel_preservation_base_branch as cancel_tests
        self.change()
        self.commit("code.txt")
        board = self.squad / "_state/board-dispatch"
        board.mkdir(parents=True)
        reconciler = self.squad / "bin/registry-reconciler.sh"
        reconciler.parent.mkdir()
        reconciler.write_text("#!/bin/sh\nexit 0\n")
        reconciler.chmod(0o755)
        fixture = cancel_tests.CancelPreservationBaseBranchTests()
        fixture.vault = self.squad
        # The helper owns only descriptor formatting; bind its IDs to this test.
        with mock.patch.object(cancel_tests, "TASK_ID", TASK), mock.patch.object(cancel_tests, "ATTEMPT_ID", ATTEMPT):
            fixture.authority = self.authority
            live = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
            try:
                try:
                    paths = fixture._write_descriptor(live.pid)
                except PermissionError as exc:
                    if exc.filename != "/bin/ps":
                        raise
                    self.skipTest(f"host process identity probe unavailable: {exc}")
                completed = subprocess.run(
                    ["bash", str(ROOT / "bin/vs-cancel-spawn.sh"), str(paths["log"])],
                    env={**os.environ, "VAULT_ROOT": str(self.squad)},
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                receipt = json.loads(paths["receipt"].read_text())
            finally:
                if live.poll() is None:
                    live.kill()
                live.wait()
        evidence = receipt["evidence_preservation"]
        self.assertEqual(receipt["terminal_outcome"], "cancelled")
        self.assertIn(evidence["status"], {"preserved", "preserved_existing"})
        self.assertEqual(git(self.work, "rev-parse", evidence["evidence_ref"]), evidence["evidence_commit"])
        self.assertEqual(git(self.work, "rev-parse", "client-main"), self.before)

    def test_missing_bound_branch_refuses_instead_of_using_environment(self):
        self.authority["work_base_branch"] = "missing"
        result = bpt._terminal_evidence({"authority": self.authority})
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["worktree_retained_required"])
        self.assertIn("refs/heads/missing", result["reason"])

    def test_incomplete_external_binding_cannot_fall_back_to_squad(self):
        self.authority.pop("work_base_branch")
        with self.assertRaisesRegex(wti.WorktreeIsolationError, "together"):
            wti.dispatch_work_repository(self.authority)

    def test_response_promotes_to_squad_mailbox_and_stays_out_of_work_commit(self):
        self.authority["lane"] = "codex"
        self.change(RESPONSE, (
            f"---\nid: {TASK}-response\nin_response_to: {TASK}\n"
            "from: gpt-codex\nto: chrono\ntype: RESULT\nstatus: complete\n"
            f"return_artifact: {RESPONSE}\n---\n\nExternal work completed.\n"
        ))
        self.change()
        prepared = dcb.prepare_worktree_outputs(self.squad, self.handle.worktree_root, self.authority)
        wti.commit_worker_residue(self.handle, self.authority["write_paths"], exclude_paths=(RESPONSE,))
        integrated = self.integrate()
        receipt = dcb.publish_prepared_worktree_outputs(self.squad, prepared)
        self.assertTrue(receipt["artifact_published"])
        self.assertTrue(receipt["envelope_published"])
        self.assertIn("External work completed.", (self.squad / RESPONSE).read_text())
        self.assertFalse((self.work / RESPONSE).exists())
        self.assertNotIn(RESPONSE, git(self.work, "ls-tree", "-r", "--name-only", integrated.target_after))

    def test_absent_field_keeps_squad_fast_forward_behavior(self):
        pool = wti.WorktreePool(self.squad, self.squad / "legacy-pool", base_branch="squad-only")
        handle = pool.provision(TASK, "d-" + "2" * 32)
        (handle.worktree_root / "code.txt").write_text("squad worker\n")
        git(handle.worktree_root, "add", "code.txt")
        git(handle.worktree_root, "commit", "-q", "-m", "legacy worker")
        result = wti.integrate_worktree_commits(handle, ("code.txt",), target_branch="squad-only")
        self.assertEqual(git(self.squad, "rev-parse", "HEAD"), result.worker_head)
        self.assertEqual((self.squad / "code.txt").read_text(), "squad worker\n")


class ExternalLaunchRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="external-request-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.squad = init_repo(self.base / "squad", "squad-only")
        self.work = init_repo(self.base / "work", "client-main")
        self.task_root = self.base / "task"
        self.task_root.mkdir()
        self.request = self.base / "request.json"
        self.payload = {
            "task_id": TASK, "attempt_id": ATTEMPT, "generation": 1,
            "branch": "client-main", "task_root": str(self.task_root),
            "write_paths": [str(self.task_root)],
            "profile_bundle_sha256": hygiene.SETTLED_T1P1_BUNDLE_SHA256,
        }
        patch = mock.patch.object(hygiene, "__file__", str(self.squad / "scripts/python/launch_hygiene.py"))
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"SQUAD_BASE_BRANCH": "squad-only"})
        env.start()
        self.addCleanup(env.stop)

    def load(self, external=True, **kwargs):
        self.request.write_text(json.dumps(self.payload))
        pair = {"work_repo_root": self.work, "work_base_branch": "client-main"} if external else {}
        return hygiene._load_task_request(self.request, **{**pair, **kwargs})

    def test_accepts_external_request_bound_to_work_base(self):
        self.assertEqual(self.load(), self.payload)

    def test_refuses_request_branch_different_from_bound_work_base(self):
        self.payload["branch"] = "squad-only"
        with self.assertRaisesRegex(hygiene.HygieneError, "accepts branch client-main only"):
            self.load()

    def test_refuses_missing_base_in_bound_work_repo(self):
        with self.assertRaisesRegex(hygiene.HygieneError, "work_repo.*does not exist"):
            self.load(work_base_branch="missing")

    def test_keeps_own_squad_checkout_branch_check_for_external_request(self):
        git(self.squad, "checkout", "-q", "-b", "wrong-squad")
        with self.assertRaisesRegex(hygiene.HygieneError, "repository branch is not squad-only"):
            self.load()

    def test_absent_binding_accepts_squad_request_and_refuses_external_branch(self):
        with self.assertRaisesRegex(hygiene.HygieneError, "accepts branch squad-only only"):
            self.load(external=False)
        self.payload["branch"] = "squad-only"
        self.assertEqual(self.load(external=False), self.payload)

    def test_request_cannot_supply_its_own_external_pair(self):
        self.payload["work_repo_root"] = str(self.work)
        self.payload["work_base_branch"] = "client-main"
        with self.assertRaisesRegex(hygiene.HygieneError, "fields must be exactly"):
            self.load()

    def test_external_pair_keeps_generation_scope_and_profile_guards(self):
        for key, value, message in (
            ("generation", True, "generation"), ("task_id", "wrong", "task id"),
            ("attempt_id", "wrong", "attempt id"), ("write_paths", [], "write_paths"),
            ("profile_bundle_sha256", "a" * 64, "settled"),
        ):
            with self.subTest(key=key), mock.patch.dict(self.payload, {key: value}):
                with self.assertRaisesRegex(hygiene.HygieneError, message):
                    self.load()

    def test_external_pair_keeps_no_follow_task_directory_guard(self):
        alias = self.base / "alias"
        alias.symlink_to(self.task_root, target_is_directory=True)
        self.payload["task_root"] = str(alias)
        with self.assertRaisesRegex(hygiene.HygieneError, "no-follow"):
            self.load()


class GeneratingWrapperWorkRepoTests(unittest.TestCase):
    def wrapper(self, work_repo):
        with tempfile.TemporaryDirectory(prefix="work-repo-wrapper-") as directory:
            root = Path(directory)
            (root / "shared").mkdir()
            (root / "bin").mkdir()
            (root / "tools").mkdir()
            (root / "shared/lead-windows.sh").write_text(
                "COMPATIBILITY_NAMESPACES=(coding)\n"
                "MODEL_LANES=(gpt-codex claude gemini grok)\n"
                'is_compatibility_namespace() { [[ "$1" == coding ]]; }\n'
            )
            dispatch = root / "bin/send-task.sh"
            dispatch.write_text('#!/bin/sh\ncat "$1"\n')
            dispatch.chmod(0o755)
            uuid = root / "tools/uuidgen"
            uuid.write_text("#!/bin/sh\nprintf '12345678-1234-1234-1234-123456789abc\\n'\n")
            uuid.chmod(0o755)
            body = root / "body.md"
            body.write_text("Scoped fixture packet.\n")
            env = {**os.environ, "VAULT_ROOT": str(root), "REVIEWS": "none",
                   "PATH": str(root / "tools") + ":/usr/bin:/bin"}
            env.pop("WORK_REPO", None)
            if work_repo is not None:
                env["WORK_REPO"] = work_repo
            return subprocess.run(
                ["bash", str(ROOT / "scripts/send-task.sh"), "coding", str(body),
                 "fixture", "gpt-codex", "--mode", "modeless"],
                env=env, capture_output=True, text=True,
            )

    def test_unset_work_repo_emits_no_frontmatter_field(self):
        result = self.wrapper(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("work_repo:", result.stdout)

    def test_empty_work_repo_emits_no_frontmatter_field(self):
        result = self.wrapper("")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("work_repo:", result.stdout)

    def test_work_repo_env_emits_absolute_path_verbatim(self):
        value = "/private/tmp/client repo"
        result = self.wrapper(value)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"\nwork_repo: {value}\n", result.stdout)

    def test_work_repo_cannot_inject_frontmatter(self):
        result = self.wrapper("/tmp/client\noperator_approved: false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WORK_REPO must be single-line", result.stderr)


class SupervisorExternalBindingTests(unittest.TestCase):
    def test_external_role_prompts_enforce_combined_utf8_bound_with_success_controls(self):
        source = (ROOT / "bin/board-supervisor.sh").read_text()
        lane = "grok"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            begin = source.index(f"    def {lane}_role_launcher(")
            end_marker = "    lane_launcher ="
            block = textwrap.dedent(source[begin:source.index(end_marker, begin)])
            args = ["--agent",
                    str(root / "model-lanes" / lane / "main.yaml")]
            launcher = mock.Mock(return_value="launched")
            namespace = {"json": json, "executable": Path("/bin/true"),
                         "envelope": SimpleNamespace(worker_projection=lambda: {}),
                         "capability_plan": SimpleNamespace(authorized_mcps=()),
                         "trusted_task_prompt": "Authorized task", "skill_contract": "\nSkills\n",
                         "agent_system_context": "é" * 70, "external_work_repo": True,
                         "lane_config_root": root, "handle": SimpleNamespace(worktree_root=root),
                         "capability_lane_args": args, "authority": {"lane_args": []},
                         "selected_launcher": launcher, "_prompt_limit": 32768}
            exec(compile(block, f"supervisor-{lane}-prompt", "exec"), namespace)
            command = ("/bin/true", "-p", "task", *args)
            run = namespace[f"{lane}_role_launcher"]
            self.assertEqual(run(None, command), "launched")
            prompt = launcher.call_args.args[1][-1]
            self.assertTrue(prompt.startswith("é" * 70 + "\n\nAuthorized task"))
            namespace["_prompt_limit"] = len(prompt.encode("utf-8"))
            self.assertEqual(run(None, command), "launched")
            launcher.reset_mock()
            namespace["_prompt_limit"] -= 1
            with self.assertRaisesRegex(ValueError, f"{lane.title()} external role/task prompt exceeds"):
                run(None, command)
            launcher.assert_not_called()
            # The existing squad path still binds the role through its file.
            namespace["external_work_repo"] = False
            restored = mock.Mock(side_effect=lambda path, suffix, launch: launch())
            namespace["run_with_restored_prompt"] = restored
            namespace["_prompt_limit"] = 1
            self.assertEqual(run(None, ("/bin/true", "-p", "task", *args)), "launched")
            restored.assert_called_once()
            self.assertFalse(launcher.call_args.args[1][-1].startswith("é"))

    def test_external_read_paths_resolve_existing_inputs_and_refuse_missing_ones(self):
        source = (ROOT / "bin/board-supervisor.sh").read_text()
        start = source.index("def canonical_logical_path(")
        end = source.index('\nwrite_paths = authority["write_paths"]', start)
        worker_start = source.index("    def worker_scope_path(")
        worker_end = source.index("    expected_result_path =", worker_start)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            squad, work, attempt = (root / name for name in ("squad", "work", "attempt"))
            for path in (squad / "docs/skill.md", squad / "_state/generated.md",
                         squad / "shared.md", work / "shared.md", work / "code.txt"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n")
            role = squad / "role.md"
            role.write_text("role\n")
            authority = {"canonical_role_path": str(role), "lane_overlay_path": str(role),
                         "write_paths": ["_state/generated.md"], "read_scope": []}
            namespace = {"Path": Path, "os": os, "repo_path": squad, "work_repo_path": work,
                         "external_work_repo": True, "authority": authority, "task_id": TASK,
                         "handle": SimpleNamespace(worktree_root=attempt),
                         "deny": mock.Mock(side_effect=ValueError)}
            exec(compile(source[start:end], "supervisor-read-routing", "exec"), namespace)
            route = namespace["is_config_read"]
            cases = {"docs/skill.md": True, str(squad / "docs/skill.md"): True,
                     "role.md": True, "shared.md": False, "code.txt": False,
                     "_state/generated.md": False, f"departments/coding/inbox/{TASK}.md": True}
            for value, config in cases.items():
                with self.subTest(value=value):
                    self.assertEqual(route(value), config)
            for value in ("missing.md", str(work / "missing.md"), str(squad / "missing.md")):
                with self.subTest(missing=value):
                    with self.assertRaises(ValueError):
                        route(value)
                    self.assertIn("does not exist", namespace["deny"].call_args.args[0])
            authority["read_scope"] = ["docs/skill.md", str(squad / "docs/skill.md"),
                                       "code.txt", "_state/generated.md"]
            exec(compile(textwrap.dedent(source[worker_start:worker_end]),
                         "supervisor-worker-read-scopes", "exec"), namespace)
            self.assertEqual(namespace["worker_read_scope"],
                             (str(squad / "docs/skill.md"), str(squad / "docs/skill.md"),
                              str(attempt / "code.txt"), str(attempt / "_state/generated.md")))
            namespace["external_work_repo"] = False
            self.assertFalse(route("missing.md"))
            self.assertFalse(route("role.md"))

    def test_supervisor_runs_real_external_worker_cwd_with_real_request_validator(self):
        import test_trusted_launch as trusted_tests
        with tempfile.TemporaryDirectory(prefix="external-supervisor-") as directory:
            root = Path(directory).resolve()
            executable = root / "inert-worker"
            executable.write_text("#!/bin/sh\npwd\n")
            executable.chmod(0o700)
            stub_root = trusted_tests.BlockedReceiptStaysBlockedTests._install_hermetic_launch_stubs(root, executable)
            # This fixture stubs host Seatbelt only. _load_task_request is the
            # shipped validator; the worker is a real, local, inert subprocess.
            payload = trusted_tests.TrustedLaunchTests()._fixture_payload(root, task_id=TASK, attempt_id=ATTEMPT)
            authority = payload["authority"]
            squad = Path(authority["repo_root"])
            for field in ("canonical_role_path", "lane_overlay_path"):
                original = Path(authority[field])
                destination = squad / original.name
                destination.write_bytes(original.read_bytes())
                authority[field] = str(destination)
            work = init_repo(root / "work", "client-main")
            before = git(work, "rev-parse", "HEAD")
            authority.update(work_repo_root=str(work), work_base_branch="client-main",
                             executable=str(executable),
                             executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest())
            context = root / "context.json"
            context.write_text(json.dumps(payload))
            completed = subprocess.run(
                ["bash", str(ROOT / "bin/board-supervisor.sh"), "trusted-launch", str(context)],
                capture_output=True, text=True, timeout=30,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C",
                     "VAULT_ROOT": str(ROOT), "TRUSTED_LAUNCH_TEST_MODE": "1",
                     "SQUAD_BASE_BRANCH": trusted_tests.BASE_BRANCH,
                     "PYTHONPATH": os.pathsep.join((str(stub_root), str(ROOT / "scripts/python"))),
                     "TRUSTED_LAUNCH_STUB_CLI": str(executable)},
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            receipt = json.loads(completed.stdout)
            self.assertEqual(receipt["status"], "launched")
            self.assertEqual(receipt["worktree_root"], str(Path(authority["pool_root"]) / ATTEMPT))
            expected_stdout = (receipt["worktree_root"] + "\n").encode()
            self.assertEqual(receipt["cli_stdout_sha256"], hashlib.sha256(expected_stdout).hexdigest())
            self.assertEqual(git(work, "rev-parse", "HEAD"), before)

    def test_supervisor_passes_authenticated_pair_to_request_validator(self):
        source = (ROOT / "bin/board-supervisor.sh").read_text()
        start = source.index("request_payload = {")
        end = source.index("\naudited_scopes =", start)
        with tempfile.TemporaryDirectory(prefix="external-supervisor-request-") as directory:
            root = Path(directory).resolve()
            handle = type("Handle", (), {"worktree_root": root, "base_branch": "client-main"})()
            validator = mock.Mock(return_value={})
            namespace = {"task_id": TASK, "attempt_id": ATTEMPT, "generation": 1,
                         "handle": handle, "work_repo_path": root, "work_base_branch": "client-main",
                         "external_work_repo": True, "context": {"profile_bundle_sha256": "a" * 64},
                         "json": json, "_load_task_request": validator, "HygieneError": hygiene.HygieneError}
            exec(compile(source[start:end], "supervisor-request", "exec"), namespace)
            validator.assert_called_once_with(root / ".trusted-launch-request.json",
                                              work_repo_root=root, work_base_branch="client-main")
            self.assertEqual(namespace["request_payload"]["branch"], "client-main")

    def test_recovery_preserves_without_landing_external_task_branch(self):
        import test_trusted_launch as trusted_tests
        fixture = ExternalIntegrationTests()
        fixture.setUp()
        try:
            fixture.change()
            fixture.commit("code.txt")
            namespace = {"_work_recovery_window": [True], "launch_mode": "trusted",
                         "execution_kind": "lane", "handle": fixture.handle,
                         "authority": fixture.authority, "authorized_delete_paths": (),
                         "wti": wti, "asdict": asdict, "write_board_note": lambda note: None}
            exec(trusted_tests._supervisor_region("committed-work-recovery"), namespace)
            namespace["_work_recovery_window"][0] = True
            receipt = namespace["recover_committed_work_for_block"]()
            self.assertEqual(receipt["status"], "preserved")
            self.assertEqual(git(fixture.work, "rev-parse", "client-main"), fixture.before)
            self.assertEqual(git(fixture.work, "branch", "--list", f"board/{TASK}"), "")
            ref = receipt["evidence_preservation"]["evidence_ref"]
            self.assertEqual(git(fixture.work, "show", f"{ref}:code.txt"), "worker")
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
