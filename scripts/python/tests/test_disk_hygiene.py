"""Real cleanup stays in disposable Git/tmp fixtures; never sweep host scratch."""

import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[3]
TASK = "TASK-disk-fixture"
ATTEMPT = "d-00000000000000000000000000000001"
# Execute the real helper, replacing only the filesystem's free-byte reading.
# Keeping this seam in the test avoids a production override for the disk guard.
DISK_DRIVER = r'''
import os, pathlib, shutil, sys, types
source = pathlib.Path(sys.argv[1]).read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
sys.argv = [sys.argv[1], *sys.argv[2:]]
def fixture_usage(root):
    free = int(os.environ["FIXTURE_FREE_BYTES"])
    if os.environ.get("FIXTURE_SWEPT_PATH") and not pathlib.Path(os.environ["FIXTURE_SWEPT_PATH"]).exists():
        free = int(os.environ["FIXTURE_FREE_AFTER"])
    return types.SimpleNamespace(free=free)
shutil.disk_usage = fixture_usage
exec(compile(source, sys.argv[0], "exec"))
'''


class DiskHygieneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="disk-hygiene-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        from test_disk_hygiene_local_config import provision_config
        provision_config(self.root)
        self.state = self.root / "_state"
        self.dispatch = self.state / "board-dispatch"
        self.board = self.state / "board-worktrees"
        self.tmp = self.root / "tmp"
        self.scratch = self.tmp / "vs"
        for path in (self.dispatch, self.board, self.scratch, self.root / "bin", self.root / "scripts"):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "scripts/python").symlink_to(REPO / "scripts/python")
        for name in ("disk-hygiene.sh", "prune-board-worktrees.sh"):
            (self.root / "bin" / name).symlink_to(REPO / "bin" / name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "seed.txt").write_text("tracked seed\n")
        (self.root / ".gitignore").write_text("_state/\ntmp/\nbin/\nscripts/\n")
        self.git("add", "seed.txt", ".gitignore")
        self.git("commit", "-qm", "fixture")
        self.registry = {}
        self.save_registry()
        self.env = dict(os.environ, VAULT_ROOT=str(self.root), SQUAD_TEST_ISOLATION="1",
                        BOARD_SCRATCH_ROOT=str(self.scratch), BOARD_TMP_ROOT=str(self.tmp),
                        PYTHONDONTWRITEBYTECODE="1", CHRONO_AUTOCAPTURE_MAX_INFLIGHT="0")
        # The lane sandbox forbids host ps. Exercise that boundary with a fake
        # process census; live-reference and failure controls are in sweep tests.
        fakebin = self.root / "fixturebin"
        fakebin.mkdir()
        (fakebin / "ps").write_text("#!/bin/sh\necho fixture-idle-process\n")
        (fakebin / "ps").chmod(0o755)
        self.env["PATH"] = str(fakebin) + os.pathsep + self.env["PATH"]

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null",
                               "-c", "commit.gpgsign=false", *args], check=True,
                              capture_output=True, text=True)

    def save_registry(self):
        (self.state / "active-tasks.json").write_text(json.dumps(self.registry))

    def attempt(self, number=1, task=TASK, status="complete", promoted=True, worktree=True, **extra):
        attempt = f"d-{number:032x}"
        artifact = f"departments/coding/outbox/{task}-response.md"
        self.registry[task] = dict(status=status, return_artifact=artifact, delivery_attempt_id=attempt)
        self.save_registry()
        path = self.dispatch / f"{task}.{attempt}.dispatch.json"
        descriptor = dict(schema="board-dispatch-process/v1", task_id=task, attempt_id=attempt,
                          generation=1, context_path=str(path).replace(".dispatch.json", ".context.json"),
                          log_path=str(path).replace(".dispatch.json", ".log"),
                          receipt_path=str(path).replace(".dispatch.json", ".receipt.json"), **extra)
        path.write_text(json.dumps(descriptor))
        if promoted:
            out = self.root / artifact
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("promoted fixture result\n")
        wt = self.board / attempt
        if worktree:
            self.git("worktree", "add", "-q", "--detach", str(wt))
        scratch = self.scratch / attempt
        scratch.mkdir()
        (scratch / "sentinel").write_text("scratch\n")
        self.age(scratch)
        return wt, scratch, descriptor

    def run_prune(self, *args, env=None):
        return subprocess.run(["bash", str(REPO / "bin/prune-board-worktrees.sh"), *args],
                              cwd=self.root, env=env or self.env,
                              capture_output=True, text=True, timeout=40)

    def disk(self, *args, free=20_000_000_000, env=None):
        return subprocess.run([sys.executable, "-B", "-c", DISK_DRIVER,
                               str(REPO / "bin/disk-hygiene.sh"), *args],
                              cwd=self.root, env=dict(env or self.env, FIXTURE_FREE_BYTES=str(free)),
                              capture_output=True, text=True, timeout=50)

    def old_scratch(self, name):
        path = self.tmp / name
        path.mkdir()
        (path / "payload").write_text("fixture bytes\n")
        self.age(path)
        return path

    def age(self, path):
        old = time.time() - 25 * 3600
        for item in [*path.rglob("*"), path]:
            os.utime(item, (old, old), follow_symlinks=False)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rescue_and_live_guards_with_removal_control(self):
        rescue, rescue_tmp, descriptor = self.attempt(task="TASK-rescue", promoted=False)
        (rescue / "only-result.txt").write_text("irreplaceable\n")
        marked = self.old_scratch("rescue-clone")
        (marked / ".vibesquad-scratch").write_text(rescue.name)
        self.age(marked)
        referenced = self.old_scratch("vs-review-rescue")
        Path(descriptor["context_path"]).write_text(json.dumps({"read_paths": [str(referenced)]}))
        live, live_tmp, _ = self.attempt(2, "TASK-live", status="in-flight")
        removable, dead_tmp, _ = self.attempt(3, "TASK-removable")
        result = self.run_prune("--apply", "--sweep-tmp")
        self.assert_ok(result)
        self.assertIn("NEEDS-RESCUE=1", result.stdout)
        self.assertFalse(rescue.exists())
        self.assertFalse(rescue_tmp.exists())
        self.assertFalse(marked.exists() or referenced.exists())
        self.assertTrue(live.is_dir())
        self.assertTrue(live_tmp.is_dir())
        self.assertFalse(removable.exists())
        self.assertFalse(dead_tmp.exists())
        saved = self.state / "rescued-worker-artifacts/TASK-rescue" / rescue.name / "only-result.txt"
        self.assertEqual(saved.read_text(), "irreplaceable\n")

    def test_terminal_registry_cannot_delete_live_pid_or_process_group(self):
        live, scratch, _ = self.attempt(pid=os.getpid())
        group, group_tmp, _ = self.attempt(2, "TASK-group", pgid=os.getpgrp())
        dead, dead_tmp, _ = self.attempt(3, "TASK-dead")
        self.assert_ok(self.run_prune("--apply"))
        self.assertTrue(live.exists() and scratch.exists())
        self.assertTrue(group.exists() and group_tmp.exists())
        self.assertFalse(dead.exists() or dead_tmp.exists())

    def test_campaign_registry_larger_than_descriptor_json_cap_still_cleans(self):
        wt, scratch, _ = self.attempt()
        self.registry[TASK]["fixture_padding"] = "x" * (2 * 1024 * 1024)
        self.save_registry()
        self.assert_ok(self.run_prune("--apply"))
        self.assertFalse(wt.exists() or scratch.exists())

    def test_concurrent_pruner_retains_until_lock_is_released(self):
        wt, scratch, _ = self.attempt()
        with (self.state / "prune-board-worktrees.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_prune("--apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cleanup already running", result.stderr)
            self.assertTrue(wt.exists() and scratch.exists())
        self.assert_ok(self.run_prune("--apply"))
        self.assertFalse(wt.exists() or scratch.exists())

    def test_settlement_is_exactly_one_attempt_and_does_not_expire_other_logs(self):
        first, first_tmp, _ = self.attempt()
        current, current_tmp, _ = self.attempt(2)
        other, other_tmp, descriptor = self.attempt(3, "TASK-other")
        transcript = Path(descriptor["log_path"])
        transcript.write_text("old transcript\n")
        old = time.time() - 40 * 86400
        os.utime(transcript, (old, old))
        result = self.disk("--task-id", TASK)
        self.assert_ok(result)
        self.assertFalse(current.exists() or current_tmp.exists())
        self.assertTrue(all(p.exists() for p in (first, first_tmp, other, other_tmp, transcript)))

    def test_unbound_task_does_not_widen_to_global_cleanup(self):
        wt, scratch, _ = self.attempt()
        result = self.disk("--task-id", "TASK-missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(wt.exists() and scratch.exists())
        self.assert_ok(self.disk("--task-id", TASK))
        self.assertFalse(wt.exists() or scratch.exists())

    def test_receipt_allows_terminal_review_attempt_but_wrong_identity_does_not(self):
        wt, scratch, descriptor = self.attempt(status="needs_review")
        receipt = Path(descriptor["receipt_path"])
        value = dict(task_id=TASK, attempt_id="d-wrong", generation=1, response_status="needs_review")
        receipt.write_text(json.dumps(value))
        self.assert_ok(self.run_prune("--apply"))
        self.assertTrue(wt.exists() and scratch.exists())
        value["attempt_id"] = ATTEMPT
        receipt.write_text(json.dumps(value))
        self.assert_ok(self.run_prune("--apply"))
        self.assertFalse(wt.exists() or scratch.exists())

    def test_open_scratch_retained_then_removed_after_close(self):
        _, scratch, _ = self.attempt(worktree=False)
        with (scratch / "sentinel").open() as held:
            self.assertTrue(held.read())
            result = self.run_prune("--apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lsof", result.stdout)
            self.assertTrue(scratch.exists())
        self.assert_ok(self.run_prune("--apply"))
        self.assertFalse(scratch.exists())

    def test_shell_wrapper_uses_selected_root_from_unrelated_directory(self):
        _, scratch, _ = self.attempt(worktree=False)
        config = self.state / 'disk-hygiene.local.json'
        config.unlink()  # own fixture policy only
        command = ['bash', str(REPO / 'bin/disk-hygiene.sh'), '--periodic', '--now']
        result = subprocess.run(command, cwd=self.root.parent, env=self.env,
                                capture_output=True, text=True, timeout=50)
        self.assert_ok(result)
        self.assertTrue(scratch.exists())
        self.assertIn('local protection policy unavailable', result.stdout)
        from test_disk_hygiene_local_config import provision_config
        provision_config(self.root)
        result = subprocess.run(command, cwd=self.root.parent, env=self.env,
                                capture_output=True, text=True, timeout=50)
        self.assert_ok(result)
        self.assertFalse(scratch.exists())

    def test_attempt_scratch_requires_valid_local_policy_with_removal_control(self):
        from test_disk_hygiene_local_config import provision_config

        _, scratch, _ = self.attempt(worktree=False)
        config = self.state / "disk-hygiene.local.json"
        config.unlink()  # This test's own neutral policy fixture.
        for value in (None, "{broken", "{}", '{"tmp": {}}'):
            with self.subTest(value=value):
                if value is not None:
                    config.write_text(value)
                result = self.run_prune("--apply")
                self.assert_ok(result)
                self.assertTrue(scratch.exists())
                self.assertIn("RETAINED (scratch policy)", result.stdout)
                self.assertIn("local protection policy unavailable", result.stdout)
        provision_config(self.root)
        self.assert_ok(self.run_prune("--apply"))
        self.assertFalse(scratch.exists())

    def test_attempt_scratch_keeps_configured_protections_with_removal_control(self):
        from test_disk_hygiene_local_config import FIXTURE_CONFIG

        _, profile, _ = self.attempt(worktree=False)
        (profile / FIXTURE_CONFIG["tmp"]["protected_profile_substring"]).mkdir()
        self.age(profile)
        _, pinned, _ = self.attempt(2, "TASK-pinned", worktree=False)
        name = FIXTURE_CONFIG["tmp"]["pinned_patterns"][0].replace("*", "fixture")
        (pinned / name).mkdir()
        self.age(pinned)
        _, control, _ = self.attempt(3, "TASK-control", worktree=False)
        result = self.run_prune("--apply")
        self.assert_ok(result)
        self.assertTrue(profile.exists() and pinned.exists())
        self.assertEqual(result.stdout.count("RETAINED (scratch policy)"), 2)
        self.assertFalse(control.exists())

    def test_readonly_scratch_removes_dirs_without_chmod_following_links(self):
        _, scratch, _ = self.attempt(worktree=False)
        outside = self.tmp / "operator-pad"
        outside.mkdir()
        sentinel = outside / "keep"
        sentinel.write_text("keep\n")
        sentinel.chmod(0o444)
        before = stat.S_IMODE(outside.stat().st_mode)
        nested = scratch / "cache"
        nested.mkdir()
        (nested / "hardlink").hardlink_to(sentinel)
        (nested / "link").symlink_to(outside)
        self.age(scratch)
        nested.chmod(0o555)
        scratch.chmod(0o555)
        self.addCleanup(lambda: scratch.chmod(0o755) if scratch.exists() else None)
        self.addCleanup(lambda: nested.chmod(0o755) if nested.exists() else None)
        self.assert_ok(self.run_prune("--apply"))
        self.assertFalse(scratch.exists())
        self.assertEqual(sentinel.read_text(), "keep\n")
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), before)

    def test_adhoc_age_protected_paths_and_recent_descendant_controls(self):
        old = self.old_scratch("vs-review-old")
        old.chmod(0o555)
        self.addCleanup(lambda: old.chmod(0o755) if old.exists() else None)
        protected = [self.old_scratch(name) for name in (
            "claude-501", "vs-worker-heartbeat.status", "unclassified-review-clone", "vs-verify-recent")]
        (protected[-1] / "fresh").write_text("new\n")
        old_time = time.time() - 25 * 3600
        os.utime(protected[-1], (old_time, old_time))
        status = self.tmp / "vs-codex.status"
        status.write_text("running\n")
        symlink = self.tmp / "vs-worker-link"
        symlink.symlink_to(protected[0], target_is_directory=True)
        self.assert_ok(self.run_prune("--apply", "--sweep-tmp"))
        self.assertFalse(old.exists())
        self.assertTrue(all(p.exists() for p in [*protected, status, symlink]))

    def test_adhoc_live_reference_and_ownership_marker_controls(self):
        _, _, descriptor = self.attempt(status="in-flight", worktree=False)
        referenced = self.old_scratch("vs-review-referenced")
        marked = self.old_scratch("legacy-frame-dump")
        (marked / ".vibesquad-scratch").write_text(ATTEMPT)
        self.age(marked)
        Path(descriptor["context_path"]).write_text(json.dumps({"read_paths": [str(referenced)]}))
        control = self.old_scratch("vs-review-control")
        self.assert_ok(self.run_prune("--apply", "--sweep-tmp"))
        self.assertTrue(referenced.exists() and marked.exists())
        self.assertFalse(control.exists())
        self.registry[TASK]["status"] = "complete"
        self.save_registry()
        self.assert_ok(self.run_prune("--apply", "--sweep-tmp"))
        self.assertFalse(referenced.exists() or marked.exists())

    def test_empty_or_symlinked_ownership_marker_retains_with_prefix_control(self):
        empty = self.old_scratch("vs-worker-empty-marker")
        (empty / ".vibesquad-scratch").write_text("")
        self.age(empty)
        linked = self.old_scratch("vs-worker-linked-marker")
        (linked / ".vibesquad-scratch").symlink_to(empty / ".vibesquad-scratch")
        self.age(linked)
        control = self.old_scratch("vs-worker-control")
        self.assert_ok(self.run_prune("--apply", "--sweep-tmp"))
        self.assertTrue(empty.exists() and linked.exists())
        self.assertFalse(control.exists())

    def test_adhoc_open_file_and_failed_lsof_retain_with_idle_control(self):
        held_path = self.old_scratch("vs-frames-open")
        idle = self.old_scratch("vs-frames-idle")
        with (held_path / "payload").open():
            result = self.run_prune("--apply", "--sweep-tmp")
            self.assert_ok(result)
            self.assertTrue(held_path.exists())
            self.assertFalse(idle.exists())
        fakebin = self.root / "fakebin"
        fakebin.mkdir()
        lsof = fakebin / "lsof"
        lsof.write_text("#!/bin/sh\necho 'fixture census failed' >&2\nexit 1\n")
        lsof.chmod(0o755)
        result = self.run_prune("--apply", "--sweep-tmp",
                                env=dict(self.env, PATH=f"{fakebin}:{self.env['PATH']}"))
        self.assert_ok(result)
        self.assertTrue(held_path.exists())
        self.assert_ok(self.run_prune("--apply", "--sweep-tmp"))
        self.assertFalse(held_path.exists())

    def test_warning_boundary_and_five_measured_space_users(self):
        names = [f"vs-worker-size-{size}" for size in range(1, 7)]
        for size, name in enumerate(names, 1):
            path = self.tmp / name
            path.mkdir()
            (path / "data").write_bytes(b"x" * size * 128 * 1024)
        low = self.disk("--check", free=15 * 2**30 - 1)
        self.assertEqual(low.returncode, 3, low.stdout + low.stderr)
        self.assertIn("[vibesquad] DISK WARNING", low.stdout)
        self.assertIn("top 5 temp allocations", low.stdout)
        self.assertIn("APFS container free", low.stdout)
        self.assertIn("Preboot/staged updates", low.stdout)
        self.assertIn("test Postgres", low.stdout)
        self.assertIn("unique", low.stdout)
        self.assertNotIn(names[0], low.stdout)
        positions = [low.stdout.index(name) for name in reversed(names[1:])]
        self.assertEqual(positions, sorted(positions))
        for free in (15 * 2**30, 15 * 2**30 + 1, 20_000_000_000):
            result = self.disk("--check", free=free)
            self.assert_ok(result)
            self.assertNotIn("WARNING", result.stdout)

    def test_periodic_notice_and_thirty_minute_cadence(self):
        notifier = self.root / "bin/outbox-watcher.sh"
        notifier.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$VAULT_ROOT/notice.log"\n')
        first = self.old_scratch("vs-review-first")
        self.assert_ok(self.disk("--periodic", free=14_000_000_000))
        self.assertFalse(first.exists())
        notice = (self.root / "notice.log").read_text()
        self.assertIn("--notify-once", notice)
        self.assertIn("[vibesquad] DISK WARNING", notice)
        second = self.old_scratch("vs-review-second")
        self.assert_ok(self.disk("--periodic"))
        self.assertTrue(second.exists())
        (self.state / "disk-hygiene.json").write_text(json.dumps({"last_success": time.time() - 1801}))
        self.assert_ok(self.disk("--periodic"))
        self.assertFalse(second.exists())
        immediate = self.old_scratch("vs-review-immediate")
        self.assert_ok(self.disk("--periodic", "--now"))
        self.assertFalse(immediate.exists())
        self.assertNotEqual(self.disk("--check", "--now").returncode, 0)

    def test_periodic_default_sweeps_approved_pools(self):
        # Operator 2026-09-30: no environment flag is needed for idle clones.
        notifier = self.root / "bin/outbox-watcher.sh"
        notifier.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$VAULT_ROOT/notice.log"\n')
        clone_pool = self.root / "clones"
        clone = clone_pool / "code_sign_clone.old"
        clone.mkdir(parents=True)
        os.utime(clone, (time.time() - 7200, time.time() - 7200))
        env = {k: v for k, v in self.env.items() if k != "DISK_HYGIENE_AUTODELETE"}
        env["BOARD_CHROME_CLONE_ROOT"] = str(clone_pool)
        self.disk("--periodic", free=14_000_000_000, env=env)
        self.assertFalse(clone.exists())
        self.assertIn("[vibesquad] DISK WARNING", (self.root / "notice.log").read_text())

    def test_periodic_checks_free_space_after_sweeping(self):
        notifier = self.root / "bin/outbox-watcher.sh"
        notifier.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$VAULT_ROOT/notice.log"\n')
        for initial, final, warn in ((10 * 2**30, 20 * 2**30, False),
                                     (20 * 2**30, 10 * 2**30, True)):
            with self.subTest(warn=warn):
                victim = self.old_scratch(f"example-scratch-{initial}")
                (self.state / "disk-hygiene.json").write_text('{"last_success": 0}')
                result = self.disk("--periodic", free=initial,
                                   env=dict(self.env, FIXTURE_SWEPT_PATH=str(victim),
                                            FIXTURE_FREE_AFTER=str(final)))
                self.assert_ok(result)
                self.assertFalse(victim.exists())
                self.assertEqual("DISK WARNING" in result.stdout, warn)
                self.assertEqual((self.root / "notice.log").exists(), warn)

    def test_periodic_new_pools_and_wallet_marker_cannot_bypass_guards(self):
        clone_pool = self.root / "chrome-clones"
        clone_pool.mkdir()
        clone = clone_pool / "code_sign_clone.old"
        clone.mkdir()
        (clone / "payload").write_text("old clone")
        self.age(clone)
        rescued = self.state / "rescued-worker-artifacts/TASK-expired"
        rescued.mkdir(parents=True)
        (rescued / "result").write_text("old artifact")
        self.age(rescued)
        self.registry["TASK-expired"] = {"status": "complete"}
        self.save_registry()
        _, _, _ = self.attempt(worktree=False)
        wallet = self.old_scratch("example-wallet")
        (wallet / "wallet.key").write_text("fixture only")
        (wallet / ".vibesquad-scratch").write_text(ATTEMPT)
        self.age(wallet)
        safe = self.old_scratch("example-expired-gocache")
        result = self.disk("--periodic", env=dict(self.env, BOARD_CHROME_CLONE_ROOT=str(clone_pool)))
        self.assert_ok(result)
        self.assertTrue(wallet.exists())
        self.assertFalse(any(p.exists() for p in (clone, safe)))
        self.assertTrue(rescued.exists())
        self.assertIn("rescued: alert-only", result.stdout)
        self.assertIn("chrome-clones: swept 1", result.stdout)
        self.assertIn("protected wallet/profile/tool", result.stdout)

    def test_missing_notifier_falls_back_to_warning_line(self):
        result = self.disk("--periodic", free=10 * 2**30)
        self.assert_ok(result)
        self.assertIn("DISK WARNING", result.stdout)
        self.assertIn("pane notification unavailable", result.stdout)

    def test_check_does_not_expire_any_new_pool(self):
        path = self.old_scratch("example-read-only-check")
        self.assert_ok(self.disk("--check"))
        self.assertTrue(path.exists())
        self.assertFalse((self.state / "disk-hygiene.lock").exists())

    def test_doctor_reports_warning_healthy_and_unknown_without_cleanup(self):
        source = (REPO / "bin/doctor.sh").read_text()
        section = source.split("# Absolute campaign headroom", 1)[1].split("# A df that", 1)[0]
        helper = self.root / "bin/disk-hygiene.sh"
        helper.unlink()  # fixture symlink
        for code, expected in ((3, "WARN"), (0, "OK"), (2, "UNKNOWN")):
            with self.subTest(code=code):
                helper.write_text(f'#!/bin/sh\n[ "$1" = "--check" ] || exit 99\necho fixture\nexit {code}\n')
                helper.chmod(0o755)
                script = ('note_ok() { echo OK; }; note_warn() { echo WARN; }; '
                          'note_unknown() { echo UNKNOWN; };\n# Absolute campaign headroom' + section)
                result = subprocess.run(["bash", "-c", script], env=self.env, cwd=self.root,
                                        capture_output=True, text=True, timeout=10)
                self.assert_ok(result)
                self.assertEqual(result.stdout.strip(), expected)

    def test_tmp_fixture_cannot_sweep_sibling_or_chrono_root(self):
        pad = self.old_scratch("claude-501")
        eligible = pad / "vs-worker-child"
        eligible.mkdir()
        self.age(pad)
        for root in (self.root.parent, pad):
            with self.subTest(root=root):
                result = self.run_prune("--apply", "--sweep-tmp",
                                        env=dict(self.env, BOARD_TMP_ROOT=str(root)))
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(eligible.exists())
        control = self.old_scratch("vs-worker-control")
        self.assert_ok(self.run_prune("--apply", "--sweep-tmp"))
        self.assertFalse(control.exists())
        self.assertTrue(eligible.exists())

    def test_watcher_settlement_hook_passes_only_settled_task(self):
        # Execute the actual handler; only its external collaborators are fixture
        # stubs. Starting the daemon here would create a real 900-second sleeper.
        watcher = (REPO / "bin/outbox-watcher.sh").read_text()
        handler = watcher.split("handle_response_path() {", 1)[1].split(
            "\n# fswatch reports changes", 1)[0]
        helper = self.root / "bin/disk-hygiene.sh"
        helper.unlink()  # fixture symlink, never repository code
        helper.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$VAULT_ROOT/cleanup.log"\n')
        helper.chmod(0o755)
        reconciler = self.root / "bin/registry-reconciler.sh"
        reconciler.write_text('#!/bin/sh\necho "reconciled $2 -> complete"\n')
        reconciler.chmod(0o755)
        outbox = self.root / "departments/coding/outbox"
        outbox.mkdir(parents=True)
        response = outbox / f"{TASK}-response.md"
        response.write_text("---\nstatus: complete\n---\nfixture\n")
        prelude = r'''
set -u
ALL_NAMESPACES=0; NAMESPACE=coding; PROCESSED_PATHS='|'; PENDING_PATHS='|'
SESSION=fixture; TMUX_BIN=false
frontmatter_field() { return 0; }
response_context() { echo fixture; }
response_status() { echo complete; }
status_nudge_prefix() { echo DONE; }
autocapture_dispatch() { :; }
record_skill_telemetry_best_effort() { :; }
'''
        script = prelude + "\nhandle_response_path() {" + handler + '\nhandle_response_path "$1"\n'
        result = subprocess.run(["bash", "-c", script, "fixture", str(response)],
                                env=self.env, cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assert_ok(result)
        self.assertEqual((self.root / "cleanup.log").read_text().splitlines(), ["--task-id", TASK])
        # A settlement hold never reaches cleanup.
        reconciler.write_text('#!/bin/sh\necho "v2-settlement-hold $2 -> pending"\n')
        self.assert_ok(subprocess.run(["bash", "-c", script, "fixture", str(response)],
                                     env=self.env, cwd=self.root, capture_output=True, text=True, timeout=10))
        self.assertEqual((self.root / "cleanup.log").read_text().splitlines(), ["--task-id", TASK])


if __name__ == "__main__":
    unittest.main()
