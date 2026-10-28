"""Durability and recipient acknowledgment contract for the notice store."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def produce(root, index):
    from chrono_state.notifications import NotificationStore
    store = NotificationStore(Path(root))
    store.enqueue_task('TASK-concurrent', 1, 'complete', 'done')
    store.enqueue_resource('disk', str(index), 'low')


class NotificationStoreTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('chrono_state.notifications')
        except ImportError:
            self.fail('durable notice store is absent in the unfixed code')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.module.NotificationStore(self.root)
        self.store.initialize('2000-01-01T00:00:00+00:00')

    def notice(self, generation=1):
        return self.store.enqueue_task('TASK-one', generation, 'complete', 'done')

    def test_non_object_and_malformed_records_raise_value_error(self):
        valid = json.loads(self.store.path.read_text())
        payloads = [[], None, "text", 7, True,
                    {"schema": self.module.SCHEMA, "notices": {}},
                    {**valid, "cutoff": []}, {**valid, "notices": []},
                    *({**valid, "notices": {"bad": value}} for value in
                      ([], None, "text", 7, {"id": "bad", "state": []},
                       {"id": "bad", "state": "pending", "enqueued_at": []}))]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.store.path.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    self.store.list_pending()
        self.store.path.write_text(json.dumps(valid))
        self.assertEqual(self.notice()["state"], "pending")

    def test_initialize_existing_store_does_not_parse(self):
        with mock.patch.object(self.store, "_read", side_effect=AssertionError("parsed")):
            self.store.initialize()
        self.assertEqual(self.notice()["state"], "pending")

    def test_store_byte_limit_preserves_file_on_read_and_write_failure(self):
        before = self.store.path.read_bytes()
        with mock.patch.object(self.module, "MAX_STORE_BYTES", len(before), create=True):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                self.notice()
            self.assertEqual(self.store.path.read_bytes(), before)
            self.store.path.write_bytes(before + b" ")
            with mock.patch.object(self.module.json, "loads", side_effect=AssertionError("parsed oversized data")):
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    self.store.list_pending()
        self.store.path.write_bytes(before)
        self.assertEqual(self.notice()["state"], "pending")

    def test_ack_retention_compacts_payload_without_resurrecting_replay(self):
        with mock.patch.object(self.module, "utc_now", return_value="2001-01-01T00:00:00+00:00"):
            old = self.notice()
            self.store.acknowledge([old["id"]])
        fresh = self.notice(2)
        self.store.acknowledge([fresh["id"]])
        pending = self.notice(3)
        tombstone = self.store.show(old["id"])
        self.assertEqual(set(tombstone), {"id", "state", "acknowledged_at"})
        self.assertEqual(self.notice()["state"], "acknowledged")
        self.assertIn("summary", self.store.show(fresh["id"]))
        self.assertEqual([n["id"] for n in self.store.list_pending()], [pending["id"]])

    def test_concurrent_producers_deduplicate_without_losing_distinct_events(self):
        script = "from test_notification_store import produce; import sys; produce(sys.argv[1], int(sys.argv[2]))"
        search_path = os.pathsep.join((str(Path(__file__).resolve().parents[1]),
                                      str(Path(__file__).resolve().parent)))
        workers = [subprocess.Popen([sys.executable, '-c', script, str(self.root), str(i)],
                                   env={**os.environ, 'PYTHONPATH': search_path},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for i in range(6)]
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=15)
            self.assertEqual(worker.returncode, 0, stdout + stderr)
        self.assertEqual(len(self.store.list_pending()), 7)

    def test_new_generation_and_resource_episode_are_distinct(self):
        one, two = self.notice(), self.notice(2)
        self.assertNotEqual(one['id'], two['id'])
        self.assertEqual(self.notice()['id'], one['id'])
        a = self.store.enqueue_resource('disk', 'episode-1', 'low')
        b = self.store.enqueue_resource('disk', 'episode-2', 'low')
        self.assertNotEqual(a['id'], b['id'])

    def test_crash_before_atomic_enqueue_leaves_no_partial_notice(self):
        with mock.patch.object(self.module.os, 'replace', side_effect=OSError('crash before rename')):
            with self.assertRaises(OSError):
                self.notice()
        self.assertEqual(self.store.list_pending(), [])
        self.assertEqual(self.notice()['state'], 'pending')

    def test_crash_after_enqueue_replays_exact_id_without_acknowledgment(self):
        script = "from pathlib import Path; from chrono_state.notifications import NotificationStore; import os,sys; NotificationStore(Path(sys.argv[1])).enqueue_task('TASK-one',1,'complete','done'); os._exit(17)"
        run = subprocess.run([sys.executable, '-c', script, str(self.root)], env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
        self.assertEqual(run.returncode, 17)
        original = self.store.list_pending()[0]
        self.assertEqual(self.notice()['id'], original['id'])
        self.assertEqual(self.store.show(original['id'])['state'], 'pending')

    def test_missing_store_is_explicit_and_unwritable_store_fails(self):
        missing = self.module.NotificationStore(self.root / 'missing')
        with self.assertRaises(FileNotFoundError):
            missing.list_pending()
        with mock.patch.object(self.module.os, 'replace', side_effect=PermissionError('unwritable')):
            with self.assertRaises(PermissionError):
                self.notice()
        self.assertEqual(self.store.list_pending(), [])

    def test_ack_unknown_is_atomic_and_duplicate_ids_are_idempotent(self):
        notice = self.notice()
        with self.assertRaises(KeyError):
            self.store.acknowledge([notice['id'], 'unknown'])
        self.assertEqual(self.store.show(notice['id'])['state'], 'pending')
        self.store.acknowledge([notice['id'], notice['id']])
        self.store.acknowledge([notice['id']])
        self.assertEqual(self.store.show(notice['id'])['state'], 'acknowledged')
        self.assertEqual(self.store.list_pending(), [])

    def test_offers_require_process_start_identity_and_never_ack(self):
        notice = self.notice()
        with mock.patch.object(self.module, 'process_start_identity', return_value='start-A'):
            with self.assertRaises(ValueError):
                self.store.offer([notice['id']], 123, 'start-B')
            self.store.offer([notice['id']], 123, 'start-A', ttl=60)
            self.assertEqual(self.store.show(notice['id'])['state'], 'offered')
            self.assertEqual(len(self.store.list_pending()), 1)
            with self.assertRaises(ValueError):
                self.store.offer([notice['id']], 456, 'start-A')
        with mock.patch.object(self.module, 'process_start_identity', side_effect=lambda pid: 'start-B' if pid == 123 else 'start-C'):
            self.store.offer([notice['id']], 456, 'start-C')
        self.assertEqual(self.store.show(notice['id'])['lease']['pid'], 456)
        self.store.acknowledge([notice['id']])
        self.assertEqual(self.store.list_pending(), [])

    def test_expired_lease_and_attention_remain_pending_until_exact_ack(self):
        notice = self.notice()
        with mock.patch.object(self.module, 'process_start_identity', return_value='start'), mock.patch.object(self.module.time, 'time', return_value=100):
            self.store.offer([notice['id']], 123, 'start', ttl=1)
        with mock.patch.object(self.module, 'process_start_identity', return_value='new'), mock.patch.object(self.module.time, 'time', return_value=102):
            self.store.offer([notice['id']], 456, 'new')
        self.store.require_attention([notice['id']], 'consumer unavailable')
        self.assertEqual(self.store.list_pending()[0]['state'], 'operator-attention-required')
        self.store.acknowledge([notice['id']])
        self.assertEqual(self.store.list_pending(), [])

    def test_cutoff_starts_empty_and_is_immutable(self):
        store = self.module.NotificationStore(self.root / 'cutoff')
        store.initialize('2026-09-27T12:00:00+00:00')
        store.initialize('2000-01-01T00:00:00+00:00')
        self.assertIsNone(store.enqueue_task('TASK-old', 1, 'complete', 'old', occurred_at='2026-09-26T12:00:00+00:00'))
        self.assertEqual(store.list_pending(), [])
        self.assertIsNotNone(store.enqueue_task('TASK-new', 1, 'complete', 'new', occurred_at='2026-09-27T12:00:01+00:00'))

    def test_bounded_read_and_cli_exact_ack(self):
        for generation in range(1, 5):
            self.notice(generation)
        self.assertEqual(len(self.store.list_pending(limit=2)), 2)
        for limit in (0, -1, 1001):
            with self.assertRaises(ValueError):
                self.store.list_pending(limit)
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        command = [sys.executable, '-m', 'chrono_state.notifications', '--vault', str(self.root)]
        listed = subprocess.run(command + ['list', '--limit', '1'], env=env, text=True, capture_output=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        notice = json.loads(listed.stdout)[0]
        ack = subprocess.run(command + ['ack', notice['id']], env=env, text=True, capture_output=True)
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertEqual(len(self.store.list_pending()), 3)


if __name__ == '__main__':
    unittest.main()
