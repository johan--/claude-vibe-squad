#!/usr/bin/env python3
"""Move settled board history out of the paths every monitor rescans.

The registry (_state/active-tasks.json) and _state/board-dispatch/ are reread
in full by the reconcile sweep, the board dashboard and the squad monitor.
Nothing ever moved finished work out, so by 2026-10-04 the registry held 4,991
entries (1 live, 27 MB) and board-dispatch held 12,858 files (2.1 GB); every
reader overran its interval, overlapped and stalled the board.

This preserves settled history:
  - registry entries whose status is settled and whose task date is older than
    --registry-days, into _state/active-tasks-archive.json;
  - board-dispatch files of settled tasks older than --dispatch-days with no
    live process, into _state/board-dispatch-archive/.
  - archive files older than 30 days into verified monthly tar.zst packs,
    removing originals only after publication and verification of the pack.
Default is a dry run; pass --apply to move and pack.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

from disk_hygiene import RESCUE_BULK_NAMES

ROOT = Path(os.environ.get("VAULT_ROOT", Path(__file__).resolve().parents[2]))
STATE = ROOT / "_state"
REGISTRY = STATE / "active-tasks.json"
REGISTRY_LOCK = STATE / "active-tasks.json.lock"
REGISTRY_ARCHIVE = STATE / "active-tasks-archive.json"
DISPATCH = STATE / "board-dispatch"
DISPATCH_ARCHIVE = STATE / "board-dispatch-archive"

# Same settled vocabulary as disk_hygiene.SETTLED.
SETTLED = {"complete", "completed", "closed", "superseded", "auto-closed", "settled", "cancelled"}
TASK_DATE = re.compile(r"^TASK-(\d{4}-\d{2}-\d{2})")


def _status(entry) -> str:
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("status", "")).lower().replace("_", "-")


def _task_date(task_id: str):
    m = TASK_DATE.match(task_id)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _has_pending_events(entry: dict) -> bool:
    # Retry consumers only scan the active registry. Unknown shapes must stay
    # visible for repair; only an absent queue or an empty object is drained.
    for field in ("notice_pending_events", "notification_pending_events"):
        if field in entry:
            pending = entry[field]
            if not isinstance(pending, dict) or pending:
                return True
    return False


def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def archive_registry(cutoff: dt.date, apply: bool) -> tuple[int, int, dict]:
    """Returns (moved, kept, merged-status-map) under the registry lock."""
    with REGISTRY_LOCK.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        live = json.loads(REGISTRY.read_text(encoding="utf-8"))
        archived = {}
        if REGISTRY_ARCHIVE.exists():
            archived = json.loads(REGISTRY_ARCHIVE.read_text(encoding="utf-8"))
        move = {
            k: v for k, v in live.items()
            if _status(v) in SETTLED and (_task_date(k) or cutoff) < cutoff
            and not _has_pending_events(v)
        }
        statuses = {k: _status(v) for k, v in archived.items()}
        statuses.update({k: _status(v) for k, v in live.items()})
        if apply and move:
            archived.update(move)
            # Archive first: a crash between the two writes duplicates an
            # entry, it never loses one.
            _atomic_write_json(REGISTRY_ARCHIVE, archived)
            _atomic_write_json(REGISTRY, {k: v for k, v in live.items() if k not in move})
        return len(move), len(live) - len(move), statuses


def archive_dispatch(cutoff: dt.date, statuses: dict, apply: bool) -> tuple[int, int, int]:
    """Returns (files moved, tasks moved, tasks kept)."""
    groups: dict[str, list[Path]] = {}
    for path in DISPATCH.iterdir():
        if not path.name.startswith("TASK-") or not path.is_file():
            continue
        task = path.name.split(".", 1)[0]
        groups.setdefault(task, []).append(path)
    moved_files = moved_tasks = kept = 0
    if apply:
        DISPATCH_ARCHIVE.mkdir(exist_ok=True)
    for task, files in groups.items():
        date = _task_date(task)
        if date is None or date >= cutoff or statuses.get(task) not in SETTLED:
            kept += 1
            continue
        live = False
        for f in files:
            if f.name.endswith(".dispatch.json"):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    d = {}
                if _alive(d.get("pid")):
                    live = True
        if live:
            kept += 1
            continue
        moved_tasks += 1
        moved_files += len(files)
        if apply:
            for f in files:
                try:
                    os.replace(f, DISPATCH_ARCHIVE / f.name)
                except FileNotFoundError:
                    # Another sweep removed it first; nothing left to move.
                    moved_files -= 1
    return moved_files, moved_tasks, kept


def _identity(path: Path) -> tuple:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or any(p.is_symlink() for p in path.parents):
        raise ValueError(f"not an independent regular path: {path}")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _stream_digest(stream) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _tar_inventory(path: Path) -> dict:
    """Read every payload, rejecting duplicate names and non-file members."""
    inventory = {}
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = Path(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or member.name in inventory):
                raise ValueError(f"unsafe or duplicate archive member: {member.name}")
            with archive.extractfile(member) as stream:
                inventory[member.name] = (member.size, _stream_digest(stream))
    return inventory


def _decompress(source: Path, destination: Path) -> None:
    with destination.open("wb") as stream:
        subprocess.run(["zstd", "-q", "-d", "-c", "--", str(source)],
                       stdout=stream, stderr=subprocess.PIPE, check=True)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _pack_month(state: Path, pool: str, month: str, files: list[Path]) -> int:
    """Merge, verify, publish durably, recheck sources, then remove originals."""
    destination = state / f"_archive-{pool}-{month}.tar.zst"
    previous = _identity(destination) if destination.exists() or destination.is_symlink() else None
    snapshots = {p: _identity(p) for p in files}
    with tempfile.TemporaryDirectory(prefix=".state-pack-", dir=state) as temporary:
        scratch = Path(temporary)
        old_tar, new_tar = scratch / "old.tar", scratch / "new.tar"
        expected = {}
        if previous is not None:
            _decompress(destination, old_tar)
            expected = _tar_inventory(old_tar)
        with tarfile.open(new_tar, "w", dereference=True) as output:
            if previous is not None:
                with tarfile.open(old_tar, "r:") as old:
                    for member in old:
                        with old.extractfile(member) as stream:
                            output.addfile(member, stream)
            for path in files:
                name = path.relative_to(state).as_posix()
                with path.open("rb") as stream:
                    value = (snapshots[path][2], _stream_digest(stream))
                    if name in expected:
                        if expected[name] != value:
                            raise ValueError(f"archive member conflicts with source: {name}")
                    else:
                        stream.seek(0)
                        output.addfile(output.gettarinfo(str(path), arcname=name), stream)
                        expected[name] = value
                if _identity(path) != snapshots[path]:
                    raise ValueError(f"source changed while packing: {path}")
        packed = scratch / "pack.tar.zst"
        with packed.open("wb") as stream:
            subprocess.run(["zstd", "-q", "-c", "--", str(new_tar)],
                           stdout=stream, stderr=subprocess.PIPE, check=True)
            stream.flush()
            os.fsync(stream.fileno())
        # A pack may combine private documents; never broaden read access.
        packed.chmod(0o600)
        verified_tar = scratch / "verified.tar"
        _decompress(packed, verified_tar)
        if _tar_inventory(verified_tar) != expected:
            raise ValueError("packed member list, sizes or contents do not match")
        current = _identity(destination) if destination.exists() or destination.is_symlink() else None
        if current != previous:
            raise ValueError(f"archive changed while packing: {destination}")
        # Verify ALL sources before deleting any; a changed source keeps the batch.
        for path in files:
            if _identity(path) != snapshots[path]:
                raise ValueError(f"source changed before publication: {path}")
        os.replace(packed, destination)
        _sync_directory(state)
        for path in files:
            if _identity(path) != snapshots[path]:
                raise ValueError(f"source changed before removal: {path}")
            path.unlink()
        for parent in {p.parent for p in files}:
            _sync_directory(parent)
    return len(files)


def pack_history(state: Path, apply: bool, *, now: float | None = None) -> int:
    """Pack regular files by UTC mtime month; retain symlinks and rescue bulk.

    All non-bulk rescue files count as documents, regardless of extension.
    Existing monthly members are retained forever; conflicting names fail closed.
    The prune lock serializes packs with rescue writers and other pack runs.
    """
    cutoff = (time.time() if now is None else now) - 30 * 86400
    if not state.exists():
        return 0
    if state.is_symlink():
        raise ValueError(f"state root is a symlink: {state}")
    # Dry runs neither acquire a file lock nor create any filesystem entries.
    if apply:
        with (state / "prune-board-worktrees.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            return _pack_history(state, cutoff, apply)
    return _pack_history(state, cutoff, apply)


def _pack_history(state: Path, cutoff: float, apply: bool) -> int:
    count = 0
    for pool in ("board-dispatch-archive", "rescued-worker-artifacts"):
        root = state / pool
        if root.is_symlink():
            raise ValueError(f"archive source is a symlink: {root}")
        groups: dict[str, list[Path]] = {}
        def fail(exc):
            raise exc
        for parent, dirs, files in os.walk(root, followlinks=False, onerror=fail) if root.exists() else ():
            dirs[:] = sorted(d for d in dirs if not (Path(parent) / d).is_symlink()
                             and (pool != "rescued-worker-artifacts" or d not in RESCUE_BULK_NAMES))
            for name in sorted(files):
                path = Path(parent) / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_mtime >= cutoff:
                    continue
                month = dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc).strftime("%Y%m")
                groups.setdefault(month, []).append(path)
        for month, files in sorted(groups.items()):
            total = _pack_month(state, pool, month, files) if apply else len(files)
            count += total
            verb = "packed" if apply else "would pack"
            print(f"{pool}: {verb} {total} files into _archive-{pool}-{month}.tar.zst")
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--registry-days", type=int, default=2)
    ap.add_argument("--dispatch-days", type=int, default=1)
    args = ap.parse_args()
    today = dt.date.today()
    r_moved, r_kept, statuses = archive_registry(today - dt.timedelta(days=args.registry_days), args.apply)
    d_files, d_tasks, d_kept = archive_dispatch(today - dt.timedelta(days=args.dispatch_days), statuses, args.apply)
    verb = "moved" if args.apply else "would move"
    print(f"registry: {verb} {r_moved} settled entries, {r_kept} remain")
    print(f"board-dispatch: {verb} {d_files} files from {d_tasks} settled tasks, {d_kept} tasks remain")
    pack_history(STATE, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
