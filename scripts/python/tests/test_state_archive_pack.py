"""Preserve-first monthly packing, exclusively against temporary roots."""

import datetime as dt
import io
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

import state_archive as archive


NOW = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc).timestamp()
OLD = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc).timestamp()


def put(state, name, body=b"document", mtime=OLD):
    path = state / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    os.utime(path, (mtime, mtime))
    return path


def contents(pack):
    data = subprocess.run(["zstd", "-qdc", str(pack)], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar}


def test_dry_run_is_read_only_and_apply_preserves_payloads(tmp_path):
    state = tmp_path / "_state"
    names = ["board-dispatch-archive/old.json", "rescued-worker-artifacts/TASK/d-1/proof.bin"]
    old = [put(state, name) for name in names]
    recent = put(state, "board-dispatch-archive/recent.json", mtime=NOW)
    boundary = put(state, "board-dispatch-archive/boundary.json", mtime=NOW - 30 * 86400)
    bulk = [put(state, f"rescued-worker-artifacts/TASK/d-1/{name}/result.md")
            for name in archive.RESCUE_BULK_NAMES]
    symlink = state / "board-dispatch-archive/link"
    symlink.symlink_to(old[0])
    before = sorted(state.rglob("*"))
    assert archive.pack_history(state, False, now=NOW) == 2
    assert sorted(state.rglob("*")) == before
    assert all(p.exists() for p in old)
    assert archive.pack_history(state, True, now=NOW) == 2
    assert all(not p.exists() for p in old)
    assert all(p.exists() for p in [recent, boundary, *bulk])
    assert symlink.is_symlink()
    for pool, name in zip(("board-dispatch-archive", "rescued-worker-artifacts"), names):
        assert contents(state / f"_archive-{pool}-202609.tar.zst") == {name: b"document"}


def test_monthly_merge_and_crash_retry_keep_old_members(tmp_path):
    first = "board-dispatch-archive/one"
    second = "board-dispatch-archive/two"
    put(tmp_path, first)
    archive.pack_history(tmp_path, True, now=NOW)
    # Simulate a crash after publishing but before removing an original.
    put(tmp_path, first)
    put(tmp_path, second, b"second")
    assert archive.pack_history(tmp_path, True, now=NOW) == 2
    pack = tmp_path / "_archive-board-dispatch-archive-202609.tar.zst"
    assert contents(pack) == {first: b"document", second: b"second"}
    assert archive.pack_history(tmp_path, True, now=NOW) == 0
    assert contents(pack) == {first: b"document", second: b"second"}


def test_conflicting_same_name_retains_source_and_archive(tmp_path):
    name = "board-dispatch-archive/one"
    put(tmp_path, name)
    archive.pack_history(tmp_path, True, now=NOW)
    pack = tmp_path / "_archive-board-dispatch-archive-202609.tar.zst"
    before = pack.read_bytes()
    path = put(tmp_path, name, b"different")
    with pytest.raises(ValueError, match="conflicts"):
        archive.pack_history(tmp_path, True, now=NOW)
    assert path.read_bytes() == b"different"
    assert pack.read_bytes() == before


@pytest.mark.parametrize("fault", ["missing", "size", "content", "compress", "publish", "source"])
def test_failed_verification_or_publication_never_removes_sources(tmp_path, monkeypatch, fault):
    path = put(tmp_path, "board-dispatch-archive/one")
    if fault in ("missing", "size", "content"):
        original = archive._tar_inventory
        def wrong_inventory(tar):
            result = original(tar)
            if fault == "missing":
                return {}
            key = next(iter(result))
            size, digest = result[key]
            result[key] = (size + 1, digest) if fault == "size" else (size, "wrong")
            return result
        monkeypatch.setattr(archive, "_tar_inventory", wrong_inventory)
    elif fault == "compress":
        def fail(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "zstd")
        monkeypatch.setattr(archive.subprocess, "run", fail)
    elif fault == "publish":
        def fail(*args):
            raise OSError("publication failed")
        monkeypatch.setattr(archive.os, "replace", fail)
    else:
        original = archive._decompress
        def change(*args):
            original(*args)
            path.write_bytes(b"changed!")
        monkeypatch.setattr(archive, "_decompress", change)
    with pytest.raises((ValueError, OSError, subprocess.CalledProcessError)):
        archive.pack_history(tmp_path, True, now=NOW)
    assert path.exists()
    assert not list(tmp_path.glob("_archive-*.tar.zst"))
    assert not list(tmp_path.glob(".state-pack-*"))


def test_corrupt_existing_pack_is_never_replaced(tmp_path):
    path = put(tmp_path, "board-dispatch-archive/one")
    pack = tmp_path / "_archive-board-dispatch-archive-202609.tar.zst"
    pack.write_bytes(b"corrupt archive")
    with pytest.raises(subprocess.CalledProcessError):
        archive.pack_history(tmp_path, True, now=NOW)
    assert path.exists()
    assert pack.read_bytes() == b"corrupt archive"


def test_multiple_months_and_symlinked_directories(tmp_path):
    put(tmp_path, "board-dispatch-archive/september")
    put(tmp_path, "board-dispatch-archive/august", mtime=OLD - 32 * 86400)
    outside = put(tmp_path, "outside/keep")
    (tmp_path / "board-dispatch-archive/linked").symlink_to(outside.parent, target_is_directory=True)
    assert archive.pack_history(tmp_path, True, now=NOW) == 2
    assert len(list(tmp_path.glob("_archive-*.tar.zst"))) == 2
    assert outside.exists()


def test_duplicate_tar_member_is_rejected(tmp_path):
    path = tmp_path / "duplicate.tar"
    with tarfile.open(path, "w") as tar:
        for _ in range(2):
            member = tarfile.TarInfo("duplicate")
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="duplicate"):
        archive._tar_inventory(path)
