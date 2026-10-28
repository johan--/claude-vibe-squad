"""Run only doctor's read-only policy section, never the host-wide doctor."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_disk_hygiene_local_config import provision_config

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('policy,expected', [('missing', 'WARN'), ('malformed', 'WARN'),
                                            ('incomplete', 'WARN'), ('valid', 'OK')])
def test_doctor_disk_policy(tmp_path, policy, expected):
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/python').symlink_to(REPO / 'scripts/python')
    if policy != 'missing':
        path = provision_config(tmp_path)
        if policy == 'malformed':
            path.write_text('{')
        elif policy == 'incomplete':
            path.write_text('{"tmp": {}}')
    source = (REPO / 'bin/doctor.sh').read_text()
    section = source.split('# Local disk protection policy:', 1)[1].split(
        '# End local disk protection policy.', 1)[0]
    script = ('note_ok() { echo "OK $*"; }; note_warn() { echo "WARN $*"; };\n'
              '# Local disk protection policy:' + section)
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path.parent,
                            env=dict(os.environ, VAULT_ROOT=str(tmp_path),
                                     PYTHONDONTWRITEBYTECODE='1'),
                            text=True, capture_output=True, check=True)
    assert result.stdout.startswith(expected), result.stdout + result.stderr
    if expected == 'WARN':
        assert 'scratch retained' in result.stdout
