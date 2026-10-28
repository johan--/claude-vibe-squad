"""Operator-approved sweeps: temp Git/files, fake PG clients, no host mutation."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_hygiene as hygiene
import test_disk_hygiene as disk_fixtures


class SweepFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="disk-20260930-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        from test_disk_hygiene_local_config import provision_config
        provision_config(self.root)
        self.state = self.root / "_state"
        self.dispatch = self.state / "board-dispatch"
        self.tmp = self.root / "tmp"
        self.chrome = self.root / "chrome"
        for path in (self.dispatch, self.tmp, self.chrome):
            path.mkdir(parents=True)
        self.registry = {}
        self.save_registry()
        self.now = time.time()
        patch = mock.patch.dict(os.environ, SQUAD_TEST_ISOLATION="1",
                                BOARD_TMP_ROOT=str(self.tmp), BOARD_CHROME_CLONE_ROOT=str(self.chrome))
        patch.start()
        self.addCleanup(patch.stop)

    def save_registry(self):
        (self.state / "active-tasks.json").write_text(json.dumps(self.registry))

    def tree(self, name, age=13 * 3600):
        path = self.tmp / name
        path.mkdir()
        (path / "payload").write_text("disposable test bytes")
        for item in [*path.rglob("*"), path]:
            os.utime(item, (self.now - age, self.now - age))
        return path

    def sweep(self):
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(hygiene, "idle_path", return_value=True), \
                mock.patch.object(hygiene, "process_idle", return_value=True):
            hygiene.sweep_test_tmp(self.root, self.now)
        return output.getvalue()


class SweepsTests(SweepFixture):
    def test_twelve_hour_boundary_unknown_files_and_recent_descendants(self):
        old = self.tree("example-old-gocache")
        young = self.tree("young-go-cache", 12 * 3600)
        active = self.tree("example-scratch-active")
        (active / "new").touch()
        unknown = self.tree("example-customer-data")
        user = self.tmp / "example-user.txt"
        user.write_text("user material")
        link = self.tmp / "linked-go-cache"
        link.symlink_to(unknown)
        self.sweep()
        self.assertFalse(old.exists())
        self.assertTrue(all(p.exists() for p in (young, active, unknown, user, link)))

    def test_live_registry_reference_and_task_suffix_with_idle_control(self):
        referenced = self.tree("example-scratch-referenced")
        suffix = self.tree("example-abcdef12-gocache")
        control = self.tree("example-idle-gocache")
        self.registry["TASK-live-abcdef12"] = {"status": "in-flight", "read_context": [str(referenced)]}
        self.save_registry()
        self.sweep()
        self.assertTrue(referenced.exists() and suffix.exists())
        self.assertFalse(control.exists())

    def test_terminal_registry_with_live_pid_protects_context_reference(self):
        referenced = self.tree("example-scratch-referenced")
        task, attempt = "TASK-live", "d-0123"
        self.registry[task] = {"status": "complete"}
        self.save_registry()
        (self.dispatch / f"{task}.{attempt}.dispatch.json").write_text(json.dumps(
            {"task_id": task, "pid": os.getpid()}))
        (self.dispatch / f"{task}.{attempt}.context.json").write_text(json.dumps({"paths": [str(referenced)]}))
        control = self.tree("idle-go-cache")
        self.sweep()
        self.assertTrue(referenced.exists())
        self.assertFalse(control.exists())

    def test_failed_registry_or_descriptor_probe_is_not_an_empty_census(self):
        control = self.tree("idle-go-cache")
        (self.state / "active-tasks.json").write_text("invalid")
        with self.assertRaises(hygiene.Retained):
            self.sweep()
        self.assertTrue(control.exists())
        self.save_registry()
        path = self.dispatch / "TASK-broken.d-0123.dispatch.json"
        path.write_text("invalid")
        with self.assertRaises(hygiene.Retained):
            self.sweep()
        self.assertTrue(control.exists())
        path.write_text('{"task_id":"TASK-broken"}')
        self.registry["TASK-broken"] = {"status": "complete"}
        self.save_registry()
        self.sweep()
        self.assertFalse(control.exists())

    def test_reference_created_during_handle_census_retains_source(self):
        path = self.tree("late-go-cache")
        def probe(_):
            self.registry["TASK-late"] = {"status": "running", "cache": str(path)}
            self.save_registry()
            return True
        with mock.patch.object(hygiene, "idle_path", side_effect=probe), \
                mock.patch.object(hygiene, "process_idle", return_value=True), redirect_stdout(io.StringIO()):
            hygiene.sweep_test_tmp(self.root, self.now)
        self.assertTrue(path.exists())

    def test_process_reference_and_failed_ps_keep_clone_with_idle_control(self):
        path = self.chrome / "code_sign_clone.fixture"
        for result in (subprocess.CompletedProcess([], 0, f"chrome {path}", ""),
                       subprocess.CompletedProcess([], 1, "", "denied"),
                       subprocess.CompletedProcess([], 0, "", "")):
            with mock.patch.object(hygiene.subprocess, "run", return_value=result):
                self.assertFalse(hygiene.process_idle(path))
        with mock.patch.object(hygiene.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 0, "unrelated process\n", "")), \
                mock.patch.object(hygiene, "idle_path", return_value=True):
            self.assertTrue(hygiene.process_idle(path))

    def test_default_periodic_never_expires_rescued_archive_even_with_old_flag(self):
        rescued = self.state / "rescued-worker-artifacts/TASK-old"
        rescued.mkdir(parents=True)
        (rescued / "result").write_text("only surviving result")
        control = self.tree("idle-go-cache")
        with mock.patch.dict(os.environ, DISK_HYGIENE_AUTODELETE="1"), \
                mock.patch.object(hygiene, "idle_path", return_value=True), \
                mock.patch.object(hygiene, "process_idle", return_value=True), redirect_stdout(io.StringIO()):
            self.assertEqual(hygiene.periodic_sweep(self.root), 0)
        self.assertTrue(rescued.exists())
        self.assertFalse(control.exists())


class PrunerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = disk_fixtures.DiskHygieneTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_verified_rescue_removed_mismatch_and_live_retained(self):
        f = self.fixture
        good, _, _ = f.attempt(task="TASK-good", promoted=False)
        bad, _, _ = f.attempt(2, "TASK-bad", promoted=False)
        live, _, _ = f.attempt(3, "TASK-live", promoted=False, status="in-flight")
        for path in (good, bad, live):
            (path / "result").write_text("irreplaceable")
        saved = f.state / "rescued-worker-artifacts/TASK-bad" / bad.name / "result"
        saved.parent.mkdir(parents=True)
        saved.write_text("wrong content")
        result = f.run_prune("--apply")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(good.exists())
        self.assertTrue(bad.exists() and live.exists())
        good_copy = f.state / "rescued-worker-artifacts/TASK-good" / good.name / "result"
        self.assertEqual(good_copy.read_text(), "irreplaceable")
        self.assertIn("rescued-and-retained 1; live retained 1", result.stdout)

    def test_failed_new_copy_retains_worktree_and_control_removes(self):
        f = self.fixture
        wt, _, _ = f.attempt(promoted=False)
        (wt / "result").write_text("irreplaceable")
        driver = r'''
import pathlib, shutil, sys
source = pathlib.Path(sys.argv[1]).read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
shutil.copy2 = lambda *args, **kwargs: None
sys.argv = [sys.argv[1], "--apply"]
exec(compile(source, sys.argv[0], "exec"))
'''
        result = subprocess.run([sys.executable, "-B", "-c", driver, str(disk_fixtures.REPO /
                                "bin/prune-board-worktrees.sh")], env=f.env, cwd=f.root,
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(wt.exists())
        f.assert_ok(f.run_prune("--apply"))
        self.assertFalse(wt.exists())

    def test_young_attempt_scratch_retained_with_old_control(self):
        f = self.fixture
        _, young, _ = f.attempt(worktree=False)
        (young / "recent").touch()
        _, old, _ = f.attempt(2, "TASK-old", worktree=False)
        f.assert_ok(f.run_prune("--apply"))
        self.assertTrue(young.exists())
        self.assertFalse(old.exists())


FAKE_PG = r'''
import json, os, pathlib, sys
data = pathlib.Path(DATA)
state_path = data / "fixture.json"
state = json.loads(state_path.read_text())
name = pathlib.Path(sys.argv[0]).name
sql = sys.stdin.read() if name == "psql" else ""
assert "--host=" + str(data) in sys.argv and "--port=55432" in sys.argv
assert "PGHOST" not in os.environ and "PGPASSWORD" not in os.environ
assert "--username=fixture_admin" in sys.argv
assert "--force" not in sys.argv and "--no-password" in sys.argv
state.setdefault("calls", []).append({"tool": name, "args": sys.argv[1:], "sql": sql})
if name == "dropdb":
    target = sys.argv[-1]
    assert target.startswith("example_test_")
    row = next(r for r in state["databases"] if r["name"] == target)
    if row["connections"] or state.get("drop_race"):
        state_path.write_text(json.dumps(state))
        sys.exit(1)
    state["databases"].remove(row)
elif "VACUUM FULL" in sql:
    assert "pid <> pg_backend_pid()" in sql and "lock_timeout" in sql
    row = next(r for r in state["databases"] if r["name"] == "example_test")
    if row["connections"] or state.get("vacuum_race"):
        print("hygiene_retained")
    else:
        state["vacuumed"] = True
        print("hygiene_compacted")
else:
    state["snapshots"] = state.get("snapshots", 0) + 1
    if state.get("connect_after_census") and state["snapshots"] > 1:
        for row in state["databases"]:
            if row["name"].startswith("example_test_"):
                row["connections"] = 1
    print(json.dumps({"data_directory": state.get("data_directory", str(data)),
                      "port": "55432", "databases": state["databases"]}))
state_path.write_text(json.dumps(state))
'''


class PostgresTests(SweepFixture):
    def setUp(self):
        super().setUp()
        self.data = self.root / "test-pg"
        self.data.mkdir()
        (self.data / "PG_VERSION").write_text("17")
        self.fakebin = self.root / "fakebin"
        self.fakebin.mkdir()
        for name in ("psql", "dropdb"):
            path = self.fakebin / name
            path.write_text(f"#!{sys.executable}\nDATA = {str(self.data)!r}\n" + FAKE_PG)
            path.chmod(0o755)
        patch = mock.patch.dict(os.environ, BOARD_TEST_PG_ROOT=str(self.data),
                                PATH=str(self.fakebin) + os.pathsep + os.environ["PATH"],
                                PGHOST="unapproved.example.invalid", PGPASSWORD="fixture-do-not-inherit")
        patch.start()
        self.addCleanup(patch.stop)
        self.ages = {}
        self.set_databases([self.row("example_test", size=3 * hygiene.GIB),
                            self.row("example_test_b_fixture", oid=101)])

    @staticmethod
    def row(name, oid=100, size=1000, connections=0):
        return {"name": name, "oid": oid, "bytes": size, "connections": connections}

    def set_databases(self, rows, **options):
        (self.data / "fixture.json").write_text(json.dumps({"databases": rows, **options}))

    def result(self):
        return json.loads((self.data / "fixture.json").read_text())

    def run_pg(self, free=20 * hygiene.GIB):
        output = io.StringIO()
        def age(pg, row, now):
            return self.ages.get(row["name"], 7 * 3600)
        with redirect_stdout(output), mock.patch.object(hygiene.TestPostgres, "age", age), \
                mock.patch.object(hygiene.shutil, "disk_usage", return_value=SimpleNamespace(free=free)):
            hygiene.sweep_test_databases(self.root, self.now)
        return output.getvalue()

    def test_fake_dropdb_obeys_age_connections_name_and_compacts_idle_main(self):
        names = ("example_test_b_old", "example_test_a_young", "example_test_fa_connected",
                 "example_test_b2_boundary")
        rows = [self.row("example_test", size=3 * hygiene.GIB)]
        rows += [self.row(name, oid=101+i, connections=int(i == 2)) for i, name in enumerate(names)]
        self.set_databases(rows)
        self.ages = {names[1]: 3600, names[3]: 6 * 3600}
        log = self.run_pg()
        result = self.result()
        drops = [call for call in result["calls"] if call["tool"] == "dropdb"]
        self.assertEqual([call["args"][-1] for call in drops], [names[0]])
        self.assertTrue(result["vacuumed"])
        self.assertIn("VACUUM FULL", log)
        self.assertEqual({row["name"] for row in result["databases"]}, {"example_test", *names[1:]})

    def test_main_threshold_connections_and_rewrite_headroom_controls(self):
        for size, connections, free in ((2 * hygiene.GIB, 0, 20 * hygiene.GIB),
                                         (3 * hygiene.GIB, 1, 20 * hygiene.GIB),
                                         (3 * hygiene.GIB, 0, 2 * hygiene.GIB)):
            with self.subTest(size=size, connections=connections, free=free):
                self.set_databases([self.row("example_test", size=size, connections=connections)])
                self.run_pg(free)
                self.assertNotIn("vacuumed", self.result())
        self.set_databases([self.row("example_test", size=3 * hygiene.GIB)])
        self.run_pg()
        self.assertTrue(self.result()["vacuumed"])

    def test_new_connection_and_dropdb_refusal_never_force_deletion(self):
        for option in ("connect_after_census", "drop_race"):
            with self.subTest(option=option):
                self.set_databases([self.row("example_test_b_fixture")], **{option: True})
                self.run_pg()
                self.assertEqual(len(self.result()["databases"]), 1)
        self.set_databases([self.row("example_test_b_fixture")])
        self.run_pg()
        self.assertEqual(self.result()["databases"], [])

    def test_wrong_cluster_and_bad_census_fail_before_any_mutation(self):
        self.set_databases([self.row("example_test_b_fixture")], data_directory="/unapproved")
        with self.assertRaises(hygiene.Retained):
            self.run_pg()
        self.assertFalse(any(call["tool"] == "dropdb" for call in self.result()["calls"]))
        self.set_databases([self.row("production")])
        with self.assertRaises(hygiene.Retained):
            self.run_pg()
        self.set_databases([self.row("example_test_b_fixture")])
        self.run_pg()
        self.assertEqual(self.result()["databases"], [])

    def test_live_database_reference_and_session_vacuum_recheck(self):
        self.registry["TASK-deploy"] = {"status": "in-flight", "db": "example_test_b_fixture"}
        self.save_registry()
        self.set_databases([self.row("example_test", size=3 * hygiene.GIB),
                            self.row("example_test_b_fixture", oid=101)], vacuum_race=True)
        log = self.run_pg()
        self.assertEqual(len(self.result()["databases"]), 2)
        self.assertNotIn("vacuumed", self.result())
        self.assertIn("connections/size changed", log)

    def test_birth_time_is_age_oracle_not_name_and_missing_age_retains(self):
        pg = hygiene.TestPostgres(self.root)
        entry = SimpleNamespace(st_mode=stat.S_IFDIR, st_uid=os.getuid(), st_birthtime=self.now - 25000,
                                st_ctime=self.now, st_mtime=self.now)
        with mock.patch.object(Path, "lstat", return_value=entry):
            self.assertEqual(pg.age(self.row("example_test_recent_name"), self.now), 25000)
        with mock.patch.object(Path, "lstat", side_effect=FileNotFoundError()):
            with self.assertRaises(OSError):
                pg.age(self.row("example_test_old_name"), self.now)

    def test_fixture_cannot_connect_without_explicit_contained_pg_root(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("BOARD_TEST_PG_ROOT")
            with mock.patch.object(hygiene.subprocess, "run") as run:
                self.assertIsNone(hygiene.test_pg_root(self.root))
                hygiene.sweep_test_databases(self.root, self.now)
                run.assert_not_called()
        with mock.patch.dict(os.environ, BOARD_TEST_PG_ROOT=str(self.root.parent)):
            with self.assertRaises(hygiene.Retained):
                hygiene.test_pg_root(self.root)


class AccountingTests(unittest.TestCase):
    def test_apfs_container_and_preboot_update_allocation_from_plist(self):
        info = {"APFSContainerReference": "disk3"}
        containers = {"Containers": [{"ContainerReference": "disk3", "CapacityFree": 8 * hygiene.GIB,
            "CapacityCeiling": 500 * hygiene.GIB, "Volumes": [
                {"Roles": ["Preboot"], "CapacityInUse": 15 * hygiene.GIB},
                {"Roles": ["Update"], "CapacityInUse": hygiene.GIB}]}]}
        results = [subprocess.CompletedProcess([], 0, plistlib.dumps(x), b"") for x in (info, containers)]
        with mock.patch.dict(os.environ, SQUAD_TEST_ISOLATION="0"), \
                mock.patch.object(hygiene.sys, "platform", "darwin"), \
                mock.patch.object(hygiene.subprocess, "run", side_effect=results):
            report = hygiene.apfs_report()
        self.assertIn("8.00 GiB / 500.00 GiB", report)
        self.assertIn("Preboot: 15.00 GiB allocated", report)
        self.assertIn("Update: 1.00 GiB allocated", report)
        self.assertIn("leave staged updates", report)

    def test_unavailable_apfs_measurement_is_unknown_not_zero(self):
        with mock.patch.dict(os.environ, SQUAD_TEST_ISOLATION="0"), \
                mock.patch.object(hygiene.sys, "platform", "darwin"), \
                mock.patch.object(hygiene.subprocess, "run", side_effect=OSError("fixture denied")):
            report = hygiene.apfs_report()
        self.assertIn("UNKNOWN", report)
        self.assertNotIn("0.00 GiB", report)

    def test_allocated_blocks_count_hardlinks_once_and_no_symlink_traversal(self):
        with tempfile.TemporaryDirectory(prefix="disk-accounting-") as temp:
            root = Path(temp)
            file = root / "data"
            file.write_bytes(b"x" * 16384)
            (root / "hardlink").hardlink_to(file)
            (root / "link").symlink_to(root)
            expected = sum(p.lstat().st_blocks * 512 for p in (root, file, root / "link"))
            used, unique = hygiene.allocated_usage(root)
            self.assertEqual(used, expected)
            self.assertIsNone(unique)  # external hardlinks cannot be attributed safely

    @unittest.skipUnless(sys.platform == "darwin", "APFS private-size API is macOS-only")
    def test_real_apfs_clone_private_allocation_is_less_than_shared_blocks(self):
        with tempfile.TemporaryDirectory(prefix="disk-clone-") as temp:
            source, clone = Path(temp) / "source", Path(temp) / "clone"
            source.write_bytes(os.urandom(1024 * 1024))
            result = subprocess.run(["/bin/cp", "-c", str(source), str(clone)], capture_output=True)
            if result.returncode or hygiene.private_bytes(clone) is None:
                self.skipTest("fixture filesystem does not expose clone/private allocation")
            self.assertLess(hygiene.private_bytes(clone), clone.stat().st_blocks * 512)
            self.assertEqual(source.read_bytes(), clone.read_bytes())


if __name__ == "__main__":
    unittest.main()


class ChronoScratchTests(SweepFixture):
    def setUp(self):
        super().setUp()
        self.chrono = self.tmp / 'chrono-scratch'
        self.chrono.mkdir()

    def scratch(self, name, age=49 * 3600):
        return self.tree('chrono-scratch/' + name, age)

    def test_48_hour_ttl_and_protection_controls(self):
        old = self.scratch('20261025-old')
        boundary = self.scratch('boundary', 48 * 3600)
        young = self.scratch('young', 47 * 3600)
        recent = self.scratch('recent')
        (recent / 'fresh').touch()
        protected = self.scratch('protected')
        (protected / 'wallet.key').write_text('fixture')
        stamp = self.now - 49 * 3600
        for item in [*protected.rglob('*'), protected]:
            os.utime(item, (stamp, stamp))
        socket = self.scratch('tmux-fixture')
        operator = self.scratch('claude-fixture')
        link = self.chrono / 'link'
        link.symlink_to(old)
        self.sweep()
        self.assertFalse(old.exists())
        self.assertTrue(self.chrono.exists() and link.is_symlink())
        self.assertTrue(all(p.exists() for p in (boundary, young, recent, protected, socket, operator)))

    def test_live_reference_and_busy_process_retain_with_removal_control(self):
        live = self.scratch('live')
        busy = self.scratch('busy')
        old = self.scratch('control')
        self.registry['TASK-live'] = {'status': 'in-flight', 'scratch': str(live)}
        self.save_registry()
        with mock.patch.object(hygiene, 'idle_path', return_value=True), \
                mock.patch.object(hygiene, 'process_idle', side_effect=lambda p: p != busy):
            hygiene.sweep_test_tmp(self.root, self.now)
        self.assertTrue(live.exists() and busy.exists())
        self.assertFalse(old.exists())

    def test_symlink_pool_is_not_followed(self):
        self.chrono.rmdir()  # empty own fixture
        outside = self.root / 'outside'
        outside.mkdir()
        self.chrono.symlink_to(outside)
        with self.assertRaises(hygiene.Retained):
            self.sweep()
