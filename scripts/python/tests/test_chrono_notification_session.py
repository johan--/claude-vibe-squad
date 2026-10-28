"""Hook protocol and lifecycle tests with explicit simulated process identities.

These tests do not claim installed-Claude idle wake or editor-draft coverage.
"""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import io
import json
import linecache
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from chrono_state import notifications as n
from chrono_state import resource_watch as r

REPO = Path(__file__).resolve().parents[3]
HOOK = REPO / 'bin/chrono-notification-hook.sh'

# Reproducible unfixed-code controls without checking out or editing main.
# Set this only to the pinned baseline commit when running the named regressions.
CONTROL_REF = os.environ.get('CHRONO_NOTIFICATION_TEST_BASELINE')
if CONTROL_REF:
    for module in (n, r):
        path = Path(module.__file__).relative_to(REPO)
        source = subprocess.check_output(['git', '-C', str(REPO), 'show', f'{CONTROL_REF}:{path}'])
        filename = f'{CONTROL_REF}:{path}'
        linecache.cache[filename] = (len(source), None, source.decode().splitlines(keepends=True), filename)
        exec(compile(source, filename, 'exec'), module.__dict__)


class ProcessIdentity(unittest.TestCase):
    def test_darwin_start_identity_normalizes_spaces_and_excludes_zombies(self):
        start = 'Sun Sep  6 09:23:31 2026'
        with mock.patch.object(n.sys, 'platform', 'darwin'), \
                mock.patch.object(n.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0, stdout=start + ' S+\n', stderr='')
            self.assertEqual(n.process_start_identity(101), 'Sun Sep 6 09:23:31 2026')
            run.return_value = mock.Mock(returncode=0, stdout=start + ' Z\n', stderr='')
            self.assertIsNone(n.process_start_identity(101))
            run.return_value = mock.Mock(returncode=1, stdout='', stderr='')
            self.assertIsNone(n.process_start_identity(101))
            run.return_value = mock.Mock(returncode=1, stdout='', stderr='permission denied')
            with self.assertRaises(OSError):
                n.process_start_identity(101)

    def test_linux_start_ticks_and_zombie_control(self):
        with mock.patch.object(n.sys, 'platform', 'linux'), \
                mock.patch.object(n.Path, 'read_text', side_effect=[
                    '101 (a process) S ' + ' '.join(['0'] * 18) + ' 9876', 'boot-id']):
            self.assertEqual(n.process_start_identity(101), 'boot-id:9876')
        with mock.patch.object(n.sys, 'platform', 'linux'), \
                mock.patch.object(n.Path, 'read_text', return_value='101 (a process) Z'):
            self.assertIsNone(n.process_start_identity(101))


class SessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='notice-session-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.flag = self.root / '_state/chrono/notification-hooks.enabled'
        self.flag.parent.mkdir(parents=True)
        self.flag.touch()
        self.identities = {101: 'owner-start', os.getpid(): 'test-process-start', 202: 'sampler-start'}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(n, 'process_start_identity',
                                                  side_effect=lambda pid: self.identities.get(pid)))
        self.session = n.NotificationSession(self.root, 'claude-session', 101, 'owner-start')
        self.session.poll = 0.01
        self.assertTrue(self.session.open('SessionStart'))
        self.store = self.session.store

    def notice(self, task='TASK-local', generation=1):
        return self.store.enqueue_task(task, generation, 'complete', 'completed fixture')

    def record(self):
        return json.loads(self.store.path.read_text())['sessions'][self.session.key]

    def test_duplicate_waiters_pid_reuse_and_expired_live_lease(self):
        first = self.session.claim_waiter()
        self.assertIsNotNone(first)
        self.assertIsNone(self.session.claim_waiter())
        with self.store._lock():
            data = self.store._read()
            data['sessions'][self.session.key]['workers']['waiter']['expires_at'] = 0
            self.store._write(data)
        # Expiry alone cannot create overlapping workers.
        self.assertIsNone(self.session.claim_waiter())
        self.identities[os.getpid()] = 'reused-pid-start'
        # PID reuse means a new process/object, never a changed identity of self.
        restarted = n.NotificationSession(self.root, 'claude-session', 101, 'owner-start')
        replacement = restarted.claim_waiter()
        self.assertNotEqual(first, replacement)
        self.session.release('waiter', first, 'late-old-exit')
        self.assertEqual(self.record()['workers']['waiter']['token'], replacement)

    def test_atomic_concurrent_arm_and_offer(self):
        self.notice()
        with ThreadPoolExecutor(max_workers=6) as pool:
            tokens = list(pool.map(lambda _: self.session.claim_waiter(), range(6)))
            offers = list(pool.map(lambda _: self.session.offer(), range(6)))
        self.assertEqual(sum(token is not None for token in tokens), 1)
        self.assertEqual(sum(len(batch) for batch in offers), 1)

    def test_waiter_arms_before_inspection_and_does_not_ack(self):
        notice = self.notice()
        original = self.session.offer

        def inspect_after_arm():
            self.assertTrue(self.record()['workers']['waiter']['token'])
            return original()

        with mock.patch.object(self.session, 'ensure_sampler'), \
                mock.patch.object(self.session, 'offer', side_effect=inspect_after_arm):
            code, reminder = self.session.wait()
        self.assertEqual(code, 2)
        self.assertIn('not operator input, an answer, or consent', reminder)
        self.assertIn(notice['id'], reminder)
        self.assertEqual(self.store.show(notice['id'])['state'], 'offered')
        self.assertEqual(self.record()['last_waiter_exit']['reason'], 'notice')
        self.assertNotIn('waiter', self.record()['workers'])

    def test_rearm_after_each_wake_and_renewal_boundary(self):
        self.session.lifetime = 0.03  # Accelerated boundary; not a 31/61-minute canary.
        with mock.patch.object(self.session, 'ensure_sampler'):
            for i in range(3):
                notice = self.notice(f'TASK-wake-{i}')
                code, reminder = self.session.wait()
                self.assertEqual(code, 2)
                self.assertIn(notice['id'], reminder)
                self.store.acknowledge([notice['id']])
            code, reminder = self.session.wait()
            self.assertEqual(code, 2)
            self.assertIn('lifetime renewal', reminder)
            after = self.notice('TASK-after-renewal')
            code, reminder = self.session.wait()
            self.assertEqual(code, 2)
            self.assertIn(after['id'], reminder)

    def test_waiter_notices_event_enqueued_after_arm(self):
        with mock.patch.object(self.session, 'ensure_sampler'):
            results = []
            thread = threading.Thread(target=lambda: results.append(self.session.wait()))
            thread.start()
            try:
                deadline = time.monotonic() + 2
                while 'waiter' not in self.record()['workers'] and time.monotonic() < deadline:
                    time.sleep(0.01)
                notice = self.notice('TASK-between-polls')
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertIn(notice['id'], results[0][1])
            finally:
                self.session.close()
                thread.join(timeout=2)

    def test_offers_retry_after_ttl_or_recipient_crash_but_ack_is_terminal(self):
        notice = self.notice()
        self.assertEqual(len(self.session.offer()), 1)
        self.assertEqual(self.session.offer(), [])
        with mock.patch.object(n.time, 'time', return_value=time.time() + 61):
            self.assertEqual(len(self.session.offer()), 1)
        self.identities[101] = 'new-owner-start'
        replacement = n.NotificationSession(self.root, 'new-session', 101, 'new-owner-start')
        replacement.open('SessionStart')
        self.assertEqual(len(replacement.offer()), 1)
        self.store.acknowledge([notice['id']])
        self.assertEqual(replacement.offer(), [])
        self.assertEqual(self.notice()['state'], 'acknowledged')
        self.assertNotEqual(self.notice(generation=2)['id'], notice['id'])
        self.assertEqual(n.session_health(self.store)['pending'], 1)
        self.assertIsNotNone(n.session_health(self.store)['max_ack_latency_seconds'])

    def test_context_stop_recursion_and_held_attention(self):
        self.session.claim_waiter()
        notice = self.notice()
        self.store.require_attention([notice['id']], 'operator decision held')
        before = self.store.path.read_bytes()
        self.assertIsNone(self.session.context('Stop', True))
        self.assertEqual(self.store.path.read_bytes(), before)
        output = self.session.context('Stop')
        self.assertEqual(output['decision'], 'block')
        self.assertIn('does not approve a held decision', output['reason'])
        self.assertEqual(self.store.show(notice['id'])['state'], 'operator-attention-required')
        self.assertIsNone(self.session.context('Stop'))

    def test_busy_prompt_tool_context_and_bounded_untrusted_summary(self):
        self.session.claim_waiter()
        for event in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
            notice = self.store.enqueue_task('TASK-' + event, 1, 'complete', '\x1b\n' + 'x' * 8000)
            output = self.session.context(event)
            context = output['hookSpecificOutput']
            self.assertEqual(context['hookEventName'], event)
            self.assertIn(notice['id'], context['additionalContext'])
            self.assertLess(len(context['additionalContext']), 2500)
            self.assertNotIn('\x1b', context['additionalContext'])
            self.assertNotIn('permissionDecision', context)
            self.store.acknowledge([notice['id']])

    def test_eight_notice_batch_leaves_rest_pending(self):
        for i in range(12):
            self.notice('TASK-' + str(i))
        first = self.session.offer()
        self.assertEqual(len(first), 8)
        self.assertEqual(len(self.session.offer()), 4)
        self.assertEqual(len(self.store.list_pending()), 12)

    def test_session_end_closes_only_matching_session_without_signals(self):
        other = n.NotificationSession(self.root, 'another', 101, 'owner-start')
        other.open('UserPromptSubmit')
        self.session.claim_waiter()
        with mock.patch.object(n.os, 'kill') as kill:
            self.session.close()
        kill.assert_not_called()
        self.assertFalse(self.session.active())
        self.assertTrue(other.active())
        self.assertFalse(self.session.open('Stop'))
        self.assertTrue(self.session.open('SessionStart', source='resume'))
        self.assertTrue(self.session.active())
        self.assertFalse(other.active())

    def test_clear_retires_old_session_resume_is_idempotent_and_owner_death_stops(self):
        self.assertTrue(self.session.open('SessionStart'))
        other = n.NotificationSession(self.root, 'cleared', 101, 'owner-start')
        other.open('SessionStart', source='clear')
        self.assertFalse(self.session.active())
        self.assertTrue(other.active())
        self.identities.pop(101)
        with mock.patch.object(n.time, 'monotonic', return_value=time.monotonic() + 6):
            self.assertFalse(other.active())

    def test_nested_startup_does_not_retire_coordinator(self):
        self.identities[303] = 'nested-start'
        other = n.NotificationSession(self.root, 'nested', 303, 'nested-start')
        other.open('SessionStart')
        self.assertTrue(self.session.active())
        self.assertTrue(self.session.open('PreToolUse'))
        for source in ('startup', 'resume', 'compact'):
            other.open('SessionStart', source=source)
            self.assertTrue(self.session.active())

    def test_resume_retires_same_owner_sampler_but_preserves_other_owners(self):
        self.identities[303] = 'other-start'
        foreign = n.NotificationSession(self.root, 'foreign', 303, 'other-start')
        foreign.open('SessionStart', source='startup')
        replacement = n.NotificationSession(self.root, 'resumed', 101, 'owner-start')
        with mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=os.getpid())):
            self.session.ensure_sampler()
        token = self.record()['workers']['sampler']['token']
        samples = []

        def resume_after_sample(_):
            samples.append(True)
            replacement.open('SessionStart', source='resume')
            # Bound the unfixed control without leaving a runaway sampler.
            if len(samples) > 1:
                self.session.close()

        with mock.patch.object(r, 'sample_resources', return_value={
                'disk_free_bytes': 100 * r.GIB, 'memory_pressure': 'normal'}), \
                mock.patch.object(r.time, 'sleep', side_effect=resume_after_sample):
            self.assertEqual(r.run_sampler(self.root, self.session.key, token), 0)
        self.assertEqual(len(samples), 1, 'old sampler survived same-process resume')
        self.assertFalse(self.session.active())
        self.assertNotIn('sampler', self.record()['workers'])
        self.assertTrue(foreign.active())
        self.assertTrue(replacement.active())
        self.assertTrue(replacement.open('SessionStart', source='compact'))
        for event in ('PreToolUse', 'Stop'):
            self.assertFalse(self.session.open(event))
            self.assertFalse(self.session.active())
            self.assertTrue(replacement.active(), 'late old hook retired the replacement')
        self.assertTrue(self.session.open('SessionStart', source='resume'))
        self.assertTrue(self.session.active())
        self.assertFalse(replacement.active(), 'resume-back left the abandoned session active')
        self.assertTrue(foreign.active())

    def assert_resume_back_delivers(self, source):
        # Review R1 reproduction: A startup -> B resume/clear -> A resume-back.
        replacement = n.NotificationSession(self.root, 'replacement-' + source, 101, 'owner-start')
        self.assertTrue(replacement.open('SessionStart', source=source))
        self.assertFalse(self.session.active())
        self.assertTrue(replacement.active())  # A fresh key works even before the fix.
        for event in ('PreToolUse', 'Stop'):
            self.assertFalse(self.session.open(event))
            self.assertTrue(replacement.active())
        # Each hook normally constructs a fresh object with the original ID.
        resumed = n.NotificationSession(self.root, 'claude-session', 101, 'owner-start')
        self.assertTrue(resumed.open('SessionStart', source='resume'))
        self.assertTrue(resumed.active())
        self.assertFalse(replacement.active())
        self.assertTrue(resumed.open('PreToolUse'))
        notice = self.notice('TASK-resume-back-after-' + source)
        with mock.patch.object(resumed, 'ensure_sampler'):
            code, reminder = resumed.wait()
        self.assertEqual(code, 2)
        self.assertIn(notice['id'], reminder)
        offered = self.store.show(notice['id'])
        self.assertEqual(offered['state'], 'offered')
        self.assertEqual(offered['lease']['session'], resumed.key)
        self.store.acknowledge([notice['id']])
        self.assertEqual(self.store.show(notice['id'])['state'], 'acknowledged')

    def test_resume_back_delivers_after_same_process_resume(self):
        self.assert_resume_back_delivers('resume')

    def test_resume_back_delivers_after_same_process_clear(self):
        self.assert_resume_back_delivers('clear')

    def test_resume_back_preserves_workers_until_identity_checked(self):
        waiter = self.session.claim_waiter()
        with mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=202)) as spawn:
            self.session.ensure_sampler()
            workers = self.record()['workers']
            replacement = n.NotificationSession(self.root, 'resumed', 101, 'owner-start')
            replacement.open('SessionStart', source='resume')
            self.assertTrue(self.session.open('SessionStart', source='resume'))
            self.assertEqual(self.record()['workers'], workers)
            self.assertIsNone(self.session.claim_waiter(), 'duplicated a still-live waiter')
            self.session.ensure_sampler()
            spawn.assert_called_once()
        self.identities[os.getpid()] = 'restarted-waiter'
        resumed = n.NotificationSession(self.root, 'claude-session', 101, 'owner-start')
        new_waiter = resumed.claim_waiter()
        self.assertIsNotNone(new_waiter)
        self.assertNotEqual(new_waiter, waiter, 'dead lease prevented a replacement waiter')
        self.session.release('waiter', waiter, 'late-old-exit')
        self.assertEqual(self.record()['workers']['waiter']['token'], new_waiter)

    def test_transient_sampler_failure_does_not_repeat_context_warning(self):
        self.session.claim_waiter()
        with mock.patch.object(n.subprocess, 'Popen', side_effect=OSError('offline')):
            self.session.ensure_sampler()
        self.assertEqual(n.session_health(self.store)['sessions'][0]['sampler_failures'], 1)
        for event in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure'):
            self.assertIsNone(self.session.context(event), 'transient failure added tool context')
        for _ in range(n.SAMPLER_FAULT_NOTICE_AFTER - 1):
            retry = self.record()['sampler_retry_at']
            with mock.patch.object(n.time, 'time', return_value=retry), \
                    mock.patch.object(n.subprocess, 'Popen', side_effect=OSError('offline')):
                self.session.ensure_sampler()
        # Even after the durable warning is acknowledged, a persistent outage
        # remains visible until a successful sampler heartbeat clears it.
        self.store.acknowledge([notice['id'] for notice in self.store.list_pending()])
        warning = self.session.context('PreToolUse')['hookSpecificOutput']['additionalContext']
        self.assertIn('RESOURCE MONITORING OFF', warning)

    def test_sampler_fault_backoff_survives_hooks_and_bounds_ten_minute_spawns(self):
        for failure in ('spawn', 'import', 'config'):
            with self.subTest(failure=failure):
                session = n.NotificationSession(self.root, failure, 101, 'owner-start')
                session.open('SessionStart')
                elapsed = [0]
                wall = int(time.time())
                spawns = []

                def spawn(*args, **kwargs):
                    spawns.append(elapsed[0])
                    if failure == 'spawn':
                        raise OSError('spawn unavailable')
                    return mock.Mock(pid=os.getpid(), poll=mock.Mock(return_value=1))

                with mock.patch.object(n.time, 'time', side_effect=lambda: wall + elapsed[0]), \
                        mock.patch.object(n.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                        mock.patch.object(n, 'utc_now', side_effect=lambda: n.datetime.fromtimestamp(
                            wall + elapsed[0], n.timezone.utc).isoformat()), \
                        mock.patch.object(n.subprocess, 'Popen', side_effect=spawn), \
                        mock.patch.dict(os.environ, {'CHRONO_NOTIFICATION_DISK_WARN_GIB': '10',
                                                     'CHRONO_NOTIFICATION_DISK_CRITICAL_GIB': '20'}):
                    for second in range(0, 601, 5):
                        elapsed[0] = second
                        # A fresh hook cannot reset the persisted retry budget.
                        session = n.NotificationSession(self.root, failure, 101, 'owner-start')
                        session.maintain_sampler()
                        record = session.store.snapshot()['sessions'][session.key]
                        worker = record['workers'].get('sampler')
                        if worker and failure == 'config':
                            with self.assertRaisesRegex(ValueError, 'invalid resource'):
                                r.run_sampler(self.root, session.key, worker['token'])
                        elif worker and failure == 'import':
                            # The process died without writing its release record.
                            with session.store._lock():
                                data = session.store._read()
                                data['sessions'][session.key]['workers']['sampler']['process_start'] = 'dead'
                                session.store._write(data)
                    self.assertLessEqual(len(spawns), 3, f'first starts: {spawns[:10]}')
                    self.assertEqual(len(spawns), 3, 'positive control: no retry path exercised')
                    record = session.store.snapshot()['sessions'][session.key]
                    self.assertEqual(record['sampler_failures'], 3)
                    self.assertGreater(record['sampler_retry_at'], wall + 600)
                    faults = [x for x in session.store.list_pending()
                              if x['identity'][:2] == ['sampler', session.key]]
                    self.assertEqual(len(faults), 1)
                    self.assertIn('RESOURCE MONITORING OFF', faults[0]['summary'])
                    # ACK is receipt only: context keeps monitoring-off visible.
                    session.store.acknowledge([faults[0]['id']])
                    session.claim_waiter()
                    context = session.context('PreToolUse')['hookSpecificOutput']['additionalContext']
                    self.assertIn('RESOURCE MONITORING OFF', context)
                    task = self.notice('TASK-during-' + failure)
                    code, message = session.wait()
                    # The existing ARM belongs to this fixture; release it and
                    # exercise the waiter again if singleton exclusion fired.
                    if code == 0:
                        token = session.store.snapshot()['sessions'][session.key]['workers']['waiter']['token']
                        session.release('waiter', token, 'fixture')
                        code, message = session.wait()
                    self.assertEqual(code, 2)
                    self.assertIn(task['id'], message)
                    elapsed[0] = int(record['sampler_retry_at'] - wall) + 1
                    session.maintain_sampler()
                    worker = session.store.snapshot()['sessions'][session.key]['workers'].get('sampler')
                    if worker:
                        session.release('sampler', worker['token'], 'fault: still failing')
                    # Same outage never creates a second obligation, even after ACK.
                    all_notices = session.store.snapshot()['notices'].values()
                    self.assertEqual(sum(x.get('identity', [])[:2] == ['sampler', session.key]
                                         for x in all_notices), 1)

    def test_sampler_backoff_resets_only_after_a_committed_sample(self):
        with mock.patch.object(n.subprocess, 'Popen', side_effect=OSError('offline')):
            self.session.maintain_sampler()
        self.assertEqual(self.record().get('sampler_failures'), 1)
        retry = self.record()['sampler_retry_at']
        with mock.patch.object(n.time, 'time', return_value=retry), \
                mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=os.getpid())):
            self.session.ensure_sampler()
        self.assertEqual(self.record()['sampler_failures'], 1, 'spawn is not recovery')
        token = self.record()['workers']['sampler']['token']
        with mock.patch.object(r, 'sample_resources', return_value={
                'disk_free_bytes': 100 * r.GIB, 'memory_pressure': 'normal'}), \
                mock.patch.object(r.time, 'sleep', side_effect=lambda _: self.session.close()):
            r.run_sampler(self.root, self.session.key, token)
        self.assertEqual(self.record().get('sampler_failures', 0), 0)
        self.assertNotIn('sampler_retry_at', self.record())
        self.assertIn('last_sample_at', self.record())

    def test_sampler_independent_of_waiter_singleton_and_restart(self):
        with mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=202)) as spawn:
            self.session.ensure_sampler()
            self.session.ensure_sampler()
            spawn.assert_called_once()
            sampler = self.record()['workers']['sampler']
            self.notice()
            self.session.wait()
            self.assertEqual(self.record()['workers']['sampler'], sampler)
            self.identities[202] = 'reused-sampler-pid'
            with mock.patch.object(n.time, 'monotonic', return_value=time.monotonic() + 6):
                self.session.ensure_sampler()
                self.assertEqual(spawn.call_count, 1)
            with mock.patch.object(n.time, 'time', return_value=time.time() + 6), \
                    mock.patch.object(n.time, 'monotonic', return_value=time.monotonic() + 12):
                self.session.ensure_sampler()
            self.assertEqual(spawn.call_count, 2)
            self.assertNotEqual(self.record()['workers']['sampler']['token'], sampler['token'])

    def test_sampler_cancel_token_cannot_write_for_another_worker(self):
        with mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=202)):
            self.session.ensure_sampler()
        with mock.patch.object(r, 'sample_resources') as sample:
            r.run_sampler(self.root, self.session.key, 'wrong-token')
        sample.assert_not_called()
        self.assertIn('sampler', self.record()['workers'])

    def test_sampler_positive_control_samples_then_cooperatively_stops(self):
        with mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=os.getpid())):
            self.session.ensure_sampler()
        token = self.record()['workers']['sampler']['token']
        with mock.patch.object(r, 'sample_resources', return_value={
                'disk_free_bytes': 14 * r.GIB, 'memory_pressure': 'critical'}), \
                mock.patch.object(r.time, 'sleep', side_effect=lambda _: self.session.close()):
            self.assertEqual(r.run_sampler(self.root, self.session.key, token), 0)
        self.assertEqual(len(self.store.list_pending()), 2)
        self.assertTrue(self.record()['last_sample_at'])
        self.assertNotIn('sampler', self.record()['workers'])

    def test_health_reports_missed_delivery_and_stale_sampler(self):
        self.notice()
        with mock.patch.object(n.time, 'time', return_value=time.time() + 126):
            health = n.session_health(self.store)
        self.assertEqual(health['delivery_health'], 'degraded')
        self.assertGreater(health['oldest_pending_seconds'], 5)
        self.assertEqual(health['sessions'][0]['workers']['sampler'], 'degraded')
        self.assertEqual(health['consumer_health'], 'degraded')

    def test_owned_sampler_is_polled_and_reaped(self):
        child = mock.Mock(pid=202)
        child.poll.return_value = None
        with mock.patch.object(n.subprocess, 'Popen', return_value=child):
            self.session.ensure_sampler()
            self.session.ensure_sampler()
            child.poll.assert_called_once_with()
            self.assertEqual(self.session.children, [child])
            child.poll.return_value = 0
            self.session.ensure_sampler()
            self.assertEqual(self.session.children, [])

    def test_removed_flag_cancels_without_consuming_pending(self):
        results = []
        with mock.patch.object(self.session, 'ensure_sampler'):
            thread = threading.Thread(target=lambda: results.append(self.session.wait()))
            thread.start()
            try:
                deadline = time.monotonic() + 2
                while 'waiter' not in self.record()['workers'] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.flag.rename(self.flag.with_suffix('.disabled'))
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [(0, '')])
            finally:
                self.session.close()
                thread.join(timeout=2)
        notice = self.notice()
        self.assertFalse(self.session.active())
        self.assertEqual(self.store.show(notice['id'])['state'], 'pending')

    def test_failed_offer_commit_retains_pending_obligation(self):
        notice = self.notice()
        with mock.patch.object(self.store, '_write', side_effect=OSError('full')):
            with self.assertRaises(OSError):
                self.session.offer()
        self.assertEqual(self.store.show(notice['id'])['state'], 'pending')
        self.assertEqual(len(self.session.offer()), 1)

    def test_unavailable_identity_fails_closed_and_health_reports_unknown(self):
        self.session.claim_waiter()
        with mock.patch.object(n, 'process_start_identity', side_effect=PermissionError('ps denied')):
            with self.assertRaises(PermissionError):
                self.session.open('PreToolUse')
            self.assertEqual(n.session_health(self.store)['sessions'][0]['workers']['waiter'], 'unknown')

    def test_exception_waiter_is_fault_reminder_not_ack(self):
        notice = self.notice()
        payload = {'hook_event_name': 'SessionStart', 'session_id': 'claude-session', 'cwd': str(self.root)}
        args = mock.Mock(vault=self.root, owner_pid=101, owner_start='owner-start', mode='wait')
        with mock.patch.object(n.os, 'getcwd', return_value=str(self.root)), \
                mock.patch.dict(os.environ, {'CLAUDE_PROJECT_DIR': str(self.root)}), \
                mock.patch.object(n.sys, 'stdin', io.StringIO(json.dumps(payload))), \
                mock.patch.object(n.sys, 'stderr', new_callable=io.StringIO) as stderr, \
                mock.patch.object(n.NotificationSession, 'wait', side_effect=OSError('store full')), \
                mock.patch.object(n.time, 'sleep') as sleep:
            self.assertEqual(n.run_hook(args), 2)
        sleep.assert_not_called()
        self.assertIn('Notification hook degraded', stderr.getvalue())
        self.assertEqual(self.store.show(notice['id'])['state'], 'pending')

    def test_real_dead_sampler_does_not_break_waiter_delivery(self):
        child = subprocess.Popen([sys.executable, '-B', '-c', 'raise SystemExit(17)'])
        self.assertEqual(child.wait(timeout=5), 17)
        notice = self.notice('TASK-after-sampler-death')
        with mock.patch.object(n.subprocess, 'Popen', return_value=child):
            code, reminder = self.session.wait()
        self.assertEqual(code, 2)
        self.assertIn(notice['id'], reminder)
        self.assertEqual(self.record()['last_waiter_exit']['reason'], 'notice')
        self.assertIn('fault:', self.record()['last_sampler_exit']['reason'])

    def test_sampler_invalid_configuration_records_fault_reason(self):
        with mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=os.getpid())):
            self.session.ensure_sampler()
        token = self.record()['workers']['sampler']['token']
        with mock.patch.dict(os.environ, {'CHRONO_NOTIFICATION_DISK_WARN_GIB': '10',
                                         'CHRONO_NOTIFICATION_DISK_CRITICAL_GIB': '20'}):
            with self.assertRaisesRegex(ValueError, 'invalid resource'):
                r.run_sampler(self.root, self.session.key, token)
        self.assertNotIn('sampler', self.record()['workers'])
        self.assertIn('invalid resource', self.record()['last_sampler_exit']['reason'])

    def test_fault_handoff_skips_rendezvous(self):
        with self.store._lock():
            data = self.store._read()
            data['sessions'][self.session.key]['last_waiter_exit'] = {
                'at': n.utc_now(), 'reason': 'fault: fixture'}
            self.store._write(data)
        started = time.monotonic()
        output = self.session.context('PreToolUse')
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertIn('degraded', output['hookSpecificOutput']['additionalContext'])

    def test_stale_fault_handoff_waits_for_new_arm(self):
        with self.store._lock():
            data = self.store._read()
            data['sessions'][self.session.key]['last_waiter_exit'] = {
                'at': n.utc_now(), 'reason': 'fault: old fixture'}
            self.store._write(data)
        with mock.patch.object(n.time, 'time', return_value=time.time() + 61), \
                mock.patch.object(n.time, 'sleep', side_effect=lambda _: self.session.claim_waiter()) as sleep:
            output = self.session.context('PreToolUse')
        sleep.assert_called_once()
        self.assertIsNone(output, 'stale fault hid a newly armed waiter')

    def test_corrupt_fault_marker_recovers_and_still_throttles(self):
        marker = self.store.directory / ('fault-' + self.session.key + '.json')
        for content in ('{broken', '{}', 'null', '{"last_fault_wake_at":"bad"}',
                        '{"last_fault_wake_at":NaN}', '{"last_fault_wake_at":1e999}',
                        '{"last_fault_wake_at":true}',
                        json.dumps({'last_fault_wake_at': 10 ** 400})):
            with self.subTest(content=content):
                marker.write_text(content)
                self.assertTrue(self.session.claim_fault_wake('fixture'), 'corrupt marker muted wake')
                self.assertFalse(self.session.claim_fault_wake('fixture'), 'recovery lost cooldown')
                self.assertIsInstance(json.loads(marker.read_text())['last_fault_wake_at'], (int, float))
        with mock.patch.object(n.time, 'time', return_value=time.time() + 61):
            self.assertTrue(self.session.claim_fault_wake('expired control'))
        with mock.patch.object(self.store, '_atomic_write', side_effect=OSError('full')), \
                mock.patch.object(n.time, 'time', return_value=time.time() + 122):
            self.assertFalse(self.session.claim_fault_wake('unwritable control'))

    def test_concurrent_fault_hooks_emit_one_wake_even_with_corrupt_store(self):
        script = '''
import io, json, os, sys
from pathlib import Path
from unittest import mock
from test_chrono_notification_session import n
root = Path(sys.argv[1])
args = mock.Mock(vault=root, owner_pid=101, owner_start='owner-start', mode='wait')
payload = dict(hook_event_name='SessionStart', session_id='claude-session', cwd=str(root))
n.process_start_identity = lambda pid: 'owner-start' if pid == 101 else 'child-start'
with mock.patch.object(n.os, 'getcwd', return_value=str(root)), \
     mock.patch.dict(os.environ, {'CLAUDE_PROJECT_DIR': str(root)}), \
     mock.patch.object(n.sys, 'stdin', io.StringIO(json.dumps(payload))), \
     mock.patch.object(n.NotificationSession, 'wait', side_effect=OSError('fault fixture')), \
     mock.patch.object(n.time, 'sleep'):
    raise SystemExit(n.run_hook(args))
'''
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                root = self.root / ('corrupt' if corrupt else 'healthy')
                flag = root / '_state/chrono/notification-hooks.enabled'
                flag.parent.mkdir(parents=True)
                flag.touch()
                store = n.NotificationStore(root)
                store.initialize()
                if corrupt:
                    store.path.write_text('{broken json')
                children = [subprocess.Popen([sys.executable, '-B', '-c', script, str(root)],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                             env={**os.environ, "PYTHONPATH": os.pathsep.join((
                                                 str(Path(__file__).parent), str(Path(__file__).parents[1])))})
                            for _ in range(6)]
                outputs = [child.communicate(timeout=10) for child in children]
                self.assertCountEqual([child.returncode for child in children], [2, 0, 0, 0, 0, 0], outputs)
                self.assertEqual(sum('Notification hook degraded' in err for _, err in outputs), 1)

    def test_offer_backoff_and_wake_cap_survive_restart_with_context_catchup(self):
        for attention in (False, True):
            with self.subTest(attention=attention):
                notice = self.notice('TASK-backoff-' + str(attention))
                if attention:
                    self.store.require_attention([notice['id']], 'held decision')
                now = time.time()
                for offset in (0, 60, 360, 2160):
                    with mock.patch.object(n.time, 'time', return_value=now + offset):
                        offered = self.session.offer()
                        self.assertEqual([x['id'] for x in offered], [notice['id']])
                    # A 60-second polling retry used to wake here after offer #2.
                    with mock.patch.object(n.time, 'time', return_value=now + offset + 59):
                        self.assertEqual(self.session.offer(), [])
                    if offset == 60:
                        with mock.patch.object(n.time, 'time', return_value=now + 121):
                            self.assertEqual(self.session.offer(), [])
                self.session = n.NotificationSession(self.root, 'claude-session', 101, 'owner-start')
                with mock.patch.object(n.time, 'time', return_value=now + 10000):
                    self.assertEqual(self.session.offer(), [])
                    health = n.session_health(self.store)
                    self.assertEqual(health['context_only_pending'], 1)
                    self.session.claim_waiter()
                    self.assertIsNone(self.session.context('PreToolUse'))
                    context = self.session.context('UserPromptSubmit')
                    self.assertIn(notice['id'], context['hookSpecificOutput']['additionalContext'])
                record = self.store.show(notice['id'])
                self.assertEqual(record['offer_count'], 5)
                if attention:
                    self.assertEqual(record['state'], 'operator-attention-required')
                self.store.acknowledge([notice['id']])

    def test_idle_poll_budget_and_changed_store_controls(self):
        # Deterministic three-second idle interval; ps latency is a native-canary metric.
        token = self.session.claim_waiter()
        with mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=202)):
            self.session.ensure_sampler()
        self.session.offer()  # Warm the read-only snapshot.
        clock = time.monotonic()
        wall = time.time()
        with mock.patch.object(n, 'process_start_identity', wraps=n.process_start_identity) as identity, \
                mock.patch.object(self.store, '_read', wraps=self.store._read) as reads, \
                mock.patch.object(self.store, '_lock', wraps=self.store._lock) as locks:
            for tick in range(7):
                with mock.patch.object(n.time, 'monotonic', return_value=clock + tick * 0.5), \
                        mock.patch.object(n.time, 'time', return_value=wall + tick * 0.5):
                    self.assertTrue(self.session.active())
                    self.assertTrue(self.session.heartbeat('waiter', token))
                    self.session.ensure_sampler()
                    self.assertEqual(self.session.offer(), [])
            self.assertLessEqual(identity.call_count, 1)
            self.assertEqual(reads.call_count, 0)
            self.assertEqual(locks.call_count, 0)
        # A different producer's atomic replacement invalidates the snapshot.
        producer = n.NotificationStore(self.root)
        notice = producer.enqueue_task('TASK-changed-store', 1, 'complete', 'changed')
        self.assertEqual([x['id'] for x in self.session.offer()], [notice['id']])
        other = n.NotificationSession(self.root, 'claude-session', 101, 'owner-start')
        other.close()
        self.assertFalse(self.session.active())

    def test_hook_timestamp_writes_are_throttled(self):
        with mock.patch.object(self.store, '_write', wraps=self.store._write) as writes:
            for event in ('PreToolUse', 'PostToolUse', 'Stop') * 3:
                self.assertTrue(self.session.open(event))
            self.assertEqual(writes.call_count, 0)
            with mock.patch.object(n.time, 'time', return_value=time.time() + 6):
                self.assertTrue(self.session.open('UserPromptSubmit'))
            self.assertEqual(writes.call_count, 1)

    def test_sampler_poll_budget_rechecks_owner_and_preserves_heartbeat(self):
        with mock.patch.object(n.subprocess, 'Popen', return_value=mock.Mock(pid=os.getpid())):
            self.session.ensure_sampler()
        token = self.record()['workers']['sampler']['token']
        mono, wall = time.monotonic(), time.time()
        elapsed = [0.0]

        def advance(seconds):
            elapsed[0] += seconds
            if elapsed[0] >= 12:
                self.identities.pop(101, None)
            self.assertLess(elapsed[0], 18, 'sampler ignored owner death')

        with mock.patch.object(n.time, 'monotonic', side_effect=lambda: mono + elapsed[0]), \
                mock.patch.object(n.time, 'time', side_effect=lambda: wall + elapsed[0]), \
                mock.patch.object(n.time, 'sleep', side_effect=advance), \
                mock.patch.object(n, 'process_start_identity', wraps=n.process_start_identity) as identities, \
                mock.patch.object(n.NotificationStore, '_read', autospec=True,
                                  side_effect=n.NotificationStore._read) as reads, \
                mock.patch.object(r, 'sample_resources', return_value={
                    'disk_free_bytes': 100 * r.GIB, 'memory_pressure': 'normal'}) as samples:
            self.assertEqual(r.run_sampler(self.root, self.session.key, token), 0)
        self.assertGreaterEqual(elapsed[0], 12)
        self.assertLessEqual(identities.call_count, 6)
        self.assertLessEqual(reads.call_count, 12)
        samples.assert_called_once()
        self.assertIn('last_sample_at', self.record())
        self.assertNotIn('sampler', self.record()['workers'])


class CanaryRunbook(unittest.TestCase):
    def document(self):
        path = 'docs/runbooks/notification-hooks-canary.md'
        if CONTROL_REF:
            return subprocess.check_output(['git', '-C', str(REPO), 'show',
                                            f'{CONTROL_REF}:{path}'], text=True)
        return (REPO / path).read_text()

    def test_delivery_sla_excludes_ack_approval_delay(self):
        document = self.document()
        script = re.search(r'cat > "\$S/check-delivery.py" <<\'PY\'\n(.*?)\nPY',
                           document, re.DOTALL)
        self.assertIsNotNone(script, 'runbook has no independently testable delivery check')
        notice = {'id': 'notice-fixture', 'enqueued_at': '2026-09-27T22:00:00Z',
                  'offered_at': '2026-09-27T22:00:01Z', 'state': 'offered', 'offer_count': 1}
        ack = {**notice, 'state': 'acknowledged', 'offer_count': 2,
               'offered_at': '2026-09-27T22:01:01Z', 'ack_latency_seconds': 300}

        def check(visible):
            with mock.patch.object(sys, 'argv', ['check-delivery.py', 'first.json', 'final.json', visible]), \
                    mock.patch.object(Path, 'read_text', side_effect=[json.dumps(notice), json.dumps(ack)]), \
                    mock.patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
                exec(compile(script[1], 'runbook:check-delivery.py', 'exec'), {})
                return json.loads(output.getvalue())

        self.assertEqual(check('2026-09-27T22:00:02Z')['ack_latency_seconds'], 300)
        with self.assertRaisesRegex(AssertionError, 'visible reminder'):
            check('2026-09-27T22:00:06Z')
        notice['offered_at'] = '2026-09-27T22:00:06Z'
        with self.assertRaisesRegex(AssertionError, 'waiter offer'):
            check('2026-09-27T22:00:02Z')
        notice['offered_at'] = '2026-09-27T22:00:01Z'
        ack['id'] = 'notice-unrelated'
        with self.assertRaisesRegex(AssertionError, 'mismatched ACK'):
            check('2026-09-27T22:00:02Z')

    def test_each_canary_schedule_reserves_rollback_inside_watchdog(self):
        document = self.document()
        rows = re.findall(r'^\| (interactive|soak) \| ([\d, ]+) \| ([\d |]+)\|$',
                          document, re.MULTILINE)
        self.assertEqual({row[0] for row in rows}, {'interactive', 'soak'},
                         'interactive and 67-minute soak need separate scratch runs')
        for run, sections, budget in rows:
            setup, cases, catchup, rollback, total, margin = map(int, budget.split('|'))
            self.assertEqual(setup + cases + catchup + rollback, total)
            self.assertEqual(total + margin, 90)
            self.assertGreaterEqual(rollback, 5)
            self.assertGreater(margin, 0)
            self.assertIn('6', sections)
            if run == 'soak':
                self.assertEqual(cases, 67)
                self.assertNotIn('3', sections)
                self.assertNotIn('5', sections)
        self.assertIn('time.monotonic() + 5400', document)
        self.assertLess(document.index("printf 'Evidence directory"), document.index('git -C "$LIVE" archive'))


class HookGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='notice-guards-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / 'called'
        helper = self.root / 'helper.sh'
        helper.write_text(f'printf called > {shlex.quote(str(self.marker))}\nchrono_pane_has_coordinator() {{ return 0; }}\n')
        tmux = self.root / 'tmux'
        tmux.write_text('#!/bin/sh\nprintf "notice-fixture\\n"\n')
        tmux.chmod(0o755)
        python = self.root / 'python'
        python.write_text('#!/bin/sh\nprintf "POSITIVE-CONTROL\\n"\n')
        python.chmod(0o755)
        record = self.root / 'coordinator.pid'
        record.write_text(f'pid {os.getpid()}\nstart fixture-start\n')
        self.hook = HOOK
        if CONTROL_REF:
            self.hook = self.root / 'control-hook.sh'
            self.hook.write_bytes(subprocess.check_output(
                ['git', '-C', str(REPO), 'show', f'{CONTROL_REF}:bin/chrono-notification-hook.sh']))
        self.env = {**os.environ, 'CLAUDE_PROJECT_DIR': str(self.root),
                    'CHRONO_NOTIFICATION_ROOT': str(self.root),
                    'CHRONO_NOTIFICATION_PANE_HELPER': str(helper),
                    'CHRONO_NOTIFICATION_PYTHON': str(python), 'TMUX_BIN': str(tmux),
                    'CHRONO_COORDINATOR_PIDFILE': str(record), 'TMUX_PANE': '%123'}

    def enable(self):
        flag = self.root / '_state/chrono/notification-hooks.enabled'
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()

    def invoke(self, cwd=None, env=None):
        return subprocess.run(['bash', str(self.hook)], cwd=cwd or self.root,
                              env=env or self.env, input='not even JSON',
                              capture_output=True, text=True, timeout=3)

    def assert_noop(self, result):
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertFalse(self.marker.exists())

    def test_absent_flag_is_fast_silent_noop_with_enabled_positive_control(self):
        started = time.monotonic()
        self.assert_noop(self.invoke())
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse((self.root / '_state').exists())
        self.enable()
        positive = self.invoke()
        self.assertEqual(positive.stdout, 'POSITIVE-CONTROL\n')
        self.assertTrue(self.marker.exists())

    def test_workers_excluded_before_dependencies_even_with_flag(self):
        self.enable()
        worker = self.root / '_state/board-worktrees/worker'
        worker.mkdir(parents=True)
        self.assert_noop(self.invoke(cwd=worker))
        self.assert_noop(self.invoke(env={**self.env, 'CLAUDE_PROJECT_DIR': str(worker)}))

    def test_payload_worker_guard_and_missing_flag_python_guard(self):
        self.enable()
        args = mock.Mock(vault=self.root, mode='context')
        payload = {'cwd': '/repo/_state/board-worktrees/a', 'session_id': 'a', 'hook_event_name': 'SessionStart'}
        with mock.patch.object(n.os, 'getcwd', return_value=str(self.root)), \
                mock.patch.dict(os.environ, {'CLAUDE_PROJECT_DIR': str(self.root)}), \
                mock.patch.object(n.sys, 'stdin', io.StringIO(json.dumps(payload))), \
                mock.patch.object(n, 'NotificationSession') as session:
            self.assertEqual(n.run_hook(args), 0)
        session.assert_not_called()

    def test_non_coordinator_is_noop(self):
        self.enable()
        helper = self.root / 'negative.sh'
        helper.write_text('chrono_pane_has_coordinator() { return 1; }\n')
        self.assert_noop(self.invoke(env={**self.env, 'CHRONO_NOTIFICATION_PANE_HELPER': str(helper)}))

    def test_foreign_parent_is_silent_even_in_coordinator_pane(self):
        self.enable()
        self.assertEqual(self.invoke().stdout, 'POSITIVE-CONTROL\n')
        Path(self.env['CHRONO_COORDINATOR_PIDFILE']).write_text('pid 99999999\nstart fixture-start\n')
        result = self.invoke()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_time_wrapper_passes_and_nearest_nested_claude_is_rejected(self):
        self.enable()
        # A real forking wrapper, with a controlled process snapshot because
        # lane sandboxes may deny host ps. Native ps parentage is canary-owned.
        helper = Path(self.env['CHRONO_NOTIFICATION_PANE_HELPER'])
        helper.write_text('chrono_pane_has_coordinator() { return 0; }\n'
                          f'ps() {{ printf "%s {os.getpid()} /usr/bin/time\\n'
                          f'{os.getpid()} 1 /fixture/claude\\n" "$PPID"; }}\n')
        result = subprocess.run(['/usr/bin/time', 'bash', str(self.hook)], cwd=self.root,
                                env=self.env, input='fixture', capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'POSITIVE-CONTROL\n')
        # Fixture the one ps snapshot to pin nearest-ancestor and hop boundaries.
        Path(self.env['CHRONO_COORDINATOR_PIDFILE']).write_text('pid 9090\nstart fixture-start\n')
        for nested, expected in (('/usr/bin/time', 'POSITIVE-CONTROL\n'),
                                 ('/opt/claude/versions/2.1.0', '')):
            helper.write_text('chrono_pane_has_coordinator() { return 0; }\n'
                              'ps() { printf "%s 8080 /bin/sh\\n8080 9090 %s\\n9090 1 /opt/claude\\n" '
                              f'"$PPID" {shlex.quote(nested)}; }}\n')
            self.assertEqual(self.invoke().stdout, expected)
        helper.write_text('chrono_pane_has_coordinator() { return 0; }\n'
                          'ps() { printf "%s 8080 /bin/sh\\n8080 7070 /bin/sh\\n7070 6060 /bin/sh\\n'
                          '6060 9090 /bin/sh\\n9090 1 /opt/claude\\n" "$PPID"; }\n')
        self.assertEqual(self.invoke().stdout, '', 'unbounded ancestry accepted')

    def test_settings_event_contract_timeout_margin_and_root_chrono_resolution(self):
        settings = json.loads((REPO / '.claude/settings.json').read_text())['hooks']
        self.assertEqual(set(settings), n.HOOK_EVENTS)
        for event, matchers in settings.items():
            hooks = matchers[0]['hooks']
            if event != 'SessionEnd':
                self.assertTrue(hooks[0]['asyncRewake'])
                self.assertGreater(hooks[0]['timeout'], 1500)
                self.assertTrue(hooks[0]['command'].endswith('wait'))
            self.assertTrue(hooks[-1]['command'].endswith('context'))
        self.assertEqual(settings['SessionStart'][0]['matcher'], 'startup|resume|clear|compact')
        (self.root / 'bin').mkdir()
        stub = self.root / 'bin/chrono-notification-hook.sh'
        stub.write_text('printf "RESOLVED:%s\\n" "$1"\n')
        (self.root / 'chrono').mkdir()
        for cwd in (self.root, self.root / 'chrono'):
            result = subprocess.run(['bash', '-c', settings['SessionStart'][0]['hooks'][0]['command']],
                                    env={**self.env, 'CLAUDE_PROJECT_DIR': str(cwd)}, cwd=cwd,
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.stdout, 'RESOLVED:wait\n')


if __name__ == '__main__':
    unittest.main()
