"""Exercise the existing move stages and new pack stage through the real CLI."""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import state_archive as archive


SCRIPT = Path(__file__).resolve().parents[1] / "state_archive.py"


@pytest.fixture
def registry_paths(tmp_path, monkeypatch):
    state = tmp_path / "_state"
    state.mkdir()
    for name, filename in (("REGISTRY", "active-tasks.json"),
                           ("REGISTRY_LOCK", "active-tasks.json.lock"),
                           ("REGISTRY_ARCHIVE", "active-tasks-archive.json")):
        monkeypatch.setattr(archive, name, state / filename)
    return state


@pytest.mark.parametrize("field", ["notice_pending_events", "notification_pending_events"])
@pytest.mark.parametrize("pending", [{"event": {}}, None, [], ["event"], "", "event", 0, False])
def test_pending_or_malformed_queues_stay_active(registry_paths, field, pending):
    task = "TASK-2020-01-01-0000-pending"
    drained = "TASK-2020-01-01-0001-drained"
    registry = {task: {"status": "complete", field: pending},
                drained: {"status": "complete", "notice_pending_events": {},
                          "notification_pending_events": {}}}
    archive.REGISTRY.write_text(json.dumps(registry))
    cutoff = dt.date(2026, 10, 1)
    assert archive.archive_registry(cutoff, False)[:2] == (1, 1)
    assert json.loads(archive.REGISTRY.read_text()) == registry
    assert archive.archive_registry(cutoff, True)[:2] == (1, 1)
    assert json.loads(archive.REGISTRY.read_text()) == {task: registry[task]}
    assert json.loads(archive.REGISTRY_ARCHIVE.read_text()) == {drained: registry[drained]}


def test_registry_retention_still_uses_task_date(registry_paths):
    registry = {
        "TASK-2026-09-30-old": {"status": "complete"},
        "TASK-2026-10-01-boundary": {"status": "complete"},
        "TASK-2026-10-02-new": {"status": "complete"},
        "TASK-2020-01-01-live": {"status": "in-flight"},
        "TASK-undated": {"status": "complete"},
    }
    archive.REGISTRY.write_text(json.dumps(registry))
    assert archive.archive_registry(dt.date(2026, 10, 1), True)[:2] == (1, 4)
    assert json.loads(archive.REGISTRY.read_text()) == {
        key: value for key, value in registry.items() if key != "TASK-2026-09-30-old"
    }


def test_failed_notice_survives_settlement_archive_and_retry(registry_paths, monkeypatch):
    import registry_reconciler as rr
    from chrono_state.notifications import NotificationStore

    root = registry_paths.parent
    monkeypatch.setattr(rr, "VAULT_ROOT", root)
    monkeypatch.setattr(rr, "STATE_DIR", registry_paths)
    monkeypatch.setattr(rr, "REGISTRY_PATH", archive.REGISTRY)
    monkeypatch.setenv(rr.TEST_ISOLATION_ENV, "1")
    # Settlement and retry exercise the real reconciler, with external delivery
    # stubbed. Only the durable local store is the delivery oracle here.
    monkeypatch.setattr(rr, "deliver_notification_sweep", lambda events, *_: [True] * len(events))
    monkeypatch.setattr(rr, "RESPONSE_MIN_AGE", dt.timedelta(0))
    task = "TASK-2020-01-01-0000-retry"
    archive.REGISTRY.write_text(json.dumps({task: {
        "status": "in-flight", "to_model": "gpt-codex",
        "dispatched_at": "2020-01-01T00:00:00Z"}}))
    outbox = root / "departments/coding/outbox"
    outbox.mkdir(parents=True)
    (outbox / f"{task}-response.md").write_text("---\nstatus: complete\n---\n\nDone.\n")
    with monkeypatch.context() as failure:
        def unavailable(*args, **kwargs):
            raise OSError("fixture store unavailable")
        failure.setattr(NotificationStore, "enqueue_task", unavailable)
        rr.reconcile(task, False)
    settled = json.loads(archive.REGISTRY.read_text())[task]
    assert settled["status"] == "complete"
    assert settled["notice_pending_events"]
    store = NotificationStore(root)
    assert store.list_pending() == []
    cutoff = dt.date(2026, 10, 1)
    assert archive.archive_registry(cutoff, True)[:2] == (0, 1)
    rr.reconcile(task, False)
    retried = json.loads(archive.REGISTRY.read_text())[task]
    assert not retried.get("notice_pending_events")
    notices = store.list_pending()
    assert len(notices) == 1
    assert task in notices[0]["identity"]
    store.acknowledge([notices[0]["id"]])
    assert store.list_pending() == []
    assert archive.archive_registry(cutoff, True)[:2] == (1, 0)
    assert task in json.loads(archive.REGISTRY_ARCHIVE.read_text())


def test_cli_dry_run_then_apply_archives_and_packs(tmp_path):
    state = tmp_path / "_state"
    dispatch = state / "board-dispatch"
    dispatch.mkdir(parents=True)
    old = dt.date.today() - dt.timedelta(days=70)
    task = f"TASK-{old.isoformat()}-0000-old"
    live = f"TASK-{old.isoformat()}-0001-live"
    registry = {task: {"status": "complete"}, live: {"status": "in-flight"}}
    (state / "active-tasks.json").write_text(json.dumps(registry))
    source = dispatch / f"{task}.dispatch.json"
    source.write_text(json.dumps({"task_id": task}))
    timestamp = dt.datetime.combine(old, dt.time(), tzinfo=dt.timezone.utc).timestamp()
    os.utime(source, (timestamp, timestamp))
    live_source = dispatch / f"{live}.dispatch.json"
    live_source.write_text(json.dumps({"task_id": live, "pid": os.getpid()}))
    env = dict(os.environ, VAULT_ROOT=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert source.exists()
    assert json.loads((state / "active-tasks.json").read_text()) == registry
    assert not list(state.glob("_archive-*.tar.zst"))
    result = subprocess.run([sys.executable, str(SCRIPT), "--apply"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "packed 1 files" in result.stdout
    assert not source.exists()
    assert not (state / "board-dispatch-archive" / source.name).exists()
    assert live_source.exists()
    assert json.loads((state / "active-tasks.json").read_text()) == {live: registry[live]}
    assert json.loads((state / "active-tasks-archive.json").read_text()) == {task: registry[task]}
    assert (state / f"_archive-board-dispatch-archive-{old:%Y%m}.tar.zst").is_file()
