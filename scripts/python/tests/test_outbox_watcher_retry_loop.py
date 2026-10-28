"""Execute the shipped retry functions against a temporary outbox and clock."""
import os
from pathlib import Path
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = (ROOT / "bin/outbox-watcher.sh").read_text()


def function(name):
    start = SOURCE.index(f"{name}() {{")
    return SOURCE[start:SOURCE.index("\n}\n", start) + 3]


def run_retry(tmp_path, *, age=0, ready_on=0, remove_on=0, duplicate=False):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    response = outbox / "TASK-fixture-response.md"
    response.write_text("---\nid: fixture\n---\n\nUnfinished response.\n")
    timestamp = time.time() - age
    os.utime(response, (timestamp, timestamp))
    script = '''set -eu
PENDING_PATHS="|"
RESPONSE_MIN_AGE_SECONDS=0
sleep() {
    printf '%s\\n' "$1" >> "$SLEEPS"
    SECONDS=$((SECONDS + $1))
    count=$((count + 1))
    if (( count == READY_ON )); then
        printf '%s\\n' '---' 'status: complete' '---' > "$RESPONSE"
    fi
    if (( count == REMOVE_ON )); then
        rm -- "$RESPONSE"
    fi
    return 0
}
handle_response_path() { printf '%s\\n' "$1" >> "$HANDLED"; }
count=0
'''
    script += function("legacy_response_ready_for_status")
    script += function("schedule_response_retry")
    script += '\nschedule_response_retry "$RESPONSE"\n'
    if duplicate:
        script += 'schedule_response_retry "$RESPONSE"\n'
    script += "wait\n"
    sleeps = tmp_path / "sleeps"
    handled = tmp_path / "handled"
    result = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10,
        env={**os.environ, "RESPONSE": str(response), "SLEEPS": str(sleeps),
             "HANDLED": str(handled), "READY_ON": str(ready_on), "REMOVE_ON": str(remove_on)},
    )
    assert result.returncode == 0, result.stderr
    return result, sleeps.read_text().splitlines() if sleeps.exists() else [], handled.exists()


def test_statusless_response_stops_after_600_seconds(tmp_path):
    result, sleeps, handled = run_retry(tmp_path)
    assert sleeps == ["5"] * 120
    assert "expired after 600s" in result.stdout
    assert not handled


def test_old_response_never_starts_a_retry(tmp_path):
    result, sleeps, handled = run_retry(tmp_path, age=7 * 86400 + 60)
    assert "mtime older than 7 days" in result.stdout
    assert sleeps == []
    assert not handled


@pytest.mark.parametrize("age", [0, 7 * 86400 - 60])
def test_new_status_is_detected_and_handled(tmp_path, age):
    result, sleeps, handled = run_retry(tmp_path, age=age, ready_on=2)
    assert sleeps == ["5", "5"]
    assert handled
    assert "expired" not in result.stdout


def test_repeated_events_schedule_only_one_retry(tmp_path):
    result, sleeps, handled = run_retry(tmp_path, ready_on=1, duplicate=True)
    assert sleeps == ["5"]
    assert handled
    assert result.stdout.count("scheduling delayed retry") == 1


def test_removed_response_ends_retry(tmp_path):
    result, sleeps, handled = run_retry(tmp_path, remove_on=1)
    assert sleeps == ["5"]
    assert not handled
    assert "expired" not in result.stdout


def test_real_handler_uses_bounded_retry():
    handler = function("handle_response_path")
    assert 'schedule_response_retry "$path"' in handler
    assert 'while [[ -f "$path" ]]' not in handler
