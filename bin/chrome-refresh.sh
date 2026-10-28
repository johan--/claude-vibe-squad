#!/bin/bash
# Nightly refresh preserves the authenticated profile. Tabs depend on Chrome's
# session restore (chrome-bootstrap.sh currently passes --restore-last-session).
# Set CHRONO_SKIP_CHROME_REFRESH=1 to retain the current browser processes.
set -euo pipefail

if [[ "${CHRONO_SKIP_CHROME_REFRESH:-0}" == 1 ]]; then
    echo "chrome-refresh: skipped by CHRONO_SKIP_CHROME_REFRESH=1"
    exit 0
fi

# shellcheck source-path=SCRIPTDIR source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"

exec python3 - "$VAULT_ROOT" <<'PY'
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path(sys.argv[1])
uid = os.getuid()
home = Path.home().resolve()
persistent_profile = (home / ".chrono/chrome-persistent-profile").resolve()
BROWSER_TOOLS = {"playwright", "chrome-devtools", "browser-use", "computer-use"}


def board_browser_idle():
    # Like the other maintenance phases, consume the canonical task-id map.
    # Recheck it at each mutation boundary; unreadable/unknown means retain.
    path = root / "_state/active-tasks.json"
    def identity(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    with path.open() as stream:
        before = identity(os.fstat(stream.fileno()))
        registry = json.load(stream)
        if before != identity(os.fstat(stream.fileno())) or before != identity(path.stat()):
            raise ValueError("registry changed during read")
    if not isinstance(registry, dict):
        raise ValueError("registry is not a task map")
    active = []
    for task, entry in registry.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("status"), str):
            raise ValueError("registry contains an invalid task record")
        if entry["status"] == "in-flight":
            active.append((task, entry))
    if not active:
        return True
    source = json.loads((root / "model-lanes/specialist-lane-capabilities.v1.json").read_text())
    capabilities = {(entry["specialist"], entry["lane"]): entry for entry in source["entries"]}
    for task, entry in active:
        lane = entry.get("to_model")
        if lane == "codex":
            lane = "gpt-codex"
        role = capabilities.get((entry.get("specialist"), lane))
        # Global-MCP lanes can expose browser tools beyond their role map.
        if role is None or lane in {"gemini", "grok"}:
            print(f"chrome-refresh: deferred; browser access unknown for {task}")
            return False
        tools = {item["id"] for group in ("tools", "mcps") for item in role[group]}
        if tools & BROWSER_TOOLS:
            print(f"chrome-refresh: deferred; browser-capable lane in flight: {task}")
            return False
    return True


def process_table():
    result = subprocess.run(
        ["ps", "-axo", "uid=,pid=,ppid=,lstart=,command="],
        check=True, capture_output=True, text=True, timeout=15,
    )
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 8)
        if len(fields) != 9:
            raise ValueError("incomplete process census")
        owner, pid, parent = map(int, fields[:3])
        rows[pid] = (owner, parent, " ".join(fields[3:8]), fields[8])
    if not rows:
        raise ValueError("empty process census")
    return rows


def mcp_chrome(row):
    owner, _, _, command = row
    # Only browser roots, never helpers, shells containing Chrome argv, or the
    # operator's ordinary Chrome. macOS code-sign clones keep this binary tail.
    executable_roots = ("/Applications/", "/private/var/folders/", "/var/folders/",
                        str(home / "Applications") + "/",
                        str(home / "Library/Caches/ms-playwright") + "/",
                        str(home / ".cache/ms-playwright") + "/")
    if owner != uid or not command.startswith(executable_roots) or not re.match(
        r"^/[^\n]+/Contents/MacOS/(?:Google Chrome(?: for Testing)?|Chromium)(?: |$)",
        command,
    ):
        return False
    match = re.search(r"(?:^| )--user-data-dir=(.+?)(?= --|$)", command)
    if not match:
        return False
    profile = Path(match[1].strip('"\''))
    if not profile.is_absolute():
        return False
    profile = profile.resolve()
    if profile == persistent_profile or persistent_profile in profile.parents:
        return False
    if profile.parent == home / ".cache/chrome-devtools":
        return profile.name == "chrome-profile" or profile.name.startswith("chrome-profile-")
    if profile.parent in (home / ".cache/ms-playwright", home / "Library/Caches/ms-playwright"):
        return profile.name.startswith("mcp-")
    # Playwright's isolated context uses this mkdtemp prefix on macOS/Linux.
    return bool(re.fullmatch(r"playwright_chromiumdev_profile-[A-Za-z0-9_-]+", profile.name))


def orphan(row, rows):
    parent = row[1]
    # Exited parents are normally reparented to launchd/init (PID 1). Any
    # surviving non-init parent is reason to retain, even if it is not an MCP.
    return parent == 1 or (parent > 1 and parent not in rows)


def main():
    if not board_browser_idle():
        return
    census = process_table()
    reaped = 0
    for pid, row in census.items():
        if pid <= 1 or not mcp_chrome(row) or not orphan(row, census):
            continue
        if not board_browser_idle():
            return
        current = process_table()
        # Compare uid, parent, start time AND full command to reject PID reuse.
        if current.get(pid) != row or not orphan(row, current):
            print(f"chrome-refresh: retained changed process {pid}")
            continue
        subprocess.run(["kill", "-TERM", str(pid)], check=True, timeout=15)
        reaped += 1
        print(f"chrome-refresh: sent TERM to orphan MCP Chrome pid={pid}")
    # Leave persistent Chrome running to preserve open tabs and in-progress
    # logins. The periodic sweep can reclaim code_sign_clone data after that
    # Chrome exits on its own.
    print(f"chrome-refresh: orphan TERM signals={reaped}; persistent Chrome left running")


try:
    main()
except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
    print(f"chrome-refresh: failed; remaining processes retained: {exc}", file=sys.stderr)
    sys.exit(1)
PY
