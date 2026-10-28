"""Read-only disk and memory-pressure sampling into the durable notice inbox.

No cleanup, process termination, swap manipulation, or resource-limit changes.
Defaults: Data volume warn <25 GiB / critical <15 GiB, 2 GiB hysteresis,
two consecutive lower-severity samples, and a 60-second sampling period.
CHRONO_NOTIFICATION_* environment settings permit explicit local overrides.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import sys
import time

from host_admission import read_pressure_level
from .notifications import (NotificationSession, NotificationStore, enabled,
                            setting, utc_now)

GIB = 1024 ** 3
RANK = {'normal': 0, 'warn': 1, 'critical': 2}


@dataclass(frozen=True)
class ResourceConfig:
    warn_gib: float = 25
    critical_gib: float = 15
    hysteresis_gib: float = 2
    recovery_samples: int = 2
    period: float = 60

    def __post_init__(self):
        values = (self.warn_gib, self.critical_gib, self.hysteresis_gib, self.period)
        if (not all(math.isfinite(v) for v in values)
                or not 0 < self.critical_gib < self.warn_gib
                or self.hysteresis_gib <= 0 or self.period <= 0
                or type(self.recovery_samples) is not int or self.recovery_samples < 1):
            raise ValueError('invalid resource thresholds, hysteresis or period')

    @classmethod
    def from_env(cls):
        count = setting('RECOVERY_SAMPLES', 2, 1, 10)
        if not count.is_integer():
            raise ValueError('RECOVERY_SAMPLES must be an integer')
        return cls(setting('DISK_WARN_GIB', 25, 0.1, 100000),
                   setting('DISK_CRITICAL_GIB', 15, 0.1, 100000),
                   setting('DISK_HYSTERESIS_GIB', 2, 0.1, 10000), int(count),
                   setting('SAMPLE_SECONDS', 60, 0.05, 3600))


def sample_resources(root):
    disk_path = Path('/System/Volumes/Data') if sys.platform == 'darwin' else Path(root)
    errors = {}
    try:
        free = shutil.disk_usage(disk_path).free
    except OSError as exc:
        free = None
        errors['disk'] = f'{type(exc).__name__}: {str(exc)[:160]}'
    # Reuse the existing enum parser. Free-memory percentage is not pressure.
    pressure = read_pressure_level() if sys.platform == 'darwin' else 'unknown'
    if pressure == 'unknown':
        errors['memory'] = 'memory pressure enum unavailable'
    return {'disk_free_bytes': free, 'memory_pressure': pressure,
            'disk_path': str(disk_path), 'errors': errors}


def disk_level(free, previous, config):
    if (not isinstance(free, (int, float)) or isinstance(free, bool)
            or not math.isfinite(free) or free < 0):
        return 'unknown'
    gib = free / GIB
    if gib < config.critical_gib:
        return 'critical'
    if previous == 'critical' and gib < config.critical_gib + config.hysteresis_gib:
        return 'critical'
    if gib < config.warn_gib:
        return 'warn'
    if previous in {'warn', 'critical'} and gib < config.warn_gib + config.hysteresis_gib:
        return 'warn'
    return 'normal'


def transition(prior, observed, recovery_samples):
    """Escalate/unknown immediately; require consecutive good samples to recover."""
    state = dict(prior or {'level': 'normal', 'episode': 0})
    old = state['level']
    if observed == old:
        state.update(candidate=None, candidate_count=0)
        return state, None
    down = observed != 'unknown' and (old == 'unknown' or RANK[observed] < RANK[old])
    if down:
        count = state.get('candidate_count', 0) + 1 if state.get('candidate') == observed else 1
        state.update(candidate=observed, candidate_count=count)
        if count < recovery_samples:
            return state, None
    if old == 'normal' or observed == 'unknown':
        state['episode'] += 1
        state['transition_seq'] = 0
    state['transition_seq'] = state.get('transition_seq', 0) + 1
    state.update(level=observed, candidate=None, candidate_count=0)
    return state, ('recovered' if observed == 'normal' else observed)


def publish_sample(store, sample, config):
    """Episode transition and enqueue share one fsynced transaction.

    Failure before replacement commits neither, so a later sample can retry.
    Multiple session samplers share the same episode IDs and cannot double-page.
    """
    with store._lock():
        data = store._read()
        resources = data.setdefault('resources', {})
        notices = []
        for resource in ('disk', 'memory'):
            prior = resources.get(resource)
            if resource == 'disk':
                value = sample.get('disk_free_bytes')
                observed = disk_level(value, (prior or {}).get('last_known_level',
                                                            (prior or {}).get('level')), config)
                detail = ('unknown free space' if observed == 'unknown'
                          else f'{value / GIB:.2f} GiB free')
            else:
                value = sample.get('memory_pressure')
                observed = value if value in RANK else 'unknown'
                detail = 'pressure enum ' + str(observed)
            state, edge = transition(prior, observed, config.recovery_samples)
            state.update(last_sample_at=utc_now(), observed=observed,
                         value=None if observed == 'unknown' else value)
            if state['level'] != 'unknown':
                state['last_known_level'] = state['level']
            resources[resource] = state
            if edge:
                count = len(data['notices'])
                notice = store._enqueue_into(
                    data, ['resource', resource, f"{state['episode']}:{state['transition_seq']}:{edge}"],
                    f'RESOURCE {resource} {edge}: {detail}. Read-only observation; no cleanup authorized.',
                    metadata={'episode': state['episode'], 'level': edge,
                              'transition_seq': state['transition_seq'],
                              'disk_path': sample.get('disk_path'),
                              'errors': sample.get('errors', {}),
                              'warn_gib': config.warn_gib, 'critical_gib': config.critical_gib,
                              'hysteresis_gib': config.hysteresis_gib,
                              'recovery_samples': config.recovery_samples})
                if notice and len(data['notices']) > count:
                    notices.append(notice)
        data['last_resource_sample_at'] = utc_now()
        store._write(data)
        return notices


def run_sampler(root, key, token):
    store = NotificationStore(root)
    # A child never creates a session. Only its guarded hook can authorize it.
    with store._lock():
        record = store._read().get('sessions', {}).get(key)
    if not record or record.get('closed_at'):
        return 0
    session = NotificationSession(root, record['session_id'], **{
        'owner_pid': record['owner']['pid'], 'owner_start': record['owner']['process_start']})
    if session.key != key:
        return 0
    next_sample = 0
    reason = 'stopped'
    try:
        config = ResourceConfig.from_env()
        while session.active() and session.heartbeat('sampler', token):
            sample_started = time.monotonic()
            if sample_started >= next_sample:
                publish_sample(store, sample_resources(root), config)
                session.heartbeat('sampler', token, last_sample_at=utc_now(),
                                  sample_period_seconds=config.period)
                next_sample = sample_started + config.period
            time.sleep(session.poll)
        return 0
    except Exception as exc:
        reason = f'fault: {type(exc).__name__}: {str(exc)[:300]}'
        raise
    finally:
        if enabled(root):
            session.release('sampler', token, reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args(argv)
    if not enabled(args.vault):
        return 0
    return run_sampler(args.vault, args.session, args.token)


if __name__ == '__main__':
    raise SystemExit(main())
