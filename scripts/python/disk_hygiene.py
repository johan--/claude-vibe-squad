"""Guarded periodic retention for storage outside disposable board worktrees."""

import fcntl
from fnmatch import fnmatchcase
import ctypes
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time


GIB = 2**30
DAY = 86400
RESCUE_TERMINAL = {"complete", "completed", "closed", "superseded", "auto-closed"}
# Broad product-prefixed and f2-* names may hold user data. Only recognisable
# regenerable pools and descriptor-backed scratch markers opt into cleanup.
CACHE_TMP_PATTERNS = ("*-go-cache", "*-gocache")
SCRATCH_TMP_PATTERNS = ("vs-worker-*", "vs-review-*",
                        "vs-verify-*", "vs-frames-*")
SETTLED = RESCUE_TERMINAL | {"settled", "cancelled"}
# Keep the age toolchain family conservatively, including caches under it.
# Host-specific tool pins belong in the private tmp.pinned_patterns policy.
PINNED_TMP_PATTERNS = ("*age*toolchain*", "age", "age-*")
# Disk-cleanup skill pools (explicit `bin/disk-hygiene.sh --plan/--apply-*`).
VAULT_SNAPSHOT_KEEP = 3
SNAPSHOT_SUFFIXES = (".tar", ".tgz", ".tar.gz", ".tar.zst", ".tar.xz", ".tar.bz2")
# Regenerable build/cache subtrees INSIDE a rescued artifact. Everything else in
# a rescue is treated as possibly-sole work product and never auto-deleted; only
# a subtree whose directory name is one of these is Tier-2 reclaimable.
RESCUE_BULK_NAMES = frozenset({
    ".cache", "dist", "build", "node_modules", "target", ".next",
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".cargo",
})
# Stopped containers younger than this may be a release rehearsal the operator
# still wants to inspect; never prune them automatically.
DOCKER_CONTAINER_MIN_AGE = "24h"


class Retained(OSError):
    """A failed safety check is a reason to keep the entire tree."""


LOCAL_CONFIG_REL = "_state/disk-hygiene.local.json"


def local_config_section(root, section):
    """Load private host policy without logging its values or guessing defaults."""
    try:
        value = json.loads((root / LOCAL_CONFIG_REL).read_text())
        if not isinstance(value, dict) or not isinstance(value.get(section), dict):
            raise ValueError("missing section")
        return value[section]
    except (OSError, ValueError):
        print(f"{section}: SKIP (local configuration absent, unreadable or invalid)", flush=True)
        return None


def tmp_policy(root):
    value = local_config_section(root, "tmp")
    if value is None:
        return None
    try:
        for key in ("cache_patterns", "scratch_patterns", "pinned_patterns"):
            patterns = value[key]
            if (not isinstance(patterns, list) or not patterns
                    or any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9_.*?\[\]-]+", p)
                           or not re.search(r"[A-Za-z0-9]", p) for p in patterns)):
                raise ValueError("invalid basename patterns")
        profile = value["protected_profile_substring"]
        if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
            raise ValueError("missing protected profile substring")
        return value
    except (KeyError, ValueError):
        print("tmp: SKIP (invalid local selection or protection policy)", flush=True)
        return None


def postgres_policy(root):
    value = local_config_section(root, "postgres")
    if value is None:
        return None
    try:
        directory, database = value["data_directory"], value["database"]
        superuser = value["superuser"]
        if (not isinstance(directory, str) or not directory.strip()
                or not (directory.startswith("~/") or Path(directory).is_absolute())
                or ".." in Path(directory).parts or len(Path(directory).parts) < 3
                or not isinstance(database, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", database)
                or database in {"postgres", "template0", "template1"}
                or not isinstance(superuser, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", superuser)):
            raise ValueError("invalid test cluster identity")
        return value
    except (KeyError, ValueError):
        print("postgres: SKIP (invalid local test cluster policy; "
              "data_directory, database and superuser required)", flush=True)
        return None


def fixture_path(root, path, variable):
    if os.environ.get("SQUAD_TEST_ISOLATION") == "1" and (
        not os.environ.get(variable) or not path.resolve().is_relative_to(root.resolve())
        or path.resolve() == root.resolve()
    ):
        raise Retained(f"test {variable} must be explicit and inside the fixture")
    return path


def tmp_root(root):
    return fixture_path(root, Path(os.environ.get("BOARD_TMP_ROOT", "/private/tmp")),
                        "BOARD_TMP_ROOT")


def chrome_root(root):
    override = os.environ.get("BOARD_CHROME_CLONE_ROOT")
    if override:
        return fixture_path(root, Path(override), "BOARD_CHROME_CLONE_ROOT")
    if os.environ.get("SQUAD_TEST_ISOLATION") == "1":
        return None  # Fixtures must never discover the real user's Chrome pool.
    if sys.platform != "darwin":
        return None
    result = subprocess.run(["getconf", "DARWIN_USER_DIR"], capture_output=True,
                            text=True, timeout=10)
    user_dir = Path(result.stdout.strip())
    if result.returncode or result.stderr or not user_dir.is_absolute() or user_dir.name != "0":
        raise Retained("Chrome user directory discovery failed")
    return user_dir.parent / "X/com.google.Chrome.code_sign_clone"


def idle_path(path):
    """Only lsof's quiet exit 1 proves no open files, cwd, or mapped files."""
    try:
        selection = ["+D", str(path)] if path.is_dir() else ["--", str(path)]
        result = subprocess.run(["lsof", "-nP", "-t", *selection],
                                capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 1 and not result.stdout and not result.stderr


def protected_tmp_name(name, policy=None):
    if policy is None:
        raise Retained("local protection policy unavailable")
    return (name == "wallet.key" or fnmatchcase(name, "*.keychain-db")
            or policy["protected_profile_substring"] in name
            or any(fnmatchcase(name, pattern) for pattern in (*PINNED_TMP_PATTERNS, *policy["pinned_patterns"])))


def tree_usage(path, cutoff, protect=False, policy=None):
    """Check every entry without following links; count allocated blocks once."""
    top = path.lstat()
    if not stat.S_ISDIR(top.st_mode) or path.is_symlink():
        raise Retained("not a regular directory")
    seen, blocks = set(), 0

    def inspect(name, entry):
        nonlocal blocks
        if entry.st_dev != top.st_dev or entry.st_uid != os.getuid():
            raise Retained("foreign mount or owner")
        if protect and protected_tmp_name(name, policy):
            raise Retained(f"protected wallet/profile/tool entry: {name}")
        if entry.st_mtime >= cutoff:
            raise Retained("young directory or recently modified descendant")
        identity = entry.st_dev, entry.st_ino
        if identity not in seen:
            blocks += entry.st_blocks * 512
            seen.add(identity)

    def fail(exc):
        raise exc

    inspect(path.name, top)
    for _, dirs, files, fd in os.fwalk(path, follow_symlinks=False, onerror=fail):
        inspect("", os.fstat(fd))
        for name in dirs + files:
            inspect(name, os.stat(name, dir_fd=fd, follow_symlinks=False))
    return blocks


def remove_old(path, cutoff, *, protect=False, policy=None, eligible=lambda: True):
    """Recheck policy and contents after the slow open-handle census."""
    try:
        identity = path.lstat()
        tree_usage(path, cutoff, protect, policy)
        if not idle_path(path):
            raise Retained("open handles or lsof could not prove path idle")
        if not eligible():
            raise Retained("task eligibility changed")
        current = path.lstat()
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise Retained("directory identity changed")
        size = tree_usage(path, cutoff, protect, policy)
        # Go module caches contain read-only directories. Change only directory
        # permissions, through fds, never files, symlink targets or hardlinks.
        def fail(exc):
            raise exc
        for _, _, _, fd in os.fwalk(path, follow_symlinks=False, onerror=fail):
            entry = os.fstat(fd)
            if entry.st_dev != current.st_dev or entry.st_uid != os.getuid():
                raise Retained("foreign mount or owner")
            if not entry.st_mode & stat.S_IWUSR:
                os.fchmod(fd, entry.st_mode | stat.S_IWUSR)
        shutil.rmtree(path)
        return 1, size
    except OSError as exc:
        print(f"  RETAINED ({exc})  {path}", flush=True)
        return 0, 0


def children(pool):
    if pool is None:
        return []
    try:
        entry = pool.lstat()
    except FileNotFoundError:
        return []
    # /private/tmp is root-owned shared infrastructure. Ownership is checked on
    # each deletion candidate, not on this container; unreadable pools raise.
    if not stat.S_ISDIR(entry.st_mode):
        raise Retained(f"unsafe storage pool: {pool}")
    return sorted(pool.iterdir())


class Registry:
    """Refresh atomic registry snapshots without reparsing an unchanged large file."""

    def __init__(self, root):
        self.path = root / "_state/active-tasks.json"
        self.identity = None
        self.value = None

    def read(self):
        def identity(entry):
            return (entry.st_dev, entry.st_ino, entry.st_size,
                    entry.st_mtime_ns, entry.st_ctime_ns)
        try:
            with self.path.open() as stream:
                before = identity(os.fstat(stream.fileno()))
                if before == self.identity:
                    return self.value
                value = json.load(stream)
                if (not isinstance(value, dict) or before != identity(os.fstat(stream.fileno()))
                        or before != identity(self.path.stat())):
                    return None
            self.identity, self.value = before, value
            return value
        except (OSError, ValueError):
            return None

def sweep_rescued(root, now):
    # Operator 2026-09-30: these copies may be the ONLY surviving work product.
    # No registry status or age authorizes automatic expiration of the archive.
    print(f"rescued: alert-only; {len(children(root / '_state/rescued-worker-artifacts'))} "
          "archive entries need Chrono; no automatic deletion", flush=True)


def process_idle(path):
    """Open handles and argv references are both reasons to retain a pool."""
    try:
        # Include launch-time GOCACHE/TMPDIR references and avoid line clipping.
        # The environment census is consumed only in memory, never logged.
        environment = "-Eww" if sys.platform == "darwin" else "eww"
        result = subprocess.run(["ps", environment, "-axo", "command="], capture_output=True,
                                text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode or result.stderr or not result.stdout.strip():
        return False
    # ps flattens argv and environment into text. Match absolute path tokens,
    # including descendants and NAME=/path, without matching sibling prefixes.
    paths = {str(path.absolute()), str(path.resolve())}
    if sys.platform == "darwin":
        paths.update(p[len("/private"):] for p in tuple(paths)
                     if p.startswith(("/private/tmp/", "/private/var/")))
    for full_path in paths:
        # Any character that cannot continue a path component is a boundary,
        # so an unknown separator over-retains rather than over-deletes.
        pattern = (r"(?:^|[^A-Za-z0-9._~@%+/-])" + re.escape(full_path)
                   + r"(?=$|[^A-Za-z0-9._~@%+-])")
        if re.search(pattern, result.stdout):
            return False
    return idle_path(path)


def sweep_chrome(root, now):
    removed = size = 0
    for path in children(chrome_root(root)):
        if not fnmatchcase(path.name, "code_sign_clone.*"):
            continue
        count, used = remove_old(path, now - 3600, eligible=lambda: process_idle(path))
        removed += count
        size += used
    print(f"chrome-clones: swept {removed} dir(s); allocated upper bound {size / GIB:.6f} GiB; "
          "unique reclaimed bytes unknown (APFS shared blocks are not additive)", flush=True)


class LiveReferences:
    """Fail closed on unreadable ownership; refresh at each deletion boundary."""

    def __init__(self, root):
        self.root = root
        self.registry = Registry(root)

    def snapshot(self):
        from board_process_truth import descriptor_error, load_json, terminal_outcome
        registry = self.registry.read()
        if registry is None:
            raise Retained("live task registry unavailable or malformed")
        dispatch = self.root / "_state/board-dispatch"
        if not dispatch.is_dir() or dispatch.is_symlink():
            raise Retained("live task descriptors unavailable")
        live, owners, finished_current, active_descriptors = set(), {}, set(), set()
        for task, entry in registry.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("status"), str):
                raise Retained("invalid live task entry")
            if entry["status"].lower().replace("_", "-") not in SETTLED:
                live.add(task)
        references = {}
        for path in dispatch.glob("*.dispatch.json"):
            value = load_json(path)
            if value is None or (value.get("schema") and descriptor_error(path, value)):
                raise Retained("invalid live task descriptor")
            task = value.get("task_id")
            match = re.fullmatch(r"(TASK-.+)\.(d-[0-9a-f]+)\.dispatch\.json", path.name)
            if not match or task != match[1]:
                raise Retained("unresolved task descriptor ownership")
            owners[match[2]] = task
            references.setdefault(task, []).extend((path, path.with_name(
                path.name.replace(".dispatch.json", ".context.json"))))
            receipt = load_json(path.with_name(path.name.replace(".dispatch.json", ".receipt.json")))
            settled = (registry.get(task, {}).get("status", "").lower().replace("_", "-") in SETTLED
                       or (value.get("schema") and terminal_outcome(receipt, value) is not None))
            running = False
            for field, probe in (("pid", os.kill), ("pgid", os.killpg)):
                pid = value.get(field)
                if pid is None:
                    continue
                if type(pid) is not int or pid <= 0:
                    running = True
                    continue
                try:
                    probe(pid, 0)
                    running = True
                except ProcessLookupError:
                    pass
                except OSError:
                    running = True
            if running or not settled:
                active_descriptors.add(task)
            elif registry.get(task, {}).get("delivery_attempt_id") == match[2]:
                finished_current.add(task)
        live = (live - finished_current) | active_descriptors
        text = []
        for task in live:
            if not re.fullmatch(r"TASK-[A-Za-z0-9_-]+", task):
                raise Retained("invalid live task identifier")
            text.append(task + " " + json.dumps(registry.get(task, {})))
            paths = references.get(task, [])
            for folder in ("inbox", "active", "archive"):
                paths += list((self.root / "departments").glob(f"*/{folder}/{task}.md"))
            for path in paths:
                if path.exists():
                    text.append(path.read_text())  # unreadable => retain
        return "\n".join(text), live, owners

    def unreferenced(self, name, marker=None):
        text, live, owners = self.snapshot()
        if name in text:
            return False
        # Older lanes named caches with only the task's final eight hex digits.
        if any(re.search(r"[0-9a-f]{8}$", task) and task[-8:] in name for task in live):
            return False
        if marker is not None and (marker not in owners or owners[marker] in live):
            return False
        return True


def tmp_candidate(path, policy=None):
    if policy is None:
        return False
    if protected_tmp_name(path.name, policy) or path.name.endswith(".status"):
        return False
    if path.parent.name == "chrono-scratch":
        return not path.name.startswith(("tmux-", "claude-"))
    marker = path / ".vibesquad-scratch"
    return (any(fnmatchcase(path.name, pattern)
                for pattern in (*CACHE_TMP_PATTERNS, *SCRATCH_TMP_PATTERNS,
                                *policy["cache_patterns"], *policy["scratch_patterns"]))
            or marker.exists() or marker.is_symlink())


def sweep_test_tmp(root, now):
    policy = tmp_policy(root)
    if policy is None:
        return
    pool = tmp_root(root)
    if any(part.startswith("claude-") for part in pool.resolve().parts):
        raise Retained("operator/Chrono scratchpads are protected")
    references = LiveReferences(root)
    references.snapshot()  # a failed census never means there are no live tasks
    removed = size = 0
    candidates = [(path, 12 * 3600) for path in children(pool)
                  if path.name != "chrono-scratch"]
    # Only direct children are disposable; never expire the container itself.
    candidates += [(path, 48 * 3600) for path in children(pool / "chrono-scratch")]
    for path, ttl in candidates:
        if not tmp_candidate(path, policy):
            continue
        def eligible():
            marker = path / ".vibesquad-scratch"
            attempt = None
            if marker.exists() or marker.is_symlink():
                if marker.is_symlink() or not marker.is_file():
                    return False
                attempt = marker.read_text().strip()
            return references.unreferenced(path.name, attempt) and process_idle(path)
        if not eligible():
            print(f"  RETAINED (live reference or uncertain ownership/process census)  {path}", flush=True)
            continue
        count, used = remove_old(path, now - ttl, protect=True, policy=policy, eligible=eligible)
        removed += count
        size += used
    print(f"test-tmp: swept {removed} dir(s), {size / GIB:.6f} GiB "
          "(allocated upper bound; unique reclaimed bytes unknown)", flush=True)


PG_DATABASES_SQL = """
SELECT json_build_object(
  'data_directory', current_setting('data_directory'),
  'port', current_setting('port'),
  'databases', COALESCE((SELECT json_agg(json_build_object(
    'name', d.datname, 'oid', d.oid::bigint, 'bytes', pg_database_size(d.oid),
    'connections', (SELECT count(*) FROM pg_stat_activity a WHERE a.datid = d.oid)))
    FROM pg_database d WHERE d.datname = '{database}'
      OR d.datname ~ '^{database}_[A-Za-z0-9_]+$'), '[]'::json));
"""
PG_VACUUM_SQL = """
SELECT (NOT EXISTS (SELECT FROM pg_stat_activity
  WHERE datname = current_database() AND pid <> pg_backend_pid())
  AND pg_database_size(current_database()) > 2147483648) AS run \\gset hygiene_
\\if :hygiene_run
SET lock_timeout = '1s';
SET statement_timeout = '10min';
VACUUM FULL;
\\echo hygiene_compacted
\\else
\\echo hygiene_retained
\\endif
"""


def test_pg_root(root, policy=None):
    policy = postgres_policy(root) if policy is None else policy
    if policy is None:
        return None
    override = os.environ.get("BOARD_TEST_PG_ROOT")
    if os.environ.get("SQUAD_TEST_ISOLATION") == "1" and not override:
        return None  # No discovery or connection to a host DB from fixtures.
    path = fixture_path(root, Path(override) if override else
                        Path(policy["data_directory"]).expanduser(), "BOARD_TEST_PG_ROOT")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise Retained("test Postgres directory is not an owned regular directory")
    return path


class TestPostgres:
    """Only the named local test cluster's Unix socket, never ambient PGHOST."""

    def __init__(self, root):
        policy = postgres_policy(root)
        self.database = policy["database"] if policy is not None else None
        self.superuser = policy["superuser"] if policy is not None else None
        self.data = test_pg_root(root, policy) if policy is not None else None

    def executable(self, name):
        found = shutil.which(name)
        if found:
            return found
        major = (self.data / "PG_VERSION").read_text().strip()
        if re.fullmatch(r"[0-9]+", major):
            for prefix in ("/opt/homebrew", "/usr/local"):
                path = Path(prefix) / f"opt/postgresql@{major}/bin/{name}"
                if path.is_file() and os.access(path, os.X_OK):
                    return str(path)
        raise Retained(f"{name} unavailable for test Postgres")

    def command(self, name, database):
        if self.data is None or self.superuser is None:
            raise Retained("test Postgres policy unavailable; not connected")
        args = [self.executable(name), f"--host={self.data}", "--port=55432",
                f"--username={self.superuser}", "--no-password"]
        if name == "psql":
            args += ["-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", f"--dbname={database}"]
        else:
            args += ["--maintenance-db=postgres", database]
        return args

    def run(self, name, database, sql=None, timeout=30):
        # Do not inherit services, connection URIs, passwords, or psql startup
        # scripts from a deployment lane. No network host can be selected here.
        env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C",
               "PGPASSFILE": os.devnull, "PGSERVICEFILE": os.devnull,
               "PGCONNECT_TIMEOUT": "3",
               "PGOPTIONS": "-c search_path=pg_catalog -c lock_timeout=1s -c statement_timeout=600000"}
        result = subprocess.run(self.command(name, database), input=sql,
                                capture_output=True, text=True, timeout=timeout, env=env)
        if result.returncode:
            # Avoid echoing ambient connection material from client diagnostics.
            raise Retained(f"test Postgres {name} failed (exit {result.returncode}); no forced retry")
        return result.stdout.strip()

    def snapshot(self):
        try:
            value = json.loads(self.run("psql", "postgres", PG_DATABASES_SQL.format(database=self.database)))
            if (Path(value["data_directory"]).resolve() != self.data.resolve()
                    or str(value["port"]) != "55432"):
                raise Retained("test Postgres cluster identity mismatch")
            rows = value["databases"]
            if not isinstance(rows, list):
                raise ValueError("database list")
            for row in rows:
                if (not isinstance(row, dict) or not re.fullmatch(
                        rf"{re.escape(self.database)}(?:_[A-Za-z0-9_]+)?", row.get("name", ""))
                        or any(type(row.get(key)) is not int or row[key] < 0
                               for key in ("oid", "bytes", "connections")) or row["oid"] == 0):
                    raise ValueError("database entry")
            return rows
        except (ValueError, KeyError, TypeError) as exc:
            raise Retained("test Postgres census unavailable or malformed") from exc

    def age(self, row, now):
        # pg_database has no creation timestamp. On macOS use the database
        # directory's birth time; elsewhere ctime/mtime give a conservative
        # minimum age. Never infer age from an arbitrary database name.
        path = self.data / "base" / str(row["oid"])
        entry = path.lstat()
        if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid():
            raise Retained("test database age cannot be verified")
        created = getattr(entry, "st_birthtime", max(entry.st_ctime, entry.st_mtime))
        if created <= 0 or created > now:
            raise Retained("test database creation time unavailable")
        return now - created


def sweep_test_databases(root, now):
    pg = TestPostgres(root)
    if pg.data is None:
        print("test-postgres: local cluster absent or fixture disabled; not connected", flush=True)
        return
    references = LiveReferences(root)
    references.snapshot()
    rows = pg.snapshot()
    for row in rows:
        name = row["name"]
        if name == pg.database or row["connections"]:
            continue
        try:
            if pg.age(row, now) <= 6 * 3600 or not references.unreferenced(name):
                print(f"test-postgres: retained {name} (young or live task reference)", flush=True)
                continue
            current = next((x for x in pg.snapshot() if x["name"] == name), None)
            if (current is None or current["oid"] != row["oid"] or current["connections"]
                    or pg.age(current, time.time()) <= 6 * 3600
                    or not references.unreferenced(name)):
                print(f"test-postgres: retained {name} (identity/eligibility changed)", flush=True)
                continue
            # No --force: PostgreSQL itself refuses a newly connected database.
            pg.run("dropdb", name)
            print(f"test-postgres: dropped {name} (idle, older than 6h)", flush=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"test-postgres: retained {name}: {exc}", flush=True)
    main = next((x for x in pg.snapshot() if x["name"] == pg.database), None)
    if main and main["bytes"] > 2 * GIB and main["connections"] == 0:
        # VACUUM FULL temporarily requires a second copy. Retain when there is
        # not even one database-size of headroom; never make a full disk worse.
        if shutil.disk_usage(pg.data).free <= main["bytes"]:
            print("test-postgres: compaction needs Chrono (insufficient rewrite headroom)", flush=True)
            return
        output = pg.run("psql", pg.database, PG_VACUUM_SQL, timeout=620)
        if "hygiene_compacted" in output.splitlines():
            print(f"test-postgres: compacted {pg.database} with VACUUM FULL", flush=True)
        elif "hygiene_retained" in output.splitlines():
            print(f"test-postgres: retained {pg.database} (connections/size changed)", flush=True)
        else:
            raise Retained("test database compaction outcome unverified")
    elif main:
        print(f"test-postgres: retained {pg.database} (connected or <= 2 GiB)", flush=True)


def private_bytes(path):
    """APFS private allocation, not st_blocks (which counts shared clones)."""
    if sys.platform != "darwin":
        return None
    class AttrList(ctypes.Structure):
        _fields_ = [("bitmapcount", ctypes.c_uint16), ("reserved", ctypes.c_uint16),
                    *[(name, ctypes.c_uint32) for name in
                      ("common", "volume", "directory", "file", "extended")]]
    # SDK sys/attr.h: ATTR_CMN_RETURNED_ATTRS, ATTR_CMNEXT_PRIVATESIZE;
    # FSOPT_NOFOLLOW | FSOPT_ATTR_CMN_EXTENDED. Returned fields are 4-byte aligned.
    attributes = AttrList(5, 0, 0x80000000, 0, 0, 0, 0x8)
    buffer = ctypes.create_string_buffer(32)
    try:
        function = ctypes.CDLL(None, use_errno=True).getattrlist
        function.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.c_ulong]
        function.restype = ctypes.c_int
        if function(os.fsencode(path), ctypes.byref(attributes), buffer, 32, 0x21) != 0:
            return None
        value = buffer.raw
        if int.from_bytes(value[:4], sys.byteorder) != 32 or not (
                int.from_bytes(value[20:24], sys.byteorder) & 0x8):
            return None
        return int.from_bytes(value[24:32], sys.byteorder)
    except (AttributeError, OSError):
        return None


def allocated_usage(path, deadline=None):
    """Hardlinks counted once; never traverse symlinks or foreign mounts."""
    seen, allocated, unique = set(), 0, 0
    device = path.lstat().st_dev
    def add(entry_path):
        nonlocal allocated, unique
        if deadline is not None and time.monotonic() > deadline:
            raise Retained("allocation census time budget exceeded")
        entry = entry_path.lstat()
        if entry.st_dev != device:
            raise Retained("allocation census encountered another volume")
        identity = entry.st_dev, entry.st_ino
        if identity in seen:
            return
        seen.add(identity)
        allocated += entry.st_blocks * 512
        # A directory's private allocation is filesystem metadata, not a file
        # clone estimate. Unsupported private-size queries stay UNKNOWN.
        private = private_bytes(entry_path)
        if private is None or (not stat.S_ISDIR(entry.st_mode) and entry.st_nlink > 1):
            unique = None  # an external hardlink may still own the allocation
        elif unique is not None:
            unique += private
    def fail(exc):
        raise exc
    add(path)
    if path.is_dir() and not path.is_symlink():
        for folder, dirs, files in os.walk(path, followlinks=False, onerror=fail):
            for name in dirs + files:
                add(Path(folder) / name)
    return allocated, unique


def apfs_report():
    if os.environ.get("SQUAD_TEST_ISOLATION") == "1" or sys.platform != "darwin":
        return "APFS container free: UNKNOWN; Preboot/staged updates: UNKNOWN (no APFS probe)"
    try:
        def plist(*args):
            result = subprocess.run(["diskutil", *args], capture_output=True, timeout=15)
            if result.returncode or result.stderr:
                raise Retained("diskutil unavailable")
            return plistlib.loads(result.stdout)
        info = plist("info", "-plist", "/System/Volumes/Data")
        container = next(c for c in plist("apfs", "list", "-plist")["Containers"]
                         if c["ContainerReference"] == info["APFSContainerReference"])
        free = int(container["CapacityFree"])
        capacity = int(container["CapacityCeiling"])
        parts = [f"APFS container free: {free / GIB:.2f} GiB / {capacity / GIB:.2f} GiB "
                 "(physical allocation shared by all volumes)"]
        for role in ("Preboot", "Update"):
            volumes = [v for v in container["Volumes"] if role in v.get("Roles", [])]
            size = sum(int(v["CapacityInUse"]) for v in volumes) if volumes else None
            parts.append(f"{role}: " + (f"{size / GIB:.2f} GiB allocated" if size is not None
                                        else "UNKNOWN") + "; Chrono only, leave staged updates")
        parts.append("staged update subset in Preboot/Update: unique attribution UNKNOWN; not additive")
        return "; ".join(parts)
    except (OSError, ValueError, KeyError, TypeError, StopIteration,
            plistlib.InvalidFileException, subprocess.TimeoutExpired) as exc:
        return f"APFS container free: UNKNOWN; Preboot/staged updates: UNKNOWN ({type(exc).__name__})"


def disk_report(root):
    parts = [apfs_report()]
    deadline = time.monotonic() + 20
    def measure(label, path, policy):
        if path is None:
            parts.append(f"{label}: UNKNOWN/unconfigured; {policy}")
            return
        try:
            used, private = allocated_usage(path, deadline)
            private_text = f"{private / GIB:.3f} GiB" if private is not None else "UNKNOWN"
            parts.append(f"{label}: allocated {used / GIB:.3f} GiB, unique {private_text}; {policy}")
        except FileNotFoundError:
            parts.append(f"{label}: absent; {policy}")
        except OSError as exc:
            parts.append(f"{label}: UNKNOWN ({exc}); {policy}")
    if sys.platform == "darwin" and os.environ.get("SQUAD_TEST_ISOLATION") != "1":
        preboot = Path("/System/Volumes/Preboot")
        measure("staged macOS update (Preboot/staging)", preboot / "staging", "Chrono only; leave intact")
        try:
            for path in children(preboot):
                if re.fullmatch(r"[0-9A-Fa-f-]{36}", path.name):
                    measure("staged macOS update (restore-staged)", path / "restore-staged",
                            "Chrono only; leave intact")
        except OSError as exc:
            parts.append(f"staged update census: UNKNOWN ({exc})")
    measure("board worktrees", root / "_state/board-worktrees", "AUTO after verified rescue; live retained")
    measure("rescued-artifact archive", root / "_state/rescued-worker-artifacts", "Chrono only")
    try:
        measure("Chrome code_sign_clone", chrome_root(root), "AUTO when idle")
    except (OSError, subprocess.TimeoutExpired) as exc:
        parts.append(f"Chrome code_sign_clone: UNKNOWN ({type(exc).__name__})")
    try:
        pg = TestPostgres(root)
        measure("test Postgres cluster", pg.data, "AUTO idle per-run DBs >6h; main compacted above 2 GiB")
        if pg.data is not None:
            for row in pg.snapshot():
                measure(f"test DB {row['name']}", pg.data / "base" / str(row["oid"]),
                        f"connections={row['connections']}; WAL/shared tablespaces in cluster total")
    except (OSError, subprocess.TimeoutExpired) as exc:
        parts.append(f"test DB census: UNKNOWN ({exc})")
    try:
        pool = tmp_root(root)
        measure("temp caches/scratch pool", pool, "AUTO idle lane pools >12h, chrono-scratch children >48h; rest Chrono only")
        policy = tmp_policy(root)
        sizes = []
        for path in children(pool):
            try:
                used, private = allocated_usage(path, deadline)
                sizes.append((used, path.name, private, tmp_candidate(path, policy)))
            except OSError:
                parts.append("temp item ranking incomplete")
                break
        parts.append("top 5 temp allocations: " + "; ".join(
            f"{name} ({used / GIB:.3f} GiB allocated, unique "
            f"{f'{private / GIB:.3f} GiB' if private is not None else 'UNKNOWN'}; "
            f"{'AUTO if eligible' if auto else 'Chrono only'})"
            for used, name, private, auto in sorted(sizes, reverse=True)[:5]))
    except OSError as exc:
        parts.append(f"temp caches: UNKNOWN ({exc})")
    parts.append("user files/unrecognised storage: Chrono only; directory allocations may share APFS "
                 "blocks and must not be summed; unique values exclude shared clone blocks")
    return "; ".join(parts)


# --- Disk-cleanup skill pools -------------------------------------------------
# These run ONLY from the explicit `bin/disk-hygiene.sh --plan/--apply-safe/
# --apply-preserve` skill path, never from the unattended nightly, so an operator
# or Chrono is always present when a new pool deletes. Default is dry-run: every
# function prints a sized plan and touches nothing unless apply=True.


def vault_snapshots_root(root):
    override = os.environ.get("BOARD_VAULT_SNAPSHOTS_ROOT")
    if override:
        return fixture_path(root, Path(override), "BOARD_VAULT_SNAPSHOTS_ROOT")
    if os.environ.get("SQUAD_TEST_ISOLATION") == "1":
        return None  # fixtures never discover the operator's real snapshot pool
    return Path.home() / "vault-snapshots"


def is_snapshot(path):
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in SNAPSHOT_SUFFIXES)


def plan_vault_snapshots(root, keep=VAULT_SNAPSHOT_KEEP):
    """Owned snapshot tarballs beyond the newest `keep`, by mtime.

    Only owned regular files whose name is a tarball are candidates; a symlink,
    a non-tarball, or a foreign-owned entry is never planned. Returns
    (pool, [(path, size)]); pool is None when the pool is unconfigured."""
    pool = vault_snapshots_root(root)
    if pool is None:
        return None, []
    try:
        entry = pool.lstat()
    except FileNotFoundError:
        return pool, []
    if not stat.S_ISDIR(entry.st_mode) or pool.is_symlink():
        raise Retained("vault snapshot pool is not a regular directory")
    files = []
    for path in sorted(pool.iterdir()):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid() or not is_snapshot(path)):
            continue
        files.append((path, info.st_mtime, info.st_size))
    files.sort(key=lambda row: row[1], reverse=True)  # newest first
    return pool, [(path, size) for path, _, size in files[keep:]]


def sweep_vault_snapshots(root, now=None, keep=VAULT_SNAPSHOT_KEEP, apply=False):
    try:
        pool, plan = plan_vault_snapshots(root, keep)
    except Retained as exc:
        print(f"vault-snapshots: RETAINED ({exc})", flush=True)
        return 0, 0
    if pool is None:
        print("vault-snapshots: pool unconfigured or fixture-disabled; not measured", flush=True)
        return 0, 0
    total = sum(size for _, size in plan)
    if not apply:
        for path, size in plan:
            print(f"  PLAN delete {path}  ({size / GIB:.3f} GiB)", flush=True)
        print(f"vault-snapshots: {len(plan)} stale tarball(s), {total / GIB:.3f} GiB "
              f"reclaimable; keep newest {keep}", flush=True)
        return 0, total
    removed = removed_bytes = 0
    for path, size in plan:
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                    or info.st_uid != os.getuid()):
                raise Retained("snapshot is not an owned regular file")
            os.unlink(path)
            removed += 1
            removed_bytes += size
        except OSError as exc:
            print(f"  RETAINED ({exc})  {path}", flush=True)
    print(f"vault-snapshots: deleted {removed} tarball(s), {removed_bytes / GIB:.3f} GiB; "
          f"kept newest {keep}", flush=True)
    return removed, removed_bytes


def tool_version_pools(root):
    override = os.environ.get("BOARD_TOOL_VERSIONS_HOME")
    if override:
        home = fixture_path(root, Path(override), "BOARD_TOOL_VERSIONS_HOME")
    elif os.environ.get("SQUAD_TEST_ISOLATION") == "1":
        return []
    else:
        home = Path.home()
    daemon = home / ".codex/packages/app-server-daemon"
    return [("codex-daemon", daemon / "releases", daemon / "current", True),
            ("claude-versions", home / ".local/share/claude/versions",
             home / ".local/bin/claude", False)]


def plan_tool_versions(pool, current, directories):
    """Keep the active symlink target and newest other owned version by mtime."""
    if not pool.exists() and not pool.is_symlink():
        return []
    entries = children(pool)  # Reject a symlinked pool.
    if not current.is_symlink():
        raise Retained("active version symlink unavailable")
    target = current.resolve(strict=True)
    if target.parent != pool.resolve():
        raise Retained("active version is outside the version pool")
    versions = []
    for path in entries:
        entry = path.lstat()
        expected_type = stat.S_ISDIR if directories else stat.S_ISREG
        if (path.is_symlink() or not expected_type(entry.st_mode)
                or entry.st_uid != os.getuid()
                or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._-]+)?", path.name)):
            continue
        versions.append((path, entry.st_mtime_ns))
    if target not in {p.resolve() for p, _ in versions}:
        raise Retained("active version is not an owned recognised version")
    others = sorted(((p, mtime) for p, mtime in versions if p.resolve() != target),
                    key=lambda row: (row[1], row[0].name), reverse=True)
    return [(path, allocated_usage(path)[0]) for path, _ in others[1:]]


def sweep_tool_versions(root, now=None, apply=False):
    now = time.time() if now is None else now
    removed = total = 0
    for label, pool, current, directories in tool_version_pools(root):
        try:
            plan = plan_tool_versions(pool, current, directories)
            for path, size in plan:
                try:
                    identity = path.lstat()
                    if not process_idle(path):
                        raise Retained("process reference, mapped binary or uncertain census")

                    def eligible():
                        return (process_idle(path) and path in {p for p, _ in
                                plan_tool_versions(pool, current, directories)})

                    if not apply:
                        print(f"  PLAN delete version {path} ({size / GIB:.3f} GiB)", flush=True)
                        total += size
                        continue
                    if directories:
                        count, used = remove_old(path, now, eligible=eligible)
                    else:
                        if not eligible():
                            raise Retained("version retention changed")
                        entry = path.lstat()
                        if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid()
                                or entry.st_nlink != 1 or entry != identity):
                            raise Retained("version identity changed or shared inode")
                        path.unlink()
                        count, used = 1, size
                    removed += count
                    total += used
                except OSError as exc:
                    print(f"  {label}: RETAINED ({exc}) {path}", flush=True)
            print(f"{label}: keep active target + newest other; mapped/in-use versions retained", flush=True)
        except OSError as exc:
            print(f"{label}: RETAINED ({exc})", flush=True)
    return removed, total


def rescued_root(root):
    return root / "_state/rescued-worker-artifacts"


def plan_rescued_bulk(root):
    """Regenerable build/cache subtrees inside settled, unreferenced rescues.

    The rescue archive itself is never a candidate -- its documents may be the
    only surviving work product (sweep_rescued stays alert-only). Only a subtree
    whose directory name is in RESCUE_BULK_NAMES, under a task that is terminal
    and not live-referenced, is planned. A failed live-reference census raises
    rather than silently returning an empty plan."""
    pool = rescued_root(root)
    plan = []
    try:
        tasks = children(pool)
    except FileNotFoundError:
        return plan
    references = LiveReferences(root)
    references.snapshot()  # raises Retained on an unreadable registry/descriptor set
    for task_dir in tasks:
        if task_dir.is_symlink() or not task_dir.is_dir():
            continue
        task = task_dir.name
        if not re.fullmatch(r"TASK-[A-Za-z0-9_-]+", task) or not references.unreferenced(task):
            continue

        def fail(exc):
            raise exc

        for current, dirs, _ in os.walk(task_dir, onerror=fail, followlinks=False):
            keep = []
            for name in dirs:
                child = Path(current) / name
                if child.is_symlink():
                    continue  # do not descend or size through a link
                if name in RESCUE_BULK_NAMES:
                    used, _ = allocated_usage(child)
                    plan.append((child, used))
                else:
                    keep.append(name)
            dirs[:] = keep  # never descend into a matched bulk subtree
    return plan


def sweep_rescued_bulk(root, now=None, apply=False):
    now = time.time() if now is None else now
    policy = tmp_policy(root)
    if policy is None:
        print("rescued-bulk: SKIP (local protection policy unavailable)", flush=True)
        return 0, 0
    plan = plan_rescued_bulk(root)
    total = sum(size for _, size in plan)
    if not apply:
        for path, size in plan:
            print(f"  PLAN delete bulk {path}  ({size / GIB:.3f} GiB)", flush=True)
        print(f"rescued-bulk: {len(plan)} regenerable subtree(s), {total / GIB:.3f} GiB "
              "reclaimable; rescue documents retained", flush=True)
        return 0, total
    references = LiveReferences(root)
    removed = removed_bytes = 0
    for path, _ in plan:
        task = path.relative_to(rescued_root(root)).parts[0]
        # protect=True aborts the subtree if it holds a wallet/keychain/profile
        # entry; the eligibility recheck refreshes the live-reference census.
        count, used = remove_old(
            path, now - 3600, protect=True, policy=policy,
            eligible=lambda t=task, p=path: references.unreferenced(t) and process_idle(p))
        removed += count
        removed_bytes += used
    print(f"rescued-bulk: deleted {removed} subtree(s), {removed_bytes / GIB:.3f} GiB; "
          "rescue documents retained", flush=True)
    return removed, removed_bytes


def _docker(args, timeout=60):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def docker_running_containers():
    """True if the daemon is reachable AND a container is running; None if the
    daemon is unreachable (so a prune must not proceed on a guess)."""
    try:
        result = _docker(["ps", "--quiet"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return bool(result.stdout.strip())


def sweep_docker(root=None, now=None, apply=False):
    """Dangling-image prune, aged stopped-container prune, colima fstrim.

    Never `system prune -a` (that removes tagged images the operator may need),
    and never a container prune while any container runs -- a running container
    means board/release work may be in flight (memory: never stop Docker with
    lanes in flight). fstrim reclaims freed blocks back to the host and is safe."""
    if os.environ.get("SQUAD_TEST_ISOLATION") == "1":
        print("docker: fixture-disabled (SQUAD_TEST_ISOLATION); not contacted", flush=True)
        return
    if shutil.which("docker") is None:
        print("docker: CLI not installed; skipped", flush=True)
        return
    try:
        df = _docker(["system", "df"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"docker: daemon unreachable ({type(exc).__name__}); skipped", flush=True)
        return
    if df.returncode:
        print("docker: daemon unreachable; skipped", flush=True)
        return
    print("docker system df:\n" + df.stdout.strip(), flush=True)
    if not apply:
        print("docker: PLAN prune dangling images + stopped containers "
              f"(exited > {DOCKER_CONTAINER_MIN_AGE}), then colima fstrim", flush=True)
        return
    running = docker_running_containers()
    if running is None:
        print("docker: daemon unreachable at apply; skipped", flush=True)
        return
    if running:
        print("docker: containers running; skipping container prune (work may be in flight)", flush=True)
    else:
        out = _docker(["container", "prune", "--force", "--filter", f"until={DOCKER_CONTAINER_MIN_AGE}"])
        print("docker container prune: " + (out.stdout.strip() or out.stderr.strip() or "ok"), flush=True)
    img = _docker(["image", "prune", "--force"])  # dangling (untagged) only
    print("docker image prune: " + (img.stdout.strip() or img.stderr.strip() or "ok"), flush=True)
    if shutil.which("colima") is None:
        print("colima: CLI not installed; fstrim skipped", flush=True)
        return
    try:
        trim = subprocess.run(["colima", "ssh", "--", "sudo", "fstrim", "-a"],
                              capture_output=True, text=True, timeout=120)
        print("colima fstrim: " + ("ok" if trim.returncode == 0 else trim.stderr.strip()), flush=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"colima fstrim: unavailable ({type(exc).__name__})", flush=True)


def run_safe_sweep(root):
    """Tier-1 board sweep shared by the nightly (--periodic) and the explicit
    --apply-safe skill path: preserve-first worktree/scratch prune, then the
    in-process chrome/test-tmp/test-db sweeps. Returns a process-style status."""
    root = root.resolve()
    result = subprocess.run(["bash", str(root / "bin/prune-board-worktrees.sh"),
                             "--apply", "--sweep-tmp"], cwd=root,
                            env={**os.environ, "VAULT_ROOT": str(root)})
    return result.returncode or periodic_sweep(root)


def cleanup_plan(root, mode, now=None):
    """Disk-cleanup skill entry point.

    mode "plan": survey + sized plan across every pool; delete nothing.
    mode "apply-safe": Tier 1 (board sweep + vault-snapshot retention + docker).
    mode "apply-preserve": Tier 1 plus Tier 2 (rescued regenerable bulk).
    Tier 3 (operator-managed storage and protected data) is reported by the
    survey but NEVER acted on here."""
    now = time.time() if now is None else now
    apply_safe = mode in ("apply-safe", "apply-preserve")
    apply_preserve = mode == "apply-preserve"
    status = 0
    print("== disk-cleanup survey ==", flush=True)
    print(disk_report(root), flush=True)
    if apply_safe:
        print("== Tier 1: board worktree/scratch/clone/test sweep ==", flush=True)
        status = run_safe_sweep(root) or status
    # A Retained is the SAFE outcome (keep the tree), never a tool failure, so
    # it is reported but never raises the exit status; only a real OSError or a
    # timeout does. A missing registry in a fresh worktree thus leaves --plan at 0.
    print("== Tier 1: vault snapshots ==", flush=True)
    try:
        sweep_vault_snapshots(root, now=now, apply=apply_safe)
    except Retained as exc:
        print(f"vault-snapshots: retained ({exc})", flush=True)
    except OSError as exc:
        print(f"vault-snapshots: error ({exc})", flush=True)
        status = status or 1
    print("== Tier 1: tool versions ==", flush=True)
    sweep_tool_versions(root, now=now, apply=apply_safe)
    print("== Tier 1: docker/colima ==", flush=True)
    try:
        sweep_docker(root, now=now, apply=apply_safe)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"docker: skipped ({type(exc).__name__})", flush=True)
    print("== Tier 2: rescued regenerable bulk "
          f"({'APPLY' if apply_preserve else 'plan only'}) ==", flush=True)
    try:
        sweep_rescued_bulk(root, now=now, apply=apply_preserve)
    except Retained as exc:
        print(f"rescued-bulk: census unavailable, retained ({exc})", flush=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"rescued-bulk: error ({type(exc).__name__})", flush=True)
        status = status or 1
    print("== Tier 3 (operator only; never automated here) ==", flush=True)
    print("  operator-managed storage and protected data -- see survey above; "
          "operator acts, not this tool", flush=True)
    return status


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Disk-cleanup skill: survey and tiered reclaim")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", dest="mode", action="store_const", const="plan")
    group.add_argument("--apply-safe", dest="mode", action="store_const", const="apply-safe")
    group.add_argument("--apply-preserve", dest="mode", action="store_const", const="apply-preserve")
    args = parser.parse_args(argv)
    return cleanup_plan(Path.cwd(), args.mode or "plan")


def periodic_sweep(root):
    # Coordinate with preservation, including settlement/nightly invocations.
    with (root / "_state/prune-board-worktrees.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("cleanup already running; deferred to periodic sweep", flush=True)
            return 1
        result = 0
        now = time.time()
        for sweep in (sweep_rescued, sweep_chrome, sweep_test_tmp, sweep_test_databases):
            try:
                sweep(root, now)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"  RETAINED ({exc})", flush=True)
                result = 1
        return result


if __name__ == "__main__":
    sys.exit(main())
