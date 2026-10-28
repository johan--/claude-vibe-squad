#!/usr/bin/env bash
# prune-board-worktrees.sh — reclaim disk from settled board worktrees WITHOUT
# destroying unpromoted worker artifacts.
#
# Why the guard exists (2026-07-30): a naive prune that deleted every worktree
# whose task had reached a terminal status permanently destroyed a 577-line
# Phase-4 chaining analysis and a 488-line economic audit. The reason is subtle
# and worth stating plainly: a `blocked` task is one whose completion envelope
# never promoted — which is exactly why its artifact still lives ONLY inside the
# worktree. Terminal-status pruning therefore targets precisely the worktrees
# whose artifacts exist nowhere else. Worker residue is not always committed, so
# `git fsck` could not recover them either. The loss was total.
#
# This script preserves first and deletes second. Deletes are operator-gated.
#
# Usage:
#   bin/prune-board-worktrees.sh            # dry run: report only
#   bin/prune-board-worktrees.sh --preserve # copy unpromoted artifacts out
#   bin/prune-board-worktrees.sh --apply    # preserve, then prune (destructive)
#   bin/prune-board-worktrees.sh --apply --task-id TASK-... # current attempt only
#   bin/prune-board-worktrees.sh --apply --sweep-tmp # also old worker scratch

set -euo pipefail
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"
cd "$VAULT_ROOT"

python3 - "$@" <<'PY'
import argparse, fcntl, hashlib, json, os, pathlib, re, shutil, stat, subprocess, sys, tempfile, time

parser = argparse.ArgumentParser(description="Preserve worker artifacts before guarded cleanup")
modes = parser.add_mutually_exclusive_group()
modes.add_argument("--apply", dest="mode", action="store_const", const="apply")
modes.add_argument("--preserve", dest="mode", action="store_const", const="preserve")
modes.add_argument("--dry-run", "--report", dest="mode", action="store_const", const="report")
parser.add_argument("--task-id")
parser.add_argument("--sweep-tmp", action="store_true")
args = parser.parse_args()
mode = args.mode or "report"
if args.task_id and (not re.fullmatch(r"TASK-[A-Za-z0-9_-]+", args.task_id) or args.sweep_tmp):
    parser.error("--task-id requires a task identifier and cannot include --sweep-tmp")
RESCUE_DIR = "_state/rescued-worker-artifacts"
TRANSCRIPT_RETENTION_DAYS = 30
TERMINAL = {"complete", "closed", "superseded", "settled", "cancelled"}
BLOCKED_STUBS = (
    "board dispatch blocked",
    "board dispatch was blocked by the controller:",
)

registry = json.load(open("_state/active-tasks.json"))
# The nightly, settlement hook and timer share the same removal lock. A busy
# run is retried by the timer; it must not stall settlement or race preservation.
if mode != "report":
    cleanup_lock = open("_state/prune-board-worktrees.lock", "a")
    try:
        fcntl.flock(cleanup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("cleanup already running; deferred to periodic sweep")
dispatch = pathlib.Path("_state/board-dispatch")
worktrees = pathlib.Path("_state/board-worktrees")
scratch_root = pathlib.Path(os.environ.get("BOARD_SCRATCH_ROOT", "/tmp/vs"))
if os.environ.get("SQUAD_TEST_ISOLATION") == "1":
    # A fixture census cannot authorize cleanup outside that fixture, even when
    # a test forgets the override or supplies a symlink to shared host scratch.
    if (not os.environ.get("BOARD_SCRATCH_ROOT")
            or not scratch_root.resolve().is_relative_to(pathlib.Path.cwd().resolve())):
        scratch_root = None

# Worker session transcripts are descriptor-owned state, not disposable
# worktree/Codex-home residue. Keep every live transcript and every settled
# transcript younger than 30 days; only the explicit --apply path expires older
# settled logs. The durable `skills` count has already been written to
# dispatch-log.jsonl by outbox-watcher before normal settlement cleanup reaches
# this script, so the aggregate survives the bounded raw-transcript window.
# Importing and planning this policy before any worktree removal makes the
# carve-out executable: a missing/broken telemetry module fails before cleanup.
sys.path.insert(0, str(pathlib.Path.cwd() / "scripts" / "python"))
from dispatch_log import DispatchLogError, enforce_transcript_retention
from disk_hygiene import RESCUE_BULK_NAMES

try:
    transcript_summary = enforce_transcript_retention(
        pathlib.Path.cwd(),
        retention_days=TRANSCRIPT_RETENTION_DAYS,
        apply=mode == "apply" and not args.task_id,
    )
except DispatchLogError as exc:
    raise SystemExit(f"transcript retention policy failed closed: {exc}")
print(
    "transcripts: "
    f"retention={transcript_summary.retention_days}d "
    f"live={transcript_summary.retained_live} "
    f"recent={transcript_summary.retained_recent} "
    f"expired={transcript_summary.expired} "
    f"removed={transcript_summary.removed} "
    f"missing={transcript_summary.missing} "
    f"invalid={transcript_summary.invalid}"
)

from board_process_truth import descriptor_error, load_json, terminal_outcome

owner, descriptors = {}, {}
for d in dispatch.glob("*.dispatch.json"):
    m = re.match(r"(TASK-.+?)\.(d-[0-9a-f]+)\.dispatch\.json", d.name)
    if m:
        owner[m.group(2)] = m.group(1)
        descriptors[m.group(2)] = d

selected_attempt = None
if args.task_id:
    selected_attempt = (registry.get(args.task_id) or {}).get("delivery_attempt_id")
    candidates = [attempt for attempt, task in owner.items() if task == args.task_id]
    if not selected_attempt and len(candidates) == 1:
        selected_attempt = candidates[0]  # legacy single-attempt task
    if selected_attempt not in candidates:
        raise SystemExit("task has no unambiguous owned attempt; cleanup retained")


def selected(attempt):
    return not args.task_id or attempt == selected_attempt


registry_identity = None


def current_registry():
    """Cache atomic registry snapshots; descriptor load_json has a 1 MiB cap.

    The campaign registry is routinely larger than that cap. Reuse a snapshot
    until its inode/size/timestamps change, then validate a fresh complete read.
    """
    global registry, registry_identity
    try:
        with open("_state/active-tasks.json") as stream:
            before = os.fstat(stream.fileno())
            identity = (before.st_dev, before.st_ino, before.st_size,
                        before.st_mtime_ns, before.st_ctime_ns)
            if identity == registry_identity:
                return registry
            value = json.load(stream)
            after = os.fstat(stream.fileno())
            if (not isinstance(value, dict) or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                return None
        registry, registry_identity = value, identity
        return registry
    except (OSError, ValueError):
        return None


def attempt_live(attempt):
    """Uncertainty retains. Even a reused PID/PGID is a reason to wait."""
    path = descriptors.get(attempt)
    value = load_json(path) if path else None
    if value is None or value.get("task_id") != owner.get(attempt):
        return True
    if value.get("schema") and descriptor_error(path, value):
        return True
    for field, probe in (("pid", os.kill), ("pgid", os.killpg)):
        pid = value.get(field)
        if pid is None:
            continue  # old descriptors did not carry process identity
        if type(pid) is not int or pid <= 0:
            return True
        try:
            probe(pid, 0)
            return True
        except ProcessLookupError:
            pass
        except OSError:
            return True
    return False


def attempt_settled(attempt):
    # Refresh the registry at each destructive boundary, not just at census.
    current = current_registry()
    task = owner.get(attempt)
    if current is None or task is None or attempt_live(attempt):
        return False
    entry = current.get(task) or {}
    if entry.get("status") in TERMINAL:
        return True
    path = descriptors[attempt]
    value = load_json(path)
    if value is None or descriptor_error(path, value):
        return False
    receipt = load_json(str(path).replace(".dispatch.json", ".receipt.json"))
    # Delivery-terminal review/blocked attempts need not wait for task closure.
    return terminal_outcome(receipt, value) is not None


def idle_path(path):
    """lsof's quiet exit 1 alone means no open files, cwd, or mapped files."""
    try:
        result = subprocess.run(["lsof", "-nP", "-t", "+D", str(path)],
                                capture_output=True, timeout=15)
        return result.returncode == 1 and not result.stdout and not result.stderr
    except (OSError, subprocess.TimeoutExpired):
        return False

def promoted(task_id):
    """True only if the declared return_artifact is real at the canonical path.

    Legacy and controller blocked placeholders count as NOT promoted: those are
    what the supervisor writes when promotion failed, and treating one as real is
    exactly how the 2026-07-30 loss happened."""
    entry = registry.get(task_id) or {}
    rel = (entry.get("return_artifact") or "").strip()
    if not rel:
        return True, "no declared artifact"
    p = pathlib.Path(rel)
    if not p.is_file():
        return False, f"missing at {rel}"
    response = p.read_text(errors="ignore").lower()
    if any(marker in response for marker in BLOCKED_STUBS):
        return False, f"stub at {rel}"
    return True, rel

# `residue()` is defined here rather than after the report exit because the
# CLASSIFICATION needs it: whether a worktree holds anything is a fact about
# the worktree, and report mode must answer it too.
def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def residue(wt):
    """Every path git reports as changed, untracked, or ignored-but-present.

    Returns a list of (path, kind) or None. None means the census itself could
    not be trusted, and the caller must RETAIN rather than remove -- a census
    that did not run is not evidence of an empty worktree.

    Three details are load-bearing, each one a way this silently lost data in
    review before it ever ran a night:

    `-z`. Porcelain v1 quotes and C-escapes any path containing a quote,
    backslash, tab or newline, and a naive `.strip('"')` produces a path that
    does not exist -- which then looks like "nothing to preserve". `-z` emits
    raw NUL-delimited names with no quoting, and also removes the need to guess
    at ` -> ` in rename lines, which an ordinary filename can contain.

    `--ignored=traditional`, not `matching`. Measured: `matching` reports an
    ignored directory as a single collapsed `scratch/` entry and omits its
    contents, so testing `is_file()` on it discards the entry, the census
    returns success, and `--force` deletes the directory whole -- taking build
    output, scratch, and any PoC inside it. `traditional` lists the individual
    files (`scratch/payload.bin`), which is what preservation needs.

    Directory expansion is still required, for a different case: a nested git
    repository or submodule reports as a bare `nested/` entry even under
    `traditional` (measured), and its contents would otherwise go uncopied.
    Mutation-tested: disabling expansion fails the nested-repo test. Reverting
    the flag to `matching` does NOT fail, because expansion then walks the
    collapsed `scratch/` entry too -- the two guards overlap deliberately, so
    losing either one alone still preserves the residue.

    Non-regular entries. A submodule appears as a directory; FIFOs and sockets
    cannot be copied at all. Skipping them silently would report a successful
    preservation of something that was never preserved, so an entry that cannot
    be captured makes the whole census untrusted.
    """
    r = subprocess.run(
        ["git", "-C", str(wt), "status", "--porcelain=v1", "-z",
         "--untracked-files=all", "--ignored=traditional"],
        capture_output=True,
    )
    # git reports "I could not look here" as a WARNING with exit 0, and emits
    # NO entry for the subtree it could not read. Measured:
    #     warning: could not open directory 'locked/': Permission denied
    #     rc=0
    # So exit-code-only made a census that ran BLIND over a subtree
    # indistinguishable from one that found the subtree empty, and the caller
    # then removed the worktree with a green "preserved 0 residue file(s)"
    # receipt. Until now that never cost a PoC only because a mode-000
    # directory also blocks `git worktree remove` -- the data survived by
    # accident of POSIX, not by design. Any cause where git cannot readdir at
    # census time but the tree is removable moments later (transient EIO, fd
    # exhaustion, a mount that reappears) loses it silently.
    if r.returncode != 0 or r.stderr.strip():
        return None

    entries, fields = [], r.stdout.split(b"\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if len(field) < 4:
            continue
        code, raw = field[:2], field[3:]
        # A rename/copy carries its ORIGIN as the next NUL field; the name we
        # must preserve is the destination, already in this field.
        if code[:1] in (b"R", b"C") or code[1:2] in (b"R", b"C"):
            i += 1
        try:
            rel = raw.decode("utf-8", "surrogateescape")
        except Exception:
            return None
        entries.append(wt / rel)

    out = []
    for entry in entries:
        try:
            st = entry.lstat()
        except FileNotFoundError:
            # Vanished between census and read: the worktree is changing under
            # us, so nothing here can be proven safe.
            return None
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            out.append((entry, "symlink"))
        elif stat.S_ISDIR(st.st_mode):
            # Collapsed ignored directory, or a submodule. Walk it: every file
            # under it is residue that would otherwise be deleted unrecorded.
            for sub in entry.rglob("*"):
                try:
                    sst = sub.lstat()
                except OSError:
                    return None
                if stat.S_ISLNK(sst.st_mode):
                    out.append((sub, "symlink"))
                elif stat.S_ISREG(sst.st_mode):
                    out.append((sub, "file"))
                elif not stat.S_ISDIR(sst.st_mode):
                    return None
        elif stat.S_ISREG(st.st_mode):
            out.append((entry, "file"))
        else:
            # FIFO, socket, device: not copyable, so not preservable.
            return None
    return out



def _system_generated(src, wt):
    """True for residue the SYSTEM wrote, never the worker's work product.

    CLASSIFICATION ONLY -- whether anything here is worth rescuing. `preserve()`
    still copies non-bulk residue when a rescue happens, so a wrong entry
    here costs a retained worktree, never a lost file.

    Measured across the 9 standing worktrees: 667 of 673 residue files were
    these. Counting them made every terminal worktree look like it held work,
    so prunable stayed permanently 0, the nightly could never clear anything,
    and the RESCUE list was 9 standing false alarms -- which is where a real
    one (untracked PoC material, often the only copy) would have been missed.

    Every entry is something the launcher or the interpreter creates, so none
    can be the only copy of anything:
      home/               the sandboxed HOME the launcher builds, holding the
                          interpreter's bytecode cache
      __pycache__, .pyc   bytecode for the repo's own modules
      .role-capabilities/ capability-probe output, rewritten per dispatch
      the launch files the supervisor writes into every worktree
    """
    # residue() yields `wt / rel`, so the worktree prefix must come off before
    # any prefix rule can match. Getting this wrong is silent: the `.pyc` suffix
    # rule still fired, ~70 of 74 entries were excluded, and the remaining
    # launch files looked like genuine worker residue.
    try:
        rel = str(pathlib.Path(src).relative_to(wt))
    except ValueError:
        # Cannot place it inside the worktree: treat as real residue, never
        # as system-generated. Fail toward keeping the worktree.
        return False
    parts = rel.split("/")
    if parts[0] in ("home", ".role-capabilities"):
        return True
    if "__pycache__" in parts or rel.endswith(".pyc"):
        return True
    return rel in (
        ".trusted-launch-request.json",
        ".trusted-launch-response.json",
        ".trusted-task-overlay.md",
        "allowed.txt",
    )


def owning_repo(wt):
    """Follow the worktree's gitdir/commondir pointers, never the squad cwd.

    Let Git resolve relative pointers and separate Git directories. Requiring a
    real worktree gitfile prevents repository discovery from falling back to
    the enclosing squad checkout when a board entry is broken or a symlink.
    """
    if wt.is_symlink() or not wt.is_dir() or (wt / ".git").is_symlink() \
            or not (wt / ".git").is_file():
        return None, "missing regular worktree .git file"
    r = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None, r.stderr.strip() or "cannot resolve owning repository"
    try:
        common = pathlib.Path(r.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        return None, str(exc)
    # A conventional checkout runs from its root. Bare/separate Git directory
    # layouts can run worktree commands directly from the common Git directory.
    return (common.parent if common.name == ".git" else common), ""


def prepare_removal(wt):
    """Make only this worktree's directories owner-writable; estimate its blocks.

    POSIX removal needs writable parent directories, not writable files. Using
    directory fds avoids chmod through symlinks, including links in a module
    cache; leaving regular files alone also protects hardlinks outside the tree.
    This runs only after preservation succeeds, and only under --apply.
    """
    if wt.is_symlink():
        raise OSError("worktree became a symlink")
    if wt.parent == worktrees and not attempt_settled(wt.name):
        raise OSError("attempt is live or no longer settled")
    if not idle_path(wt):
        raise OSError("open files or lsof could not prove path idle")
    if wt.name in descriptors and not attempt_settled(wt.name):
        raise OSError("attempt changed during open-file census")

    def fail(exc):
        raise exc

    blocks, seen = 0, set()
    device = wt.stat().st_dev
    for _, dirs, files, fd in os.fwalk(wt, follow_symlinks=False, onerror=fail):
        directory = os.fstat(fd)
        if directory.st_dev != device or directory.st_uid != os.getuid():
            raise OSError("foreign mount or directory owner; retained")
        if not directory.st_mode & stat.S_IWUSR:
            os.fchmod(fd, directory.st_mode | stat.S_IWUSR)
        blocks += directory.st_blocks
        for name in dirs + files:
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            identity = (entry.st_dev, entry.st_ino)
            if not stat.S_ISDIR(entry.st_mode) and identity not in seen:
                blocks += entry.st_blocks
                seen.add(identity)
    return blocks * 512


def report_repos():
    for repo, counts in sorted(repo_counts.items(), key=lambda row: str(row[0])):
        print(f"repo: {repo or '<unresolved>'}  prunable={counts['prunable']}  "
              f"keep-live={counts['keep-live']}  NEEDS-RESCUE={counts['rescue']}  "
              f"removed={counts['removed']}  reclaimed={counts['bytes'] / 2**30:.6f} GiB")


prunable, keep, rescue = [], [], []
for wt in sorted(worktrees.glob("d-*")):
    if not selected(wt.name):
        continue
    task = owner.get(wt.name)
    if task is None or not attempt_settled(wt.name):
        keep.append(wt)
        continue
    ok, why = promoted(task)
    if ok:
        prunable.append((wt, task, why))
        continue
    # A missing artifact is a fact about the REGISTRY. Whether anything would
    # be lost is a fact about the WORKTREE, and only the second one decides a
    # rescue. A cancelled task never had an artifact and a canary never
    # produced one; judging those by the registry alone flagged them
    # NEEDS-RESCUE forever, so the nightly could never clear them and the real
    # rescues drowned in the standing false alarms.
    found = residue(wt)
    if found is not None:
        found = [row for row in found if not _system_generated(row[0], wt)]
    if found is None:
        # Fail closed. A census that could not run is not evidence of empty.
        rescue.append((wt, task, f"{why}; residue census untrusted"))
    elif found:
        rescue.append((wt, task, f"{why}; {len(found)} residue file(s)"))
    else:
        prunable.append((wt, task, f"{why}; worktree holds no residue"))

print(f"worktrees: {len(prunable)+len(keep)+len(rescue)}  prunable={len(prunable)}  "
      f"keep-live={len(keep)}  NEEDS-RESCUE={len(rescue)}")
repos, repo_errors, repo_counts = {}, {}, {}
for category, trees in (
    ("prunable", [row[0] for row in prunable]),
    ("keep-live", keep),
    ("rescue", [row[0] for row in rescue]),
):
    for wt in trees:
        repo, error = owning_repo(wt)
        repos[wt.name], repo_errors[wt.name] = repo, error
        counts = repo_counts.setdefault(repo, dict(prunable=0, rescue=0, removed=0,
                                                   bytes=0, **{"keep-live": 0}))
        counts[category] += 1
for wt, task, why in prunable:
    print(f"  PRUNABLE  {wt.name}  {task}  (repo={repos[wt.name] or '<unresolved>'}; {why})")
for wt, task, why in rescue:
    print(f"  RESCUE  {wt.name}  {task}  ({why})")
for name, error in repo_errors.items():
    if error:
        print(f"  RETAINED (owning repository unresolved)  {name}  ({error})")

if mode == "report":
    report_repos()
    print("\nreport only — rerun with --preserve or --apply")
    sys.exit(0)

def rescue_selection(wt, found):
    """Select documents and measure excluded directory trees without following links."""
    kept, bulk = [], set()
    for src, kind in found:
        relative = src.relative_to(wt)
        for index, part in enumerate(relative.parts[:-1]):
            if part in RESCUE_BULK_NAMES:
                bulk.add(wt.joinpath(*relative.parts[:index + 1]))
                break
        else:
            kept.append((src, kind))
    skipped = []
    def fail(exc):
        raise exc
    for directory in sorted(bulk):
        size = 0
        for parent, dirs, files in os.walk(directory, followlinks=False, onerror=fail):
            for name in dirs + files:
                info = (pathlib.Path(parent) / name).lstat()
                if not stat.S_ISDIR(info.st_mode):
                    size += info.st_size
        skipped.append({"path": str(directory.relative_to(wt)), "size_bytes": size})
    return kept, skipped


def preserve(wt, task, attempt, out, *, verify_only=False):
    """Copy non-bulk residue and record bulk omissions. Returns (ok, count).

    ok is False when the census failed, a copy failed, or an existing
    destination holds DIFFERENT bytes. The caller must then retain the
    worktree: partial preservation is not preservation.

    Destinations are keyed by task AND attempt. Keying on task alone made two
    attempts at one task collide, so the second silently "preserved" nothing
    and the source was deleted.

    An existing destination is verified by digest rather than trusted. The
    previous version treated `dest.exists()` as success without reading it, so
    a copy truncated by a full disk was skipped on the next run and the intact
    source removed.
    """
    found = residue(wt)
    if found is None:
        return False, 0
    try:
        found, skipped = rescue_selection(wt, found)
        if skipped:
            # A sibling of the attempt tree cannot collide with worker residue.
            manifest = out / task / f"{attempt}.rescue-manifest.json"
            if any(p.is_symlink() for p in (manifest, *manifest.parents)):
                return False, 0
            value = {"task": task, "attempt": attempt, "skipped_bulk": skipped}
            if verify_only:
                if json.loads(manifest.read_text()) != value:
                    return False, 0
            else:
                manifest.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".rescue-manifest-", dir=manifest.parent)
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(value, stream, sort_keys=True)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, manifest)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
    except (OSError, ValueError):
        return False, 0
    n = 0
    for src, kind in found:
        dest = out / task / attempt / src.relative_to(wt)
        try:
            # A rescue must be an independent copy, never a link back into the
            # worktree (including a link in a destination parent directory).
            if any(p.is_symlink() for p in (dest.parent, *dest.parents)):
                return False, n
            if not verify_only:
                dest.parent.mkdir(parents=True, exist_ok=True)
            if kind == "symlink":
                target = os.readlink(src)
                if dest.is_symlink():
                    if os.readlink(dest) != target:
                        return False, n
                else:
                    if dest.exists() or verify_only:
                        return False, n
                    os.symlink(target, dest)
                if not dest.is_symlink() or os.readlink(dest) != os.readlink(src):
                    return False, n
            else:
                before = src.lstat()
                if not stat.S_ISREG(before.st_mode) or dest.is_symlink():
                    return False, n
                if not dest.exists() and not verify_only:
                    shutil.copy2(src, dest, follow_symlinks=False)
                    with dest.open("rb") as stream:
                        os.fsync(stream.fileno())
                # Verify NEW copies too: a successful copy call is not proof
                # of intact bytes when disk space is exhausted.
                copied = dest.lstat()
                if (not stat.S_ISREG(copied.st_mode)
                        or (copied.st_dev, copied.st_ino) == (before.st_dev, before.st_ino)
                        or copied.st_size != before.st_size or _digest(dest) != _digest(src)):
                    return False, n
                after = src.lstat()
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != \
                        (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    return False, n
            n += 1
        except OSError:
            return False, n
    return True, n


# Preservation covers EVERY terminal worktree, not just the rescue list.
#
# The previous version preserved only `rescue` -- worktrees whose declared
# artifact was missing -- and then force-removed every `prunable` one without
# ever looking inside it. But `promoted()` proves one declared path exists; it
# proves nothing about untracked residue beside it. A promoted worktree holding
# the only copy of a PoC was deleted with no census and no copy. That is the
# 2026-07-30 loss shape the header warns about, still live on the other branch
# of the same function.
# ONE loop over both lists, deliberately. The defect above was preservation
# living on one branch of this fork and not the other, and two loops doing the
# same thing is how they drifted apart in the first place. Iterating the union
# makes that divergence unwriteable: a worktree cannot reach removal without
# having been through the same census as a rescued one.
saved = 0
out = pathlib.Path(RESCUE_DIR)
unsafe = []
for wt, task, _ in rescue + prunable:
    ok, n = preserve(wt, task, wt.name, out) if attempt_settled(wt.name) else (False, 0)
    saved += n
    if not ok:
        unsafe.append(wt.name)
# Incomplete preservation withdraws the worktree from pruning. (Names are the
# worktree directory names from one glob, so they are unique across both lists.)
prunable = [row for row in rescue + prunable if row[0].name not in set(unsafe)]
print(f"preserved {saved} residue file(s) under {RESCUE_DIR}/")
if unsafe:
    print(f"RETAINED (preservation incomplete): {', '.join(sorted(set(unsafe)))}")

if mode == "preserve":
    report_repos()
    sys.exit(0)

removed = 0
removed_names = set()
refused = []
preflight_failed = []
prune_failed = []
attempted_repos = set()
for wt, task, _ in prunable:
    repo = repos[wt.name]
    # Recheck after copying: a changed or unresolved owner must never acquire
    # permissions or reach removal using a stale repository/path pairing.
    current_repo, error = owning_repo(wt)
    if repo is None or current_repo != repo:
        preflight_failed.append((wt.name, error or "owning repository changed"))
        continue
    try:
        size = prepare_removal(wt)
        # Re-census at the destructive boundary. A removed rescue copy, new
        # residue, or changed bytes after preservation must retain the source.
        if not preserve(wt, task, wt.name, out, verify_only=True)[0]:
            raise OSError("preservation verification failed before removal")
        if not attempt_settled(wt.name):
            raise OSError("attempt became live before removal")
    except OSError as exc:
        preflight_failed.append((wt.name, str(exc)))
        continue
    attempted_repos.add(repo)
    r = subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt.resolve())],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # RETAIN, never path-delete. The previous fallback here was
        # `shutil.rmtree(wt, ignore_errors=True)`, which turned git's refusal
        # into an unconditional recursive delete -- and git refuses precisely
        # when it sees state it will not discard. Suppressing that refusal
        # removed the last thing standing between a surprised worktree and
        # permanent loss. A retained worktree costs disk; a deleted one can
        # cost work that exists nowhere else.
        refused.append((wt.name, r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"))
        continue
    removed += 1
    removed_names.add(wt.name)
    repo_counts[repo]["removed"] += 1
    repo_counts[repo]["bytes"] += size
for name, why in preflight_failed:
    print(f"  RETAINED (removal preflight failed)  {name}  ({why})")
if refused:
    for name, why in refused:
        print(f"  RETAINED (git refused removal)  {name}  ({why})")
for repo in sorted(attempted_repos):
    if mode == "apply" and args.task_id:
        continue  # task settlement cannot expire another attempt's Git metadata
    r = subprocess.run(["git", "-C", str(repo), "worktree", "prune"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        prune_failed.append(repo)
        print(f"  FAILED (git worktree prune)  {repo}  ({r.stderr.strip() or 'unknown'})")
report_repos()
print(f"pruned {removed}; rescued-and-retained "
      f"{sum(wt.name not in removed_names for wt, _, _ in rescue)}; live retained {len(keep)}")
reclaimed = sum(counts["bytes"] for counts in repo_counts.values())
print(f"estimated reclaimed {reclaimed / 2**30:.6f} GiB "
      "(removed worktree allocated blocks; not net filesystem free space)")

# Sweep orphaned per-attempt build scratch (/tmp/vs/<attempt>).
#
# board-supervisor.sh points TMPDIR, GOCACHE and CARGO_TARGET_DIR at this root so
# build caches die with their attempt. The sweep exists because the create path
# and the cleanup path are not the same path: a lane that times out, is killed,
# or blocks never runs an orderly teardown, so hooking removal onto a single exit
# would leak exactly the attempts that fail. The worktree census is only one
# guard: local descriptor ownership and terminal status are also required.
#
# Unlike a worktree, scratch NEVER holds an unpromoted artifact -- it is build
# cache by construction. An absent local worktree does not prove that shared
# scratch is orphaned: another vault or a test fixture has a different census.
#
# board-codex-homes is swept by the same rule and for the same reason: it is also
# created per attempt (board-supervisor.sh, _prepare_codex_home) and also had no
# reclaim path, leaving 63 orphans. It is small -- config.toml unions, not caches
# -- so this is tidiness rather than disk, but it is the identical defect and
# fixing only the expensive instance would leave the pattern alive.
live_attempts = {p.name for p in worktrees.glob("d-*") if p.is_dir()}
scratch_failed = []
for label, root, unit, suffix in (
    ("scratch", scratch_root, 2**30, "GiB"),
    ("codex-homes", pathlib.Path("_state/board-codex-homes"), 2**20, "MiB"),
):
    if root is None or not root.is_dir():
        continue
    swept = swept_bytes = 0
    for d in root.iterdir():
        # Require this vault's descriptor and terminal registry ownership. A
        # missing local worktree is never evidence that another vault's attempt
        # is dead. Unknown roots are retained for operator inspection.
        task = owner.get(d.name)
        if (not selected(d.name) or d.is_symlink() or not d.is_dir() or d.name in live_attempts
                or task is None or not attempt_settled(d.name)):
            continue
        try:
            if label == "scratch":
                from disk_hygiene import tree_usage, LiveReferences, Retained, process_idle, tmp_policy
                policy = tmp_policy(pathlib.Path.cwd())
                try:
                    tree_usage(d, time.time() - 12 * 3600, protect=True, policy=policy)
                except Retained as exc:
                    print(f"  RETAINED (scratch policy)  {d}  ({exc})")
                    continue
                references = LiveReferences(pathlib.Path.cwd())
                if not references.unreferenced(d.name):
                    continue
                if not process_idle(d):
                    raise OSError("open files or process/lsof could not prove scratch idle")
            size = prepare_removal(d)
            if label == "scratch":
                tree_usage(d, time.time() - 12 * 3600, protect=True, policy=policy)
                if not references.unreferenced(d.name):
                    continue
            shutil.rmtree(d)
        except OSError as exc:
            scratch_failed.append(d)
            print(f"  RETAINED (scratch removal failed)  {d}  ({exc})")
            continue
        swept_bytes += size
        swept += 1
    print(f"{label}: swept {swept} orphaned attempt root(s), {swept_bytes / unit:.1f} {suffix}")

# Periodic and explicit scratch cleanup share one allowlist and live-reference
# policy. Unknown/user paths and the rescued archive never enter this sweep.
if mode == "apply" and args.sweep_tmp:
    from disk_hygiene import sweep_test_tmp
    try:
        sweep_test_tmp(pathlib.Path.cwd(), time.time())
    except (OSError, subprocess.TimeoutExpired) as exc:
        scratch_failed.append(str(exc))
        print(f"  RETAINED (tmp sweep unavailable)  ({exc})")

# Report every retained item and finish the summaries before signalling failure.
sys.exit(1 if refused or unsafe or preflight_failed or prune_failed or scratch_failed else 0)
PY
