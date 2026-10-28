"""One home for "a checkout ``bin/send-task.sh`` will actually dispatch from".

``send-task.sh`` refuses to dispatch from a linked git worktree: there the task
registry is a per-worktree stub, so its ``write_scope`` conflict check compares
against ~1 entry instead of the real ~1900 and reports "no conflicts" because it
can no longer see any; and lanes would branch off a branch the coordinator
rebases, which killed a finished lane at integration on 2026-08-15.

That refusal is correct in production, but it runs *before* every other guard in
the script. So any suite that drives the real ``send-task.sh`` becomes red or
green depending only on where the repository happens to be checked out --
a result that depends on the environment rather than on the behaviour under
test. Three suites were affected: ``test_reviewer_dispatch_recursion`` (7
failures), ``test_board_dispatch`` (1) and ``test_host_admission`` (1).

The fix is not to weaken the guard or to special-case it in each suite, but to
give the tests the shape production actually has: a normal checkout. This module
builds one throwaway normal repo per process, from the tree under test, and
hands the same path to every caller.

Use it as::

    from dispatch_checkout import normal_checkout_root
    ROOT = normal_checkout_root(Path(__file__).resolve().parents[3])
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_CACHE: dict[Path, Path] = {}
_TMPDIRS: list[tempfile.TemporaryDirectory] = []
_PENDING_DIAGNOSTICS: dict[Path, str] = {}


def _is_linked_worktree(root: Path) -> bool:
    """True when ``root`` is a linked worktree rather than a main checkout.

    A main checkout has ``--git-dir`` == ``--git-common-dir``; a linked worktree
    points its git-dir at ``.git/worktrees/<name>`` while the common dir stays
    at the parent. This is the same predicate ``send-task.sh`` applies.
    """
    def _rev_parse(flag: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", flag],
            capture_output=True, text=True, check=False, timeout=10,
        ).stdout.strip()

    git_dir = _rev_parse("--git-dir")
    common_dir = _rev_parse("--git-common-dir")
    # An empty result means "not a git repo at all" -- not a worktree, so leave
    # the caller's root alone rather than silently substituting a copy.
    return bool(git_dir) and bool(common_dir) and git_dir != common_dir


def _checkout_diagnostic(message: str) -> bool:
    """Bypass Python captures, with an fd fallback if the original stream fails."""
    try:
        if sys.__stderr__ is None:
            raise ValueError("original stderr is unavailable")
        print(message, file=sys.__stderr__, flush=True)
        return True
    except Exception as exc:
        # Diagnostics must not raise into collection, even with a broken stream.
        fallback = (
            f"!! Checkout diagnostic stderr failed ({type(exc).__name__}); using fd 2.\n"
            f"{message}\n"
        )
    try:
        pending = fallback.encode("ascii", errors="backslashreplace")
        while pending:
            written = os.write(2, pending)
            if written <= 0:
                return False
            pending = pending[written:]
        return True
    except Exception:
        # If fd 2 is also gone, no diagnostic can be delivered safely. Do not
        # fall back to stdout or sys.stderr: tests may assert on those streams.
        return False


def _warn_ignored_changes(root: Path) -> None:
    """Report HEAD substitution without affecting captured test output or results."""
    operation = "git status"
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=no"],
            capture_output=True, check=False, timeout=10,
        )
        if status.returncode:
            raise ValueError(f"exit {status.returncode}")
        operation = "parse git status"
        paths: set[bytes] = set()
        records = iter(status.stdout.split(b"\0")[:-1])
        if status.stdout and not status.stdout.endswith(b"\0"):
            raise ValueError("unterminated porcelain output")
        for record in records:
            if len(record) < 4 or record[2:3] != b" ":
                raise ValueError("malformed porcelain record")
            paths.add(record[3:])
            # With -z, a rename/copy has destination then source as separate
            # records; neither path is quoted, even with tabs or newlines.
            if b"R" in record[:2] or b"C" in record[:2]:
                source = next(records, b"")
                if not source:
                    raise ValueError("missing rename/copy source")
                paths.add(source)
        if not paths:
            return
        operation = "git rev-parse HEAD"
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        if head.returncode:
            raise ValueError(f"exit {head.returncode}")
        revision = head.stdout.strip()
        if len(revision) not in (40, 64) or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("invalid HEAD object id")
        message = (
            f"!! TESTING COMMITTED HEAD {revision}.\n"
            f"!! Ignoring changes to {len(paths)} tracked path(s) in {str(root)!r}.\n"
            "!! Uncommitted changes are INVISIBLE to this suite; commit them to test them."
        )
    except Exception as exc:
        message = (
            f"!! Could not determine tracked-change status in {str(root)!r} "
            f"({operation}: {type(exc).__name__}: {str(exc)!r}). "
            "HEAD substitution still proceeds; this is NOT a clean-tree measurement."
        )
    if not _checkout_diagnostic(message):
        # Retain the original measurement: a later status read could look clean
        # even though this cached checkout still omitted the earlier edits.
        _PENDING_DIAGNOSTICS[root] = message
    else:
        _PENDING_DIAGNOSTICS.pop(root, None)


def normal_checkout_root(root: Path) -> Path:
    """Return ``root`` if it is already a main checkout, else a copy that is one.

    The copy is built from ``git archive HEAD``, so it carries exactly the
    committed tree under test -- never the working-tree state, which would make
    the tests depend on uncommitted edits.
    """
    root = Path(root).resolve()
    if root in _CACHE:
        pending = _PENDING_DIAGNOSTICS.get(root)
        if pending is not None and _checkout_diagnostic(pending):
            _PENDING_DIAGNOSTICS.pop(root, None)
        return _CACHE[root]
    if not _is_linked_worktree(root):
        _CACHE[root] = root
        return root

    tmp = tempfile.TemporaryDirectory(prefix="vs-normal-checkout-")
    _TMPDIRS.append(tmp)
    repo = Path(tmp.name) / "repo"
    repo.mkdir()
    archive = Path(tmp.name) / "tree.tar"
    subprocess.run(
        ["git", "-C", str(root), "archive", "HEAD", "-o", str(archive)],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(["tar", "-xf", str(archive), "-C", str(repo)], check=True, timeout=10)
    for cmd in (
        ["git", "-c", "core.looseCompression=0", "init", "-q", "."],
        ["git", "-c", "core.looseCompression=0", "add", "-A"],
        ["git", "-c", "core.looseCompression=0", "-c", "user.email=tests@vibesquad.local", "-c", "user.name=tests",
         "commit", "-q", "-m", "test checkout"],
    ):
        subprocess.run(cmd, cwd=str(repo), check=False, capture_output=True, timeout=60)
    _warn_ignored_changes(root)
    _CACHE[root] = repo
    return repo


@atexit.register
def _cleanup() -> None:
    for tmp in _TMPDIRS:
        try:
            tmp.cleanup()
        except OSError:
            pass
    _TMPDIRS.clear()
