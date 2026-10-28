"""Disk-cleanup skill pools: vault snapshots, rescued bulk, docker, the plan CLI.

Every deletion runs inside a throwaway fixture; SQUAD_TEST_ISOLATION plus the
BOARD_* overrides keep the real home, snapshot pool, and host daemon untouched.
Each class carries a positive control: a referenced/in-flight pool that must be
skipped, and a key file that must never be deleted.
"""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_hygiene as hygiene
import test_disk_hygiene as disk_fixtures


class PoolFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="disk-pools-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        from test_disk_hygiene_local_config import provision_config
        provision_config(self.root)
        self.state = self.root / "_state"
        self.dispatch = self.state / "board-dispatch"
        self.snaps = self.root / "vault-snapshots"
        self.rescued = self.state / "rescued-worker-artifacts"
        for path in (self.dispatch, self.snaps, self.rescued):
            path.mkdir(parents=True)
        self.registry = {}
        self.save_registry()
        self.now = time.time()
        patch = mock.patch.dict(os.environ, SQUAD_TEST_ISOLATION="1",
                                BOARD_VAULT_SNAPSHOTS_ROOT=str(self.snaps))
        patch.start()
        self.addCleanup(patch.stop)

    def save_registry(self):
        (self.state / "active-tasks.json").write_text(json.dumps(self.registry))

    def snapshot(self, name, age_days):
        path = self.snaps / name
        path.write_bytes(b"x" * 4096)
        stamp = self.now - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def rescue_bulk(self, task, attempt="d-0000abcd", status="complete"):
        base = self.rescued / task / attempt
        (base / ".cache" / "sub").mkdir(parents=True)
        (base / ".cache" / "blob").write_bytes(b"y" * 8192)
        (base / "report.md").write_text("the only surviving analysis")
        if status is not None:
            self.registry[task] = {"status": status}
            self.save_registry()
        stamp = self.now - 25 * 3600
        for item in [*base.rglob("*"), base, base.parent]:
            os.utime(item, (stamp, stamp))
        return base

    def quiet_apply(self, func, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(hygiene, "idle_path", return_value=True), \
                mock.patch.object(hygiene, "process_idle", return_value=True):
            result = func(self.root, now=self.now, **kwargs)
        return result, out.getvalue()


class VaultSnapshotTests(PoolFixture):
    def test_keeps_newest_three_and_deletes_older(self):
        files = [self.snapshot(f"vault-snapshot-{i}.tar.gz", age_days=i) for i in range(5)]
        with redirect_stdout(io.StringIO()):
            removed, _ = hygiene.sweep_vault_snapshots(self.root, keep=3, apply=True)
        self.assertEqual(removed, 2)
        self.assertTrue(all(files[i].exists() for i in (0, 1, 2)))  # newest kept
        self.assertFalse(any(files[i].exists() for i in (3, 4)))    # oldest removed

    def test_dry_run_plan_deletes_nothing(self):
        files = [self.snapshot(f"vault-snapshot-{i}.tgz", age_days=i) for i in range(5)]
        out = io.StringIO()
        with redirect_stdout(out):
            removed, reclaim = hygiene.sweep_vault_snapshots(self.root, keep=3, apply=False)
        self.assertEqual(removed, 0)
        self.assertTrue(all(f.exists() for f in files))
        self.assertGreater(reclaim, 0)
        self.assertIn("PLAN delete", out.getvalue())

    def test_symlink_and_non_tarball_are_never_candidates(self):
        recent = [self.snapshot(f"vault-snapshot-{i}.tar", age_days=i) for i in range(3)]
        self.snapshot("vault-snapshot-ancient.tar", age_days=99)
        (self.snaps / "README.md").write_text("not a tarball")
        (self.snaps / "latest.tar").symlink_to(recent[0])  # tar suffix, but a link
        with redirect_stdout(io.StringIO()):
            _, plan = hygiene.plan_vault_snapshots(self.root, keep=3)
        self.assertEqual({path.name for path, _ in plan}, {"vault-snapshot-ancient.tar"})


class RescuedBulkTests(PoolFixture):
    def test_regenerable_bulk_deleted_but_documents_retained(self):
        base = self.rescue_bulk("TASK-done")
        (removed, _), log = self.quiet_apply(hygiene.sweep_rescued_bulk, apply=True)
        self.assertEqual(removed, 1)
        self.assertFalse((base / ".cache").exists())
        self.assertEqual((base / "report.md").read_text(), "the only surviving analysis")
        self.assertIn("documents retained", log)

    def test_live_referenced_task_bulk_is_retained(self):
        base = self.rescue_bulk("TASK-live", status="in-flight")  # positive control
        (removed, _), _ = self.quiet_apply(hygiene.sweep_rescued_bulk, apply=True)
        self.assertEqual(removed, 0)
        self.assertTrue((base / ".cache").exists())

    def test_wallet_key_inside_bulk_is_never_deleted(self):
        base = self.rescue_bulk("TASK-done")
        key = base / ".cache" / "wallet.key"  # protected name inside a bulk subtree
        key.write_text("fixture key material")
        stamp = self.now - 25 * 3600
        os.utime(key, (stamp, stamp))
        (removed, _), _ = self.quiet_apply(hygiene.sweep_rescued_bulk, apply=True)
        self.assertEqual(removed, 0)
        self.assertTrue(key.exists())
        self.assertTrue((base / ".cache").exists())

    def test_dry_run_plan_deletes_nothing(self):
        base = self.rescue_bulk("TASK-done")
        out = io.StringIO()
        with redirect_stdout(out):
            removed, reclaim = hygiene.sweep_rescued_bulk(self.root, now=self.now, apply=False)
        self.assertEqual(removed, 0)
        self.assertTrue((base / ".cache").exists())
        self.assertGreater(reclaim, 0)


class DockerSweepTests(unittest.TestCase):
    def test_absent_docker_cli_skips_without_touching_anything(self):
        out = io.StringIO()
        with mock.patch.object(hygiene.shutil, "which", return_value=None), redirect_stdout(out):
            self.assertIsNone(hygiene.sweep_docker(apply=True))
        self.assertIn("not installed", out.getvalue())

    def test_running_container_skips_container_prune_but_keeps_image_prune(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            sub = args[1]
            if sub == "system":
                return subprocess.CompletedProcess(args, 0, "TYPE   SIZE\nImages 1GB", "")
            if sub == "ps":
                return subprocess.CompletedProcess(args, 0, "abc123\n", "")  # one running
            return subprocess.CompletedProcess(args, 0, "pruned", "")

        def fake_which(name):
            return "/usr/bin/docker" if name == "docker" else None  # no colima

        out = io.StringIO()
        with mock.patch.object(hygiene.shutil, "which", side_effect=fake_which), \
                mock.patch.object(hygiene.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(out):
            hygiene.sweep_docker(apply=True)
        subcommands = [args[1] for args in calls]
        self.assertNotIn("container", subcommands)  # prune skipped while a container runs
        self.assertIn("image", subcommands)         # dangling-image prune still happens
        self.assertIn("containers running", out.getvalue())


class CleanupPlanTests(PoolFixture):
    def test_plan_mode_surveys_and_deletes_nothing(self):
        snaps = [self.snapshot(f"vault-snapshot-{i}.tar", age_days=i) for i in range(5)]
        base = self.rescue_bulk("TASK-done")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(hygiene, "sweep_docker"), \
                mock.patch.object(hygiene, "disk_report", return_value="survey-stub"):
            status = hygiene.cleanup_plan(self.root, "plan", now=self.now)
        self.assertEqual(status, 0)
        self.assertTrue(all(s.exists() for s in snaps))
        self.assertTrue((base / ".cache").exists())
        self.assertIn("disk-cleanup survey", out.getvalue())

    def test_apply_safe_runs_shared_board_sweep_and_vault_retention(self):
        snaps = [self.snapshot(f"vault-snapshot-{i}.tar", age_days=i) for i in range(5)]
        base = self.rescue_bulk("TASK-done")
        with redirect_stdout(io.StringIO()), \
                mock.patch.object(hygiene, "run_safe_sweep", return_value=0) as sweep, \
                mock.patch.object(hygiene, "sweep_docker"), \
                mock.patch.object(hygiene, "disk_report", return_value="survey-stub"):
            status = hygiene.cleanup_plan(self.root, "apply-safe", now=self.now)
        sweep.assert_called_once()  # Tier-1 board sweep shared with --periodic
        self.assertEqual(status, 0)
        self.assertEqual(sum(1 for s in snaps if s.exists()), 3)  # vault retention applied
        self.assertTrue((base / ".cache").exists())  # Tier 2 NOT run under apply-safe


class CleanupPlanCliTests(unittest.TestCase):
    """Exercise the real bin/disk-hygiene.sh --plan path, seams only the disk read."""

    def setUp(self):
        self.fixture = disk_fixtures.DiskHygieneTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_plan_flag_is_read_only_and_prints_survey(self):
        f = self.fixture
        seeded = f.old_scratch("example-plan-readonly")
        result = f.disk("--plan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("disk-cleanup survey", result.stdout)
        self.assertTrue(seeded.exists())  # --plan never deletes


if __name__ == "__main__":
    unittest.main()


class ToolVersionTests(PoolFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / 'home'
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, BOARD_TOOL_VERSIONS_HOME=str(self.home))
        patch.start()
        self.addCleanup(patch.stop)
        self.pools = hygiene.tool_version_pools(self.root)
        for _, pool, current, directories in self.pools:
            pool.mkdir(parents=True)
            current.parent.mkdir(parents=True, exist_ok=True)
            for index in range(4):
                path = pool / f'1.0.{index}'
                if directories:
                    path.mkdir()
                    (path / 'binary').write_bytes(b'x' * 4096)
                    os.utime(path / 'binary', (self.now - 86400, self.now - 86400))
                else:
                    path.write_bytes(b'x' * 4096)
                os.utime(path, (self.now - (4 - index) * 86400,) * 2)
            # Active is deliberately oldest; newest OTHER (1.0.3) must also stay.
            current.symlink_to(pool / '1.0.0')

    def apply_versions(self, apply=True):
        with redirect_stdout(io.StringIO()):
            return hygiene.sweep_tool_versions(self.root, now=self.now, apply=apply)

    def test_keeps_active_and_newest_other_with_deletion_control(self):
        with mock.patch.object(hygiene, 'process_idle', return_value=True), \
                mock.patch.object(hygiene, 'idle_path', return_value=True):
            removed, size = self.apply_versions(apply=False)
            self.assertEqual(removed, 0)
            self.assertGreater(size, 0)
            self.assertTrue(all((pool / '1.0.1').exists() for _, pool, _, _ in self.pools))
            self.assertEqual(self.apply_versions()[0], 4)
        for _, pool, _, _ in self.pools:
            self.assertTrue((pool / '1.0.0').exists() and (pool / '1.0.3').exists())
            self.assertFalse((pool / '1.0.1').exists() or (pool / '1.0.2').exists())

    def test_mapped_version_and_census_failure_retained_with_idle_control(self):
        def run(args, **kwargs):
            if args[0] == 'ps':
                return subprocess.CompletedProcess(args, 0, 'unrelated process', '')
            self.assertEqual(args[0], 'lsof')
            if args[-1].endswith('1.0.1'):
                return subprocess.CompletedProcess(args, 0, b'123\n', b'')
            return subprocess.CompletedProcess(args, 1, b'', b'')
        with mock.patch.object(hygiene.subprocess, 'run', side_effect=run) as calls:
            self.assertEqual(self.apply_versions()[0], 2)
        for _, pool, _, directories in self.pools:
            self.assertTrue((pool / '1.0.1').exists())
            self.assertFalse((pool / '1.0.2').exists())
            selections = [c.args[0] for c in calls.call_args_list if c.args[0][0] == 'lsof'
                          and c.args[0][-1] == str(pool / '1.0.1')]
            self.assertTrue(selections)
            self.assertEqual(selections[0][-2], '+D' if directories else '--')
        for response in (subprocess.CompletedProcess([], 1, b'', b'denied'),
                         subprocess.CompletedProcess([], 2, b'', b'')):
            with mock.patch.object(hygiene.subprocess, 'run', return_value=response):
                self.assertEqual(self.apply_versions()[0], 0)

    def test_real_mapped_binary_is_retained(self):
        import mmap
        path = self.pools[1][1] / '1.0.1'
        with path.open('rb') as stream:
            mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            self.assertFalse(hygiene.idle_path(path))
        finally:
            mapping.close()
        self.assertTrue(hygiene.idle_path(path))

    def test_failed_lsof_retains_all_candidates(self):
        for error in (None, OSError('denied'), subprocess.TimeoutExpired('lsof', 15)):
            def run(args, **kwargs):
                if args[0] == 'ps':
                    return subprocess.CompletedProcess(args, 0, 'unrelated process', '')
                if error is not None:
                    raise error
                return subprocess.CompletedProcess(args, 1, b'', b'permission denied')
            with mock.patch.object(hygiene.subprocess, 'run', side_effect=run):
                self.assertEqual(self.apply_versions()[0], 0)
        self.assertTrue(all((pool / '1.0.1').exists() and (pool / '1.0.2').exists()
                            for _, pool, _, _ in self.pools))

    def test_missing_broken_external_links_and_symlink_entries_fail_safe(self):
        for _, pool, current, _ in self.pools:
            current.unlink()
            for target in (None, pool / '9.9.9', self.root):
                if target is not None:
                    current.symlink_to(target)
                with self.assertRaises(OSError):
                    hygiene.plan_tool_versions(pool, current, pool == self.pools[0][1])
                if current.is_symlink():
                    current.unlink()
            current.symlink_to(pool / '1.0.0')
            (pool / '0.0.1').symlink_to(self.root)
            (pool / 'README').write_text('keep')
        with mock.patch.object(hygiene, 'process_idle', return_value=True), \
                mock.patch.object(hygiene, 'idle_path', return_value=True):
            self.assertEqual(self.apply_versions()[0], 4)
        self.assertTrue(all((pool / '0.0.1').is_symlink() and (pool / 'README').exists()
                            for _, pool, _, _ in self.pools))

    def test_active_target_changed_during_census_is_retained(self):
        def idle(path):
            for _, pool, current, _ in self.pools:
                if path == pool / '1.0.1':
                    current.unlink()
                    current.symlink_to(path)
            return True
        with mock.patch.object(hygiene, 'process_idle', side_effect=idle), \
                mock.patch.object(hygiene, 'idle_path', return_value=True):
            self.apply_versions()
        self.assertTrue(all((pool / '1.0.1').exists() for _, pool, _, _ in self.pools))

    def test_fixture_home_cannot_escape_and_missing_override_disables_discovery(self):
        with mock.patch.dict(os.environ, BOARD_TOOL_VERSIONS_HOME=str(self.root.parent)):
            with self.assertRaises(hygiene.Retained):
                hygiene.tool_version_pools(self.root)
        with mock.patch.dict(os.environ):
            os.environ.pop('BOARD_TOOL_VERSIONS_HOME')
            self.assertEqual(hygiene.tool_version_pools(self.root), [])

    def test_tier_one_routes_version_sweep(self):
        for mode, apply in [('plan', False), ('apply-safe', True), ('apply-preserve', True)]:
            with redirect_stdout(io.StringIO()), mock.patch.object(hygiene, 'disk_report'), \
                    mock.patch.object(hygiene, 'run_safe_sweep', return_value=0), \
                    mock.patch.object(hygiene, 'sweep_vault_snapshots'), \
                    mock.patch.object(hygiene, 'sweep_docker'), \
                    mock.patch.object(hygiene, 'sweep_rescued_bulk'), \
                    mock.patch.object(hygiene, 'sweep_tool_versions') as sweep:
                hygiene.cleanup_plan(self.root, mode, now=self.now)
            sweep.assert_called_once_with(self.root, now=self.now, apply=apply)
