"""Retention controls use only owned temporary fixtures, never host cleanup."""

from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_hygiene as hygiene


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="disk-retention-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        from test_disk_hygiene_local_config import provision_config
        provision_config(self.root)
        self.state = self.root / "_state"
        self.rescued = self.state / "rescued-worker-artifacts"
        self.tmp = self.root / "tmp"
        self.chrome = self.root / "chrome"
        for pool in (self.rescued, self.tmp, self.chrome, self.state / "board-dispatch"):
            pool.mkdir(parents=True)
        self.now = time.time()
        self.registry = {}
        self.save_registry()
        fakebin = self.root / "fixturebin"
        fakebin.mkdir()
        (fakebin / "ps").write_text("#!/bin/sh\necho fixture-idle-process\n")
        (fakebin / "ps").chmod(0o755)
        env = mock.patch.dict(os.environ, SQUAD_TEST_ISOLATION="1",
                              PATH=str(fakebin) + os.pathsep + os.environ["PATH"],
                              BOARD_TMP_ROOT=str(self.tmp), BOARD_CHROME_CLONE_ROOT=str(self.chrome))
        env.start()
        self.addCleanup(env.stop)

    def save_registry(self):
        (self.state / "active-tasks.json").write_text(json.dumps(self.registry))

    def age(self, path, seconds):
        for entry in [*path.rglob("*"), path]:
            os.utime(entry, (self.now - seconds, self.now - seconds), follow_symlinks=False)

    def tree(self, pool, name, age=25 * 3600):
        path = pool / name
        path.mkdir()
        (path / "payload").write_bytes(b"fixture bytes" * 1024)
        self.age(path, age)
        return path

    def sweep(self, fn, real_lsof=False):
        output = io.StringIO()
        with redirect_stdout(output):
            if real_lsof:
                fn(self.root, self.now)
            else:
                with mock.patch.object(hygiene, "idle_path", return_value=True):
                    fn(self.root, self.now)
        return output.getvalue()

    def test_archive_is_alert_only_for_every_status_and_age(self):
        paths = []
        for status in ("complete", "closed", "cancelled", "superseded", "in-flight", "unknown"):
            path = self.tree(self.rescued, f"TASK-{status}", age=90 * hygiene.DAY)
            paths.append(path)
            self.registry[path.name] = {"status": status}
        self.save_registry()
        with mock.patch.object(hygiene.shutil, "rmtree") as remove:
            log = self.sweep(hygiene.sweep_rescued)
            remove.assert_not_called()
        self.assertTrue(all(path.exists() for path in paths))
        self.assertIn("alert-only", log)
        # Positive deletion control in a separately authorised category.
        control = self.tree(self.chrome, "code_sign_clone.control")
        self.sweep(hygiene.sweep_chrome)
        self.assertFalse(control.exists())

    def test_archive_retained_even_with_missing_or_malformed_registry(self):
        tree = self.tree(self.rescued, "TASK-unknown", age=90 * hygiene.DAY)
        for value in ("invalid json", "[]", "null", "{}"):
            (self.state / "active-tasks.json").write_text(value)
            self.assertIn("alert-only", self.sweep(hygiene.sweep_rescued))
            self.assertTrue(tree.exists())

    def test_clone_idle_open_and_one_hour_boundary(self):
        idle = self.tree(self.chrome, "code_sign_clone.idle", 3601)
        held = self.tree(self.chrome, "code_sign_clone.open", 3601)
        young = self.tree(self.chrome, "code_sign_clone.young", 3600)
        unrelated = self.tree(self.chrome, "unrelated", 7200)
        with (held / "payload").open():
            log = self.sweep(hygiene.sweep_chrome, real_lsof=True)
        self.assertFalse(idle.exists())
        self.assertTrue(held.exists() and young.exists() and unrelated.exists())
        self.assertIn("lsof", log)
        self.sweep(hygiene.sweep_chrome, real_lsof=True)
        self.assertFalse(held.exists())

    def test_lsof_only_accepts_quiet_exit_one(self):
        for code, stdout, stderr, expected in (
            (1, b"", b"", True), (0, b"123\n", b"", False),
            (1, b"", b"permission denied", False), (2, b"", b"", False),
            (1, b"123", b"", False), (0, b"", b"", False),
        ):
            with self.subTest(code=code, stdout=stdout, stderr=stderr):
                result = subprocess.CompletedProcess([], code, stdout, stderr)
                with mock.patch.object(hygiene.subprocess, "run", return_value=result) as run:
                    self.assertEqual(hygiene.idle_path(self.chrome), expected)
                    self.assertIn("+D", run.call_args.args[0])
        for error in (FileNotFoundError("lsof"), subprocess.TimeoutExpired("lsof", 15)):
            with mock.patch.object(hygiene.subprocess, "run", side_effect=error):
                self.assertFalse(hygiene.idle_path(self.chrome))

    def test_tmp_allowlist_wallet_profile_keychain_and_pinned_tools(self):
        removed = [self.tree(self.tmp, name) for name in
                   ("example-old-gocache", "example-gocache.fixture", "example-scratch-old",
                    "TASK-run-go-cache", "example-run-modcache")]
        kept = [self.tree(self.tmp, name) for name in
                ("unrelated", "TASK-2026-09-24-2100-tools", "example-caddy-2.11.2-pinned",
                 "example-age-toolchain", "age-build-go-cache",
                 "TASK-2026-09-25-2007-9157e996-age-fixture")]
        for index, marker in enumerate(("wallet.key", "login.keychain-db",
                                        "private-example-profile", "private-example-profile-backup",
                                        "TASK-2026-09-24-2100-venv",
                                        "TASK-2026-09-25-2007-9157e996-age-nested")):
            path = self.tree(self.tmp, f"example-protected-{index}-gocache")
            nested = path / "nested"
            nested.mkdir()
            if "profile" in marker or "venv" in marker:
                (nested / marker).mkdir()
            else:
                (nested / marker).touch()
            self.age(path, 9 * hygiene.DAY)
            kept.append(path)
        kept.append(self.tree(self.tmp, "example-young-gocache", age=12 * 3600))
        log = self.sweep(hygiene.sweep_test_tmp)
        self.assertFalse(any(path.exists() for path in removed))
        self.assertTrue(all(path.exists() for path in kept))
        self.assertIn("test-tmp: swept 5", log)
        self.assertIn("protected wallet/profile/tool", log)

    def test_tmp_open_file_retained_and_removed_after_close(self):
        held = self.tree(self.tmp, "example-open-gocache")
        idle = self.tree(self.tmp, "example-idle-gocache")
        with (held / "payload").open():
            self.sweep(hygiene.sweep_test_tmp, real_lsof=True)
        self.assertTrue(held.exists())
        self.assertFalse(idle.exists())
        self.sweep(hygiene.sweep_test_tmp, real_lsof=True)
        self.assertFalse(held.exists())

    def test_shared_container_owner_is_allowed_but_foreign_candidate_is_not(self):
        control = self.tree(self.tmp, "example-control-gocache")
        # A shared pool need not be owned by the sweeping user (/private/tmp is
        # root-owned); the actual candidate and its contents must be owned.
        with mock.patch.object(hygiene.os, "getuid", return_value=os.getuid() + 1):
            self.assertIn(control, hygiene.children(self.tmp))
            log = self.sweep(hygiene.sweep_test_tmp)
            self.assertIn("foreign mount or owner", log)
        self.assertTrue(control.exists())
        self.sweep(hygiene.sweep_test_tmp)
        self.assertFalse(control.exists())

    def test_unreadable_pool_is_not_reported_as_empty(self):
        with mock.patch.object(Path, "lstat", side_effect=PermissionError("fixture denied")):
            with self.assertRaises(PermissionError):
                hygiene.children(self.tmp)
        self.assertEqual(hygiene.children(self.tmp), [])

    def test_new_wallet_during_lsof_is_retained(self):
        tree = self.tree(self.tmp, "example-wallet-gocache")
        def probe(path):
            (path / "wallet.key").touch()
            self.age(path, 25 * 3600)
            return True
        with mock.patch.object(hygiene, "idle_path", side_effect=probe):
            self.sweep(hygiene.sweep_test_tmp, real_lsof=True)
        self.assertTrue(tree.exists())

    def test_readonly_cache_preserves_external_symlink_and_hardlink_permissions(self):
        outside = self.tree(self.tmp, "external")
        sentinel = outside / "payload"
        sentinel.chmod(0o444)
        tree = self.tree(self.tmp, "example-fixture-modcache")
        nested = tree / "readonly"
        nested.mkdir()
        (nested / "link").symlink_to(outside, target_is_directory=True)
        (nested / "hardlink").hardlink_to(sentinel)
        self.age(tree, 25 * 3600)
        nested.chmod(0o555)
        self.sweep(hygiene.sweep_test_tmp)
        self.assertFalse(tree.exists())
        self.assertTrue(sentinel.exists())
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o444)

    def test_test_roots_cannot_escape_fixture_or_discover_live_chrome(self):
        control = self.tree(self.tmp, "example-control-gocache")
        for variable, sweep in (("BOARD_TMP_ROOT", hygiene.sweep_test_tmp),
                                 ("BOARD_CHROME_CLONE_ROOT", hygiene.sweep_chrome)):
            with mock.patch.dict(os.environ, {variable: str(self.root.parent)}):
                with self.assertRaises(hygiene.Retained):
                    self.sweep(sweep)
        with mock.patch.dict(os.environ):
            os.environ.pop("BOARD_CHROME_CLONE_ROOT", None)
            with mock.patch.object(hygiene.subprocess, "run") as run:
                self.assertIsNone(hygiene.chrome_root(self.root))
                run.assert_not_called()
        self.sweep(hygiene.sweep_test_tmp)
        self.assertFalse(control.exists())

    def test_periodic_respects_preservation_lock(self):
        control = self.tree(self.tmp, "example-control-gocache")
        with (self.state / "prune-board-worktrees.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(hygiene.periodic_sweep(self.root), 1)
        self.assertTrue(control.exists())
        with redirect_stdout(io.StringIO()), mock.patch.object(hygiene, "idle_path", return_value=True):
            self.assertEqual(hygiene.periodic_sweep(self.root), 0)
        self.assertFalse(control.exists())


if __name__ == "__main__":
    unittest.main()
