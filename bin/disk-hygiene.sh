#!/usr/bin/env bash
# Existing watcher timer owns scheduling. --check is read-only; --periodic and
# --task-id use the preservation-first pruner, never a second worktree reaper.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"
cd "$VAULT_ROOT"

# Operator pause switch: while this file exists, the automatic periodic sweep
# deletes nothing. Manual --plan/--check still run so the survey stays visible.
if [[ " $* " == *" --periodic "* && -e "_state/disk-hygiene.paused" ]]; then
    echo "disk-hygiene: periodic sweep paused ($(head -1 _state/disk-hygiene.paused 2>/dev/null))"
    exit 0
fi

python3 - "$@" <<'PY'
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description="Board disk headroom and guarded cleanup")
mode = parser.add_mutually_exclusive_group(required=True)
mode.add_argument("--check", action="store_true")
mode.add_argument("--periodic", action="store_true")
mode.add_argument("--task-id")
# disk-cleanup skill: survey (dry-run, default), Tier 1, Tier 1+2. Never Tier 3.
mode.add_argument("--plan", action="store_true")
mode.add_argument("--apply-safe", dest="apply_safe", action="store_true")
mode.add_argument("--apply-preserve", dest="apply_preserve", action="store_true")
parser.add_argument("--now", action="store_true", help="bypass periodic cadence only; keep all safety guards")
args = parser.parse_args()
if args.now and not args.periodic:
    parser.error("--now requires --periodic")
root = Path(os.environ.get("VAULT_ROOT", Path.cwd())).resolve()
state = root / "_state"
sys.path.insert(0, str(root / "scripts/python"))
from disk_hygiene import cleanup_plan, disk_report, run_safe_sweep
from chrono_state.notifications import NotificationStore

if args.plan:
    raise SystemExit(cleanup_plan(root, "plan"))
if args.apply_safe:
    raise SystemExit(cleanup_plan(root, "apply-safe"))
if args.apply_preserve:
    raise SystemExit(cleanup_plan(root, "apply-preserve"))

interval = 30 * 60
warning_floor = 15 * 2**30  # GiB; the admission floor remains separate


def check_disk():
    volume = Path("/System/Volumes/Data")
    if os.environ.get("SQUAD_TEST_ISOLATION") == "1" or not volume.is_dir():
        volume = root
    free = shutil.disk_usage(volume).free
    if free >= warning_floor:
        print(f"disk: {free / 2**30:.1f} GiB free (warning below 15 GiB)")
        return 0
    detail = disk_report(root)
    message = (f"[vibesquad] DISK WARNING: {free / 2**30:.1f} GiB free, below 15 GiB; "
               f"{detail}")
    print(message, flush=True)
    if args.periodic:
        # Use the existing bounded threshold episode for both producers.
        episode = f"disk-low:{int(time.time()) // interval}"
        try:
            notices = NotificationStore(root)
            notices.initialize()
            notices.enqueue_resource("disk", episode, message)
        except (OSError, ValueError) as exc:
            print(f"disk notice enqueue deferred: {exc}", flush=True)
        # P1 keeps the existing pane resolver and legacy receipt path.
        try:
            result = subprocess.run(["bash", str(root / "bin/outbox-watcher.sh"),
                                     "--notify-once", episode,
                                     message], timeout=40, env={**os.environ, "NOTICE_ALREADY_ENQUEUED": "1"})
            notified = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            notified = False
        if not notified:
            print("disk warning remains in watcher log; pane notification unavailable", flush=True)
    return 3  # warning, not a failed measurement


if args.task_id:
    raise SystemExit(subprocess.call(["bash", "bin/prune-board-worktrees.sh", "--apply",
                                     "--task-id", args.task_id]))

def measured_check():
    try:
        return check_disk()
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"disk headroom UNKNOWN: {exc}")
        return 2


if args.check:
    raise SystemExit(measured_check())

state.mkdir(exist_ok=True)
with (state / "disk-hygiene.lock").open("a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        measured_check()
        raise SystemExit(0)
    stamp = state / "disk-hygiene.json"
    try:
        last_run = float(json.loads(stamp.read_text())["last_success"])
    except (OSError, ValueError, KeyError, TypeError):
        last_run = 0
    now = time.time()
    if not args.now and 0 <= now - last_run < interval:
        measured_check()
        raise SystemExit(0)
    # Operator 2026-09-30: safe regenerable pools sweep by default. The rescue
    # archive and unrecognised/user files remain alert-only. This same Tier-1
    # board sweep backs the explicit `--apply-safe` skill path (one home).
    sweep_status = run_safe_sweep(root)
    check_status = measured_check()  # Alert on the data volume AFTER all sweeps.
    status = sweep_status or (2 if check_status == 2 else 0)
    if status == 0:
        temporary = stamp.with_suffix(f".tmp.{os.getpid()}")
        with temporary.open("w") as stream:
            json.dump({"last_success": now}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, stamp)
    raise SystemExit(status)
PY
