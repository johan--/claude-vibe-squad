"""Controlled argv censuses; no host process control or environment logging."""
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import disk_hygiene as hygiene


def census(path, command, idle=True):
    with mock.patch.object(hygiene.subprocess, 'run', return_value=
                           subprocess.CompletedProcess([], 0, command, '')), \
            mock.patch.object(hygiene, 'idle_path', return_value=idle):
        return hygiene.process_idle(path)


def test_distnoted_does_not_pin_dist_but_full_path_does(tmp_path):
    path = tmp_path / 'dist'
    path.mkdir()
    assert census(path, '/usr/sbin/distnoted\n')
    assert not census(path, f'/usr/sbin/distnoted\nworker {path}\n')


@pytest.mark.parametrize('suffix', ['', '/child', "'", '"', ':other'])
def test_full_path_boundaries(tmp_path, suffix):
    path = tmp_path / 'dir with spaces' / 'dist'
    assert not census(path, f'worker CACHE={path}{suffix}\n')


@pytest.mark.parametrize('command', ['worker {path}noted', 'worker {path}-old',
                                     'worker /prefix{path}', 'worker dist'])
def test_sibling_and_basename_are_not_references(tmp_path, command):
    path = tmp_path / 'dist'
    assert census(path, command.format(path=path))


def test_handle_and_failed_census_controls(tmp_path):
    assert not census(tmp_path, 'unrelated process', idle=False)
    for result in [subprocess.CompletedProcess([], 1, '', ''),
                   subprocess.CompletedProcess([], 0, '', ''),
                   subprocess.CompletedProcess([], 0, 'worker', 'denied')]:
        with mock.patch.object(hygiene.subprocess, 'run', return_value=result):
            assert not hygiene.process_idle(tmp_path)
    for error in [OSError('denied'), subprocess.TimeoutExpired('ps', 15)]:
        with mock.patch.object(hygiene.subprocess, 'run', side_effect=error):
            assert not hygiene.process_idle(tmp_path)


def test_macos_tmp_alias():
    with mock.patch.object(hygiene.sys, 'platform', 'darwin'):
        assert not census(Path('/private/tmp/fixture/dist'), 'worker /tmp/fixture/dist/file')
        assert census(Path('/private/tmp/fixture/dist'), 'worker /tmp/fixture/distnoted')


@pytest.mark.parametrize('command', ['worker PATH=/usr/bin:{path}/bin', 'sh -c ({path})',
                                     'worker [{path}]', 'worker {path}|wc', 'worker {path}>out'])
def test_separator_boundaries_are_references(tmp_path, command):
    path = tmp_path / 'dist'
    assert not census(path, command.format(path=path))
