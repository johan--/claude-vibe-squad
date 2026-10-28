"""Run the real refresh entrypoint with fake ps/kill/launchctl executables."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


@pytest.fixture
def harness(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault"
    (vault / "_state").mkdir(parents=True)
    (vault / "model-lanes").mkdir()
    registry = vault / "_state/active-tasks.json"
    registry.write_text("{}")
    capabilities = vault / "model-lanes/specialist-lane-capabilities.v1.json"
    capabilities.write_text(json.dumps({"entries": [
        {"specialist": "browser", "lane": "gpt-codex", "tools": [{"id": "playwright"}], "mcps": []},
        {"specialist": "writer", "lane": "gpt-codex", "tools": [], "mcps": []},
    ]}))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    calls = tmp_path / "calls"
    snapshots = tmp_path / "snapshots.json"
    # Every mutation is intercepted, even if a test unexpectedly takes a branch.
    stub = f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["CALLS"], "a") as stream:
    stream.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "ps":
    state = pathlib.Path(os.environ["PS_COUNT"])
    count = int(state.read_text()) if state.exists() else 0
    state.write_text(str(count + 1))
    rows = json.loads(pathlib.Path(os.environ["SNAPSHOTS"]).read_text())
    print(rows[min(count, len(rows) - 1)])
    if os.environ.get("ACTIVATE_BROWSER"):
        pathlib.Path(os.environ["REGISTRY"]).write_text(json.dumps({"TASK-new": {
            "status": "in-flight", "specialist": "browser", "to_model": "gpt-codex"}}))
sys.exit(int(os.environ.get(name.upper() + "_EXIT", "0")))
'''
    for name in ("ps", "kill", "launchctl", "uv"):
        tool = fakebin / name
        tool.write_text(stub)
        tool.chmod(0o755)

    def row(pid, parent, profile, *, owner=None, started="Mon Oct 26 10:00:00 2026", command=None):
        command = command or f"{CHROME} --user-data-dir={profile} --remote-debugging-port=0"
        return f"{os.getuid() if owner is None else owner} {pid} {parent} {started} {command}"

    def run(rows=None, *, registry_value=None, overrides=None, script="chrome-refresh.sh", later=None):
        if registry_value is not None:
            registry.write_text(json.dumps(registry_value))
        rows = rows or [row(100, 1, home / ".chrono/chrome-persistent-profile")]
        snapshots.write_text(json.dumps(["\n".join(rows), "\n".join(later or rows)]))
        environment = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}",
                       "HOME": str(home), "VAULT_ROOT": str(vault), "CALLS": str(calls),
                       "SNAPSHOTS": str(snapshots), "PS_COUNT": str(tmp_path / "ps-count"),
                       "REGISTRY": str(registry), "CHRONO_SKIP_CHROME_REFRESH": "0"}
        environment.update(overrides or {})
        result = subprocess.run(["/bin/bash", str(ROOT / "bin" / script)], env=environment,
                                capture_output=True, text=True, timeout=10)
        recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, recorded

    return run, row, home, registry


def mutations(calls):
    return [call for call in calls if call[0] in {"kill", "launchctl"}]


def test_idle_refresh_never_restarts_the_persistent_browser(harness):
    run, _, _, _ = harness
    result, calls = run()
    assert result.returncode == 0, result.stderr
    assert not [call for call in calls if call[0] == "launchctl"]


def test_skip_flag_does_not_even_census_processes(harness):
    run, _, _, _ = harness
    result, calls = run(overrides={"CHRONO_SKIP_CHROME_REFRESH": "1"})
    assert result.returncode == 0
    assert "skipped" in result.stdout
    assert calls == []


@pytest.mark.parametrize("specialist,status,allowed", [
    ("browser", "in-flight", False), ("browser", "complete", True),
    ("writer", "in-flight", True), ("unknown", "in-flight", False),
])
def test_registry_gates_browser_capability(harness, specialist, status, allowed):
    run, _, _, _ = harness
    result, calls = run(registry_value={"TASK-fixture": {
        "status": status, "specialist": specialist, "to_model": "gpt-codex"}})
    assert result.returncode == 0, result.stderr
    assert ("deferred" not in result.stdout) is allowed
    if not allowed:
        assert "deferred" in result.stdout
        assert calls == []


@pytest.mark.parametrize("content", ["broken", "[]", '{"TASK-bad": null}'])
def test_unreadable_registry_fails_without_mutation(harness, content):
    run, _, _, registry = harness
    registry.write_text(content)
    result, calls = run()
    assert result.returncode != 0
    assert calls == []


def test_only_owned_orphan_mcp_chrome_roots_are_signalled(harness):
    run, row, home, _ = harness
    persistent = home / ".chrono/chrome-persistent-profile"
    devtools = home / ".cache/chrome-devtools/chrome-profile"
    result, calls = run([
        row(100, 1, persistent),
        row(101, 1, devtools),
        row(102, 55555, home / "Library/Caches/ms-playwright/mcp-chrome-test"),
        row(103, 1, "/tmp/playwright_chromiumdev_profile-test"),
        row(104, 1, home / "ordinary-profile"),
        row(105, 200, devtools),
        row(106, 1, devtools, owner=os.getuid() + 1),
        row(107, 1, persistent / "Default"),
        row(108, 1, devtools, command=f"/bin/sh -c {CHROME} --user-data-dir={devtools}"),
        row(109, 1, devtools, command=f"/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Helper --user-data-dir={devtools}"),
        row(110, 1, devtools, command=f"/private/var/folders/ab/cd/X/com.google.Chrome.code_sign_clone.fixture/Contents/MacOS/Google Chrome --user-data-dir={devtools}"),
        row(200, 1, "unused", command="node chrome-devtools-mcp"),
    ])
    assert result.returncode == 0, result.stderr
    assert [call for call in calls if call[0] == "kill"] == [
        ["kill", "-TERM", "101"], ["kill", "-TERM", "102"], ["kill", "-TERM", "103"],
        ["kill", "-TERM", "110"],
    ]
    assert not any(call[0] == "launchctl" for call in calls)


def test_pid_reuse_retains_the_replacement(harness):
    run, row, home, _ = harness
    original = row(101, 1, home / ".cache/chrome-devtools/chrome-profile")
    replacement = row(101, 1, home / ".cache/chrome-devtools/chrome-profile",
                      started="Tue Oct 27 10:00:00 2026")
    result, calls = run([original], later=[replacement])
    assert result.returncode == 0
    assert "retained changed process" in result.stdout
    assert not any(call[0] == "kill" for call in calls)


def test_new_browser_lane_blocks_mutation_after_census(harness):
    run, row, home, _ = harness
    result, calls = run([row(101, 1, home / ".cache/chrome-devtools/chrome-profile")],
                        overrides={"ACTIVATE_BROWSER": "1"})
    assert result.returncode == 0
    assert "deferred" in result.stdout
    assert mutations(calls) == []


@pytest.mark.parametrize("tool", ["ps", "kill"])
def test_failed_host_command_is_not_reported_as_success(harness, tool):
    run, row, home, _ = harness
    result, _ = run([row(101, 1, home / ".cache/chrome-devtools/chrome-profile")],
                    overrides={tool.upper() + "_EXIT": "7"})
    assert result.returncode != 0
    assert "failed" in result.stderr


@pytest.mark.parametrize("exit_code", [0, 9])
def test_uv_phase_invokes_prune_and_propagates_failure(harness, exit_code):
    run, _, _, _ = harness
    result, calls = run(script="uv-cache-prune.sh", overrides={"UV_EXIT": str(exit_code)})
    assert result.returncode == exit_code
    assert calls == [["uv", "cache", "prune"]]
