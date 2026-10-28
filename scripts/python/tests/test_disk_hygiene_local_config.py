"""Private policy is opt-in; all mutations below target owned temporary fixtures."""

from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_hygiene as hygiene
import validate_skill_wiring as wiring


FIXTURE_CONFIG = {
    "tmp": {
        "cache_patterns": ["example-gocache.*", "example-gotmp.*", "example-*-npm-cache",
                           "example-*-uv-cache", "example-*-modcache"],
        "scratch_patterns": ["example-scratch-*"],
        "pinned_patterns": ["example-caddy-2.11.2-*", "example-age-*",
                            "TASK-*-tools", "TASK-*-venv", "TASK-*-age-*"],
        "protected_profile_substring": "private-example-profile",
    },
    "postgres": {"data_directory": "~/.local/share/example-test-pg", "database": "example_test",
                 "superuser": "fixture_admin"},
    "skill_collision": {"pair": ["example-release", "example-site-review"],
                        "reason": "Artifact publishing and website review have different triggers."},
}


def provision_config(root, config=None):
    path = root / hygiene.LOCAL_CONFIG_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(FIXTURE_CONFIG if config is None else config))
    return path


class LocalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="disk-local-config-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = time.time()
        self.pool = self.root / "tmp"
        self.pool.mkdir()
        self.state = self.root / "_state"
        (self.state / "board-dispatch").mkdir(parents=True)
        (self.state / "active-tasks.json").write_text('{}')
        patch = mock.patch.dict(os.environ, SQUAD_TEST_ISOLATION="1",
                                BOARD_TMP_ROOT=str(self.pool), BOARD_TEST_PG_ROOT=str(self.root / "pg"))
        patch.start()
        self.addCleanup(patch.stop)
        (self.root / "pg").mkdir()

    def old_tree(self, path):
        path.mkdir(parents=True)
        (path / "payload").write_text("disposable fixture")
        self.age(path)
        return path

    def age(self, path):
        stamp = self.now - 2 * hygiene.DAY
        for entry in [*path.rglob('*'), path]:
            os.utime(entry, (stamp, stamp))

    def assert_skipped(self):
        candidate = self.pool / "example-old-gocache"
        bulk = self.state / "rescued-worker-artifacts/TASK-done/d-0001/.cache"
        if not candidate.exists():
            self.old_tree(candidate)
            self.old_tree(bulk)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(hygiene.shutil, "rmtree") as remove, \
                mock.patch.object(hygiene.subprocess, "run") as run:
            hygiene.sweep_test_tmp(self.root, self.now)
            self.assertEqual(hygiene.sweep_rescued_bulk(self.root, self.now, apply=True), (0, 0))
            hygiene.sweep_test_databases(self.root, self.now)
            remove.assert_not_called()
            run.assert_not_called()
        self.assertTrue(candidate.exists() and bulk.exists())
        self.assertIn("SKIP", out.getvalue())
        self.assertIn("not connected", out.getvalue())

    def test_absent_invalid_and_unreadable_config_delete_nothing(self):
        self.assert_skipped()
        path = self.state / "disk-hygiene.local.json"
        for value in (b'{broken', b'[]', b'null', b'{}', b'\xff'):
            with self.subTest(value=value):
                path.write_bytes(value)
                self.assert_skipped()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("fixture denied")):
            self.assert_skipped()

    def test_missing_or_invalid_protections_never_mean_protect_nothing(self):
        for key, values in {
            "protected_profile_substring": [None, "", [], "../profile"],
            "pinned_patterns": [None, [], "example-*", ["*"]],
            "cache_patterns": [None, [], ["../*"]],
            "scratch_patterns": [None, [], [12]],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    config = copy.deepcopy(FIXTURE_CONFIG)
                    config['tmp'][key] = value
                    config.pop('postgres')
                    provision_config(self.root, config)
                    self.assert_skipped()
            config = copy.deepcopy(FIXTURE_CONFIG)
            del config['tmp'][key]
            config.pop('postgres')
            provision_config(self.root, config)
            self.assert_skipped()

    def test_present_policy_keeps_profile_and_pinned_subtrees_with_deletion_control(self):
        provision_config(self.root)
        control = self.old_tree(self.pool / 'example-gocache.control')
        profile = self.old_tree(self.pool / 'example-gocache.profile')
        nested = profile / FIXTURE_CONFIG['tmp']['protected_profile_substring']
        nested.mkdir()
        self.age(profile)
        pinned = self.old_tree(self.pool / 'example-age-tools')
        marker = pinned / '.vibesquad-scratch'
        marker.write_text('d-0001')
        self.age(pinned)
        bulk = self.old_tree(self.state / 'rescued-worker-artifacts/TASK-done/d-0001/.cache')
        (bulk / FIXTURE_CONFIG['tmp']['protected_profile_substring']).mkdir()
        self.age(bulk)
        with redirect_stdout(io.StringIO()), mock.patch.object(hygiene, 'idle_path', return_value=True), \
                mock.patch.object(hygiene, 'process_idle', return_value=True):
            hygiene.sweep_test_tmp(self.root, self.now)
            self.assertEqual(hygiene.sweep_rescued_bulk(self.root, self.now, apply=True), (0, 0))
        self.assertFalse(control.exists())
        self.assertTrue(profile.exists() and pinned.exists() and bulk.exists())

    def test_generic_patterns_and_protected_helpers_require_policy(self):
        self.assertFalse(hygiene.tmp_candidate(Path('generic-go-cache')))
        with self.assertRaises(hygiene.Retained):
            hygiene.protected_tmp_name('unprotected-name')
        path = self.old_tree(self.pool / 'generic-go-cache')
        with redirect_stdout(io.StringIO()), mock.patch.object(hygiene.shutil, 'rmtree') as remove:
            self.assertEqual(hygiene.remove_old(path, self.now, protect=True), (0, 0))
            remove.assert_not_called()
        provision_config(self.root)
        self.assertTrue(hygiene.tmp_candidate(path, hygiene.tmp_policy(self.root)))

    def test_generic_tool_pins_survive_without_local_policy(self):
        names = ('fixture-age-toolchain-go-cache', 'age', 'age-go-cache')
        kept = [self.old_tree(self.pool / name) for name in names]
        control = self.old_tree(self.pool / 'example-gocache.control')
        with redirect_stdout(io.StringIO()), mock.patch.object(hygiene.shutil, 'rmtree') as remove:
            hygiene.sweep_test_tmp(self.root, self.now)
            remove.assert_not_called()
        self.assertTrue(all(path.exists() for path in [*kept, control]))

        # Once cleanup is enabled, built-in pins still protect standing tools.
        provision_config(self.root)
        policy = hygiene.tmp_policy(self.root)
        for name in names:
            self.assertTrue(hygiene.protected_tmp_name(name, policy))
        self.assertFalse(hygiene.protected_tmp_name(control.name, policy))
        with redirect_stdout(io.StringIO()), mock.patch.object(hygiene, 'idle_path', return_value=True), \
                mock.patch.object(hygiene, 'process_idle', return_value=True):
            hygiene.sweep_test_tmp(self.root, self.now)
        self.assertTrue(all(path.exists() for path in kept))
        self.assertFalse(control.exists())

    def test_task_tool_pins_come_from_local_policy(self):
        config = copy.deepcopy(FIXTURE_CONFIG)
        config['tmp']['pinned_patterns'] = ['TASK-example-tools-*', 'TASK-example-age-*']
        provision_config(self.root, config)
        names = ('TASK-example-tools-go-cache', 'TASK-example-age-go-cache')
        kept = [self.old_tree(self.pool / name) for name in names]
        control = self.old_tree(self.pool / 'TASK-unpinned-go-cache')
        policy = hygiene.tmp_policy(self.root)
        without_task_pins = dict(policy, pinned_patterns=['unrelated-tool-*'])
        for name in names:
            self.assertFalse(hygiene.protected_tmp_name(name, without_task_pins))
            self.assertTrue(hygiene.protected_tmp_name(name, policy))
        with redirect_stdout(io.StringIO()), mock.patch.object(hygiene, 'idle_path', return_value=True), \
                mock.patch.object(hygiene, 'process_idle', return_value=True):
            hygiene.sweep_test_tmp(self.root, self.now)
        self.assertTrue(all(path.exists() for path in kept))
        self.assertFalse(control.exists())

    def test_invalid_database_identity_never_connects_even_with_environment_override(self):
        for field, values in {
            'database': ['', 'postgres', 'template0', 'template1', "x'; DROP DATABASE y;--", 'x.*', None],
            'data_directory': ['', '/', '/tmp', '../pg', '~/../pg', 12],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    config = copy.deepcopy(FIXTURE_CONFIG)
                    config['postgres'][field] = value
                    provision_config(self.root, config)
                    with redirect_stdout(io.StringIO()), mock.patch.object(hygiene.subprocess, 'run') as run:
                        hygiene.sweep_test_databases(self.root, self.now)
                        run.assert_not_called()

    def test_configured_database_family_is_exact_in_query_and_census(self):
        provision_config(self.root)
        pg = hygiene.TestPostgres(self.root)
        database = FIXTURE_CONFIG['postgres']['database']
        row = {'name': database + '_run1', 'oid': 100, 'bytes': 1, 'connections': 0}
        response = {'data_directory': str(self.root / 'pg'), 'port': '55432', 'databases': [row]}
        with mock.patch.object(pg, 'run', return_value=json.dumps(response)) as run:
            self.assertEqual(pg.snapshot(), [row])
        sql = run.call_args.args[2]
        self.assertIn("d.datname = '" + database + "'", sql)
        self.assertIn("'^" + database + "_[A-Za-z0-9_]+$'", sql)
        for name in ('other_test_run1', database + '_', database + '_x.y', database + 'extra'):
            row['name'] = name
            with mock.patch.object(pg, 'run', return_value=json.dumps(response)):
                with self.assertRaises(hygiene.Retained):
                    pg.snapshot()

    def test_optional_private_collision_pair_is_local_to_one_root(self):
        wired = {}
        for name in FIXTURE_CONFIG['skill_collision']['pair']:
            path = self.root / '.claude/skills' / name / 'SKILL.md'
            path.parent.mkdir(parents=True)
            path.write_text('---\nname: ' + name + '\ndescription: '
                            'Use to evaluate zebra lantern copper glacier meadow artifacts.\n---\n')
            wired[name] = path
        with redirect_stdout(io.StringIO()):
            self.assertTrue(wiring.check_trigger_collisions(wired, self.root)[0])
            provision_config(self.root)
            self.assertFalse(wiring.check_trigger_collisions(wired, self.root)[0])
            self.assertTrue(wiring.check_trigger_collisions(wired)[0])
            for pair in ([], ['only-one'], ['same', 'same'], ['bad/path', 'other'], [None, 'other']):
                config = copy.deepcopy(FIXTURE_CONFIG)
                config['skill_collision']['pair'] = pair
                provision_config(self.root, config)
                self.assertTrue(wiring.check_trigger_collisions(wired, self.root)[0])

    def test_optional_provisioned_temp_root(self):
        """The packet's two full-suite runs supply a disposable root with/without host policy."""
        supplied = os.environ.get('DISK_HYGIENE_PROVISIONED_ROOT')
        if not supplied:
            self.skipTest('external provisioning comparison not requested')
        root = Path(supplied).resolve()
        self.assertTrue(root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        config_path = root / hygiene.LOCAL_CONFIG_REL
        if not config_path.exists():
            self.assert_skipped()
            return
        config = json.loads(config_path.read_text())
        provision_config(self.root, config)
        with redirect_stdout(io.StringIO()):
            policy = hygiene.tmp_policy(self.root)
        self.assertIsNotNone(policy)
        candidates = [p.replace('*', 'fixture').replace('?', 'x') for p in
                      policy['cache_patterns'] + policy['scratch_patterns']]
        for name in candidates:
            self.old_tree(self.pool / name)
        kept = []
        for pattern in policy['pinned_patterns']:
            name = pattern.replace('*', 'fixture').replace('?', 'x')
            kept.append(self.old_tree(self.pool / name))
        profile = self.old_tree(self.pool / 'profile-go-cache')
        (profile / policy['protected_profile_substring']).mkdir()
        self.age(profile)
        kept.append(profile)
        with redirect_stdout(io.StringIO()), mock.patch.object(hygiene, 'idle_path', return_value=True), \
                mock.patch.object(hygiene, 'process_idle', return_value=True):
            hygiene.sweep_test_tmp(self.root, self.now)
        self.assertTrue(all(not (self.pool / name).exists() for name in candidates))
        self.assertTrue(all(path.exists() for path in kept))
        pg = hygiene.TestPostgres(self.root)
        self.assertEqual(pg.database, config['postgres']['database'])
        self.assertEqual(pg.data, self.root / 'pg')
        with mock.patch.dict(os.environ, SQUAD_TEST_ISOLATION='0'), \
                mock.patch.object(Path, 'home', return_value=self.root), \
                mock.patch.object(Path, 'lstat', return_value=(self.root / 'pg').stat()):
            os.environ.pop('BOARD_TEST_PG_ROOT')
            self.assertEqual(hygiene.test_pg_root(self.root),
                             Path(config['postgres']['data_directory']).expanduser())


if __name__ == '__main__':
    unittest.main()


def test_superuser_is_required_and_passed_without_os_user_fallback(tmp_path, monkeypatch):
    provision_config(tmp_path)
    data = tmp_path / 'pg'
    data.mkdir()
    monkeypatch.setenv('SQUAD_TEST_ISOLATION', '1')
    monkeypatch.setenv('BOARD_TEST_PG_ROOT', str(data))
    for value in (None, '', [], 'bad\nrole', '--user=other', 'x' * 64):
        config = copy.deepcopy(FIXTURE_CONFIG)
        config['postgres']['superuser'] = value
        provision_config(tmp_path, config)
        with mock.patch.object(hygiene.subprocess, 'run') as run:
            hygiene.sweep_test_databases(tmp_path, time.time())
            run.assert_not_called()
    config['postgres'].pop('superuser')
    provision_config(tmp_path, config)
    with mock.patch.object(hygiene.subprocess, 'run') as run:
        hygiene.sweep_test_databases(tmp_path, time.time())
        run.assert_not_called()
    provision_config(tmp_path)
    pg = hygiene.TestPostgres(tmp_path)
    with mock.patch.object(pg, 'executable', side_effect=lambda x: x):
        for tool in ('psql', 'dropdb'):
            assert '--username=fixture_admin' in pg.command(tool, 'example_test_run')


def test_safe_sweep_passes_root_to_child_from_unrelated_cwd(tmp_path, monkeypatch):
    provision_config(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv('VAULT_ROOT', str(tmp_path.parent))
    with mock.patch.object(hygiene.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
            mock.patch.object(hygiene, 'periodic_sweep', return_value=0) as periodic:
        assert hygiene.run_safe_sweep(tmp_path) == 0
    assert run.call_args.kwargs['cwd'] == tmp_path
    assert run.call_args.kwargs['env']['VAULT_ROOT'] == str(tmp_path)
    assert run.call_args.args[0][1] == str(tmp_path / 'bin/prune-board-worktrees.sh')
    periodic.assert_called_once_with(tmp_path)
    assert hygiene.tmp_policy(tmp_path) == FIXTURE_CONFIG['tmp']


def test_postgres_log_rotation_preserves_open_inode(tmp_path, monkeypatch):
    import gzip
    import subprocess
    repo = Path(__file__).resolve().parents[3]
    provision_config(tmp_path)
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/python').symlink_to(repo / 'scripts/python')
    data = tmp_path / 'pg'
    data.mkdir()
    log = data / 'launchd.stderr.log'
    content = b'fixture error\n' * 30
    log.write_bytes(content)
    env = dict(os.environ, VAULT_ROOT=str(tmp_path), SQUAD_TEST_ISOLATION='1',
               BOARD_TEST_PG_ROOT=str(data), VIBESQUAD_DAEMON_LOG_DIR=str(tmp_path / 'daemon'),
               VIBESQUAD_TMUX_LOG_DIR=str(tmp_path / 'tmux'), VIBESQUAD_DAEMON_LOG_CAP='100',
               PYTHONDONTWRITEBYTECODE='1')
    command = ['bash', str(repo / 'bin/rotate-logs.sh')]
    subprocess.run(command, env=env, capture_output=True, check=True)
    assert log.read_bytes() == content
    inode = log.stat().st_ino
    with log.open('ab') as writer:
        result = subprocess.run([*command, '--apply'], env=env, capture_output=True, text=True, check=True)
        assert log.stat().st_ino == inode and log.stat().st_size == 0, result.stdout + result.stderr
        writer.write(b'after rotation\n')
    assert log.read_bytes() == b'after rotation\n'
    archives = list(data.glob('launchd.stderr.log.*.gz'))
    assert len(archives) == 1
    assert gzip.decompress(archives[0].read_bytes()) == content
    # A missing superuser or a linked log must not truncate a different file.
    config = copy.deepcopy(FIXTURE_CONFIG)
    del config['postgres']['superuser']
    provision_config(tmp_path, config)
    log.write_bytes(content)
    subprocess.run([*command, '--apply'], env=env, capture_output=True, check=True)
    assert log.read_bytes() == content
    provision_config(tmp_path)
    log.unlink()  # own fixture only
    target = data / 'other'
    target.write_bytes(content)
    log.symlink_to(target)
    subprocess.run([*command, '--apply'], env=env, capture_output=True, check=True)
    assert target.read_bytes() == content
