"""LaunchAgent lifecycle controls; all writes and launchctl calls are fixtures.

The fixture PATH contains only the file utilities this installer needs, a local
launchctl model, and an offline uv stub. HOME is unchanged; the existing
SQUAD_LAUNCHAGENTS_DIR and SQUAD_INSTALL_AGENTS seams select disposable state.
No host launchctl, network client, Git command, or hook is available to children.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MONITOR = "com.chrono.squad-monitor"
CHROME = "com.vibesquad.chrome"
DAEMON = "com.vibesquad.daemon"
MANAGED = (DAEMON, "com.claudevibesquad.nightly", "com.vibesquad.dream", MONITOR)

MOCK_LAUNCHCTL = r'''
import json, os, pathlib, plistlib, sys
base = pathlib.Path(os.environ["ROUTINE_FIXTURE"])
state_file = base / "loaded.json"
args = sys.argv[1:]
with (base / "calls.jsonl").open("a") as handle:
    handle.write(json.dumps(args) + "\n")
state = json.loads(state_file.read_text())
if args[0] == "print":
    rc = int(os.environ.get("MOCK_QUERY_RC", "0"))
    if rc:
        raise SystemExit(rc)
    label = args[1].rsplit("/", 1)[-1]
    if label not in state:
        raise SystemExit(113)
    if os.environ.get("MOCK_NO_PATH") != "1":
        print("    path = " + state[label])
    print("    last exit code = 0")
elif args[0] == "bootstrap":
    target = pathlib.Path(args[2])
    assert target.is_relative_to(base), "bootstrap escaped fixture"
    plist = plistlib.loads(target.read_bytes())
    label = plist["Label"]
    if label == "com.chrono.squad-monitor":
        for key in ("StandardOutPath", "StandardErrorPath"):
            assert pathlib.Path(plist[key]).parent.is_dir(), "monitor log directory missing before bootstrap"
    state[label] = str(target)
elif args[0] == "bootout":
    rc = int(os.environ.get("MOCK_BOOTOUT_RC", "0"))
    if rc:
        raise SystemExit(rc)
    if os.environ.get("MOCK_BOOTOUT_STUCK") != "1":
        state.pop(args[1].rsplit("/", 1)[-1], None)
else:
    raise SystemExit("unexpected launchctl operation: " + repr(args))
state_file.write_text(json.dumps(state))
'''

# Model only the two plutil operations the installer uses, with real parsing.
# launchctl is already a fixture; no macOS executable is needed for these tests.
PLIST_TOOL = r'''
import pathlib, plistlib, sys
args = sys.argv[1:]
try:
    if len(args) == 2 and args[0] == "-lint":
        plistlib.loads(pathlib.Path(args[1]).read_bytes())
    elif len(args) == 4 and args[0] == "-extract" and args[2] == "raw":
        value = plistlib.loads(pathlib.Path(args[3]).read_bytes())[args[1]]
        if not isinstance(value, str):
            raise ValueError("fixture supports string extraction only")
        print(value)
    else:
        raise ValueError("unsupported plutil arguments: " + repr(args))
except (OSError, ValueError, KeyError, plistlib.InvalidFileException) as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
'''


class RoutineFixture:
    def __init__(self, *, launchctl: bool = True):
        self.temp = tempfile.TemporaryDirectory(
            prefix="routine-control-", dir=ROOT / "tests" / "hooks"
        )
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "Obsidian-Claude-Vibe-Squad"
        self.agents = self.base / "LaunchAgents"
        self.commands = self.base / "commands"
        for directory in (self.root / "launchd", self.root / "bin", self.agents, self.commands):
            directory.mkdir(parents=True)
        for label in (*MANAGED, CHROME):
            shutil.copyfile(ROOT / "launchd" / f"{label}.plist", self.root / "launchd" / f"{label}.plist")
        (self.base / "loaded.json").write_text("{}")
        (self.base / "calls.jsonl").write_text("")
        for command in (
            "sed", "dirname", "readlink", "id", "sort", "tr", "grep", "head",
            "mkdir", "mktemp", "cmp", "chmod", "mv", "rm", "basename", "awk",
        ):
            executable = shutil.which(command, path="/usr/bin:/bin:/usr/sbin:/sbin")
            if executable is None:
                raise RuntimeError(f"fixture requires file utility: {command}")
            (self.commands / command).symlink_to(executable)
        self.write_command("plutil", f"#!{sys.executable}\n" + PLIST_TOOL)
        self.write_command("uv", "#!/bin/bash\nexit 0\n")
        if launchctl:
            self.write_command("launchctl", f"#!{sys.executable}\n" + MOCK_LAUNCHCTL)
        self.env = {
            "HOME": os.environ["HOME"],
            "PATH": str(self.commands),
            "VAULT_ROOT": str(self.root),
            "SQUAD_LAUNCHAGENTS_DIR": str(self.agents),
            "ROUTINE_FIXTURE": str(self.base),
            "TMPDIR": str(self.base),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C",
        }

    def close(self):
        self.temp.cleanup()

    def write_command(self, name, body):
        path = self.commands / name
        path.write_text(body)
        path.chmod(0o755)

    def render(self, label):
        return (self.root / "launchd" / f"{label}.plist").read_text().replace(
            "__VAULT_ROOT__", str(self.root)
        ).replace("__HOME__", os.environ["HOME"]).encode()

    def seed(self, label, *, loaded=True, foreign=False):
        target = self.agents / f"{label}.plist"
        target.write_bytes(self.render(label))
        if loaded:
            state = self.state()
            state[label] = str(self.base / "foreign.plist" if foreign else target)
            (self.base / "loaded.json").write_text(json.dumps(state))
        return target

    def state(self):
        return json.loads((self.base / "loaded.json").read_text())

    def calls(self):
        return [json.loads(line) for line in (self.base / "calls.jsonl").read_text().splitlines()]

    def mutations(self):
        return [call for call in self.calls() if call[0] != "print"]

    def snapshot(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.agents.glob("*.plist")}

    def run(self, *args, labels=None, extra_env=None):
        environment = {**self.env, **(extra_env or {})}
        if labels is not None:
            environment["SQUAD_INSTALL_AGENTS"] = " ".join(labels)
        return subprocess.run(
            ["/bin/bash", str(ROOT / "bin" / "install-routines.sh"), *args],
            env=environment, cwd=self.root, capture_output=True, text=True, timeout=20,
        )


class InstallRoutinesOwnershipTest(unittest.TestCase):
    def fixture(self, **kwargs):
        fixture = RoutineFixture(**kwargs)
        self.addCleanup(fixture.close)
        return fixture

    def assert_exit(self, result, expected=0):
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_fixture_containment_and_existing_daemon_positive_control(self):
        fixture = self.fixture()
        self.assertEqual((fixture.commands / "launchctl").read_text().splitlines()[0], f"#!{sys.executable}")
        self.assertFalse((fixture.commands / "curl").exists())
        self.assertFalse((fixture.commands / "git").exists())
        self.assert_exit(fixture.run(labels=[DAEMON]))
        self.assertEqual(set(fixture.state()), {DAEMON})
        self.assertEqual(len(fixture.mutations()), 1)

    def test_default_install_adopts_monitor_and_preserves_operator_chrome(self):
        fixture = self.fixture()
        chrome = fixture.seed(CHROME)
        before = chrome.read_bytes(), fixture.state()[CHROME]
        result = fixture.run()
        self.assert_exit(result)
        self.assertTrue((fixture.agents / f"{MONITOR}.plist").exists())
        self.assertEqual(set(fixture.state()), {*MANAGED, CHROME})
        self.assertEqual((chrome.read_bytes(), fixture.state()[CHROME]), before)
        self.assertEqual(len(fixture.mutations()), 4)
        self.assertFalse(any(CHROME in " ".join(call) for call in fixture.mutations()))

    def test_existing_override_can_install_monitor_and_rollback_removal(self):
        fixture = self.fixture()
        self.assert_exit(fixture.run(labels=[MONITOR]))
        before = fixture.snapshot(), fixture.state()
        self.assert_exit(fixture.run("--uninstall", labels=[MONITOR]))
        self.assertEqual(fixture.snapshot(), {})
        self.assertEqual(fixture.state(), {})
        self.assert_exit(fixture.run(labels=[MONITOR]))
        self.assertEqual((fixture.snapshot(), fixture.state()), before)

    def test_uninstall_negative_control_removes_managed_jobs_but_cannot_reach_chrome(self):
        fixture = self.fixture()
        for label in (*MANAGED, CHROME):
            fixture.seed(label)
        chrome_hash = fixture.snapshot()[f"{CHROME}.plist"]
        chrome_state = fixture.state()[CHROME]
        before = len(fixture.snapshot()), len(fixture.state())
        result = fixture.run("--uninstall")
        self.assert_exit(result)
        self.assertEqual(fixture.snapshot(), {f"{CHROME}.plist": chrome_hash})
        self.assertEqual(fixture.state(), {CHROME: chrome_state})
        self.assertEqual({call[1].rsplit("/", 1)[-1] for call in fixture.mutations()}, set(MANAGED))
        self.assertTrue(all(call[0] == "bootout" for call in fixture.mutations()))
        print(f"NEGATIVE CONTROL uninstall exit={result.returncode}; plists/loaded {before}->(1, 1); managed bootouts=4; Chrome mutations=0")

    def test_override_rejects_chrome_before_any_install_or_uninstall_mutation(self):
        for args in ((), ("--force",), ("--uninstall",), ("--uninstall", "--force")):
            with self.subTest(args=args):
                fixture = self.fixture()
                fixture.seed(CHROME)
                before = fixture.snapshot(), fixture.state()
                result = fixture.run(*args, labels=[MONITOR, CHROME])
                self.assert_exit(result, 2)
                self.assertIn("operator-managed", result.stderr)
                self.assertEqual((fixture.snapshot(), fixture.state()), before)
                self.assertEqual(fixture.calls(), [])

    def test_path_shaped_override_is_rejected_before_mutation(self):
        for args in ((), ("--uninstall",)):
            with self.subTest(args=args):
                fixture = self.fixture()
                result = fixture.run(*args, labels=[f"../LaunchAgents/{CHROME}"])
                self.assert_exit(result, 2)
                self.assertIn("invalid agent label", result.stderr)
                self.assertEqual(fixture.calls(), [])

    def test_status_reports_both_owners_once_without_mutations(self):
        fixture = self.fixture()
        for label in (MONITOR, CHROME):
            fixture.seed(label)
        before = fixture.snapshot(), fixture.state()
        result = fixture.run("--status")
        self.assert_exit(result)
        for label, owner in ((MONITOR, "repo-managed"), (CHROME, "operator-managed")):
            rows = [line.split() for line in result.stdout.splitlines() if line.strip().startswith(label)]
            self.assertEqual(len(rows), 1, result.stdout)
            self.assertEqual(rows[0][1:4], ["installed", "loaded", owner])
        self.assertEqual((fixture.snapshot(), fixture.state()), before)
        self.assertEqual(fixture.mutations(), [])

    def test_status_reports_missing_and_not_loaded(self):
        fixture = self.fixture()
        result = fixture.run("--status")
        self.assert_exit(result)
        for label in (MONITOR, CHROME):
            row = next(line for line in result.stdout.splitlines() if line.strip().startswith(label))
            self.assertIn("missing", row)
            self.assertIn("not loaded", row)
        self.assertEqual(fixture.mutations(), [])

    def test_status_keeps_unknown_distinct_from_unloaded(self):
        for available, extra in ((False, {}), (True, {"MOCK_QUERY_RC": "64"}), (True, {"MOCK_NO_PATH": "1"})):
            with self.subTest(available=available, extra=extra):
                fixture = self.fixture(launchctl=available)
                for label in (MONITOR, CHROME):
                    fixture.seed(label)
                result = fixture.run("--status", extra_env=extra)
                self.assert_exit(result)
                for label in (MONITOR, CHROME):
                    row = next(line for line in result.stdout.splitlines() if line.strip().startswith(label))
                    self.assertIn("unknown", row)
                self.assertEqual(fixture.mutations(), [])

    def test_foreign_registration_is_reported_and_cannot_be_uninstalled(self):
        fixture = self.fixture()
        fixture.seed(MONITOR, foreign=True)
        before = fixture.snapshot(), fixture.state()
        status = fixture.run("--status")
        self.assert_exit(status)
        self.assertIn("foreign", status.stdout)
        self.assert_exit(fixture.run("--uninstall", labels=[MONITOR]), 1)
        self.assertEqual((fixture.snapshot(), fixture.state()), before)
        self.assertEqual(fixture.mutations(), [])

    def test_uninstall_retains_plist_on_unknown_or_failed_unload(self):
        for extra in ({"MOCK_QUERY_RC": "64"}, {"MOCK_NO_PATH": "1"}, {"MOCK_BOOTOUT_RC": "64"}, {"MOCK_BOOTOUT_STUCK": "1"}):
            with self.subTest(extra=extra):
                fixture = self.fixture()
                fixture.seed(MONITOR)
                before = fixture.snapshot(), fixture.state()
                self.assert_exit(fixture.run("--uninstall", labels=[MONITOR], extra_env=extra), 1)
                self.assertEqual((fixture.snapshot(), fixture.state()), before)

    def test_uninstall_without_launchctl_retains_plist(self):
        fixture = self.fixture(launchctl=False)
        fixture.seed(MONITOR)
        before = fixture.snapshot()
        self.assert_exit(fixture.run("--uninstall", labels=[MONITOR]), 1)
        self.assertEqual(fixture.snapshot(), before)

    def test_uninstall_can_remove_an_unloaded_monitor(self):
        fixture = self.fixture()
        fixture.seed(MONITOR, loaded=False)
        self.assert_exit(fixture.run("--uninstall", labels=[MONITOR]))
        self.assertEqual(fixture.snapshot(), {})
        self.assertEqual(fixture.mutations(), [])

    def test_uninstall_dry_run_preserves_files_and_loaded_jobs(self):
        fixture = self.fixture()
        for label in (*MANAGED, CHROME):
            fixture.seed(label)
        before = fixture.snapshot(), fixture.state()
        self.assert_exit(fixture.run("--uninstall", "--dry-run"))
        self.assertEqual((fixture.snapshot(), fixture.state()), before)
        self.assertEqual(fixture.mutations(), [])

    def test_daemon_only_does_not_expand_lifecycle_to_monitor_or_chrome(self):
        fixture = self.fixture()
        for label in (MONITOR, CHROME):
            fixture.seed(label)
        before = fixture.snapshot(), fixture.state()
        self.assert_exit(fixture.run("--daemon-only"))
        self.assert_exit(fixture.run("--daemon-only", "--uninstall"))
        self.assertEqual((fixture.snapshot(), fixture.state()), before)
        self.assertTrue(all(DAEMON in " ".join(call) for call in fixture.mutations()))

    def test_mislabeled_template_cannot_bootstrap_chrome(self):
        fixture = self.fixture()
        fixture.seed(CHROME)
        before = fixture.snapshot(), fixture.state()
        (fixture.root / "launchd" / f"{MONITOR}.plist").write_bytes(fixture.render(CHROME))
        self.assert_exit(fixture.run(labels=[MONITOR]), 1)
        self.assertEqual((fixture.snapshot(), fixture.state()), before)
        self.assertEqual(fixture.mutations(), [])

    def test_malformed_plist_cannot_be_installed_or_bootstrapped(self):
        fixture = self.fixture()
        (fixture.root / "launchd" / f"{MONITOR}.plist").write_text("not a plist\n")
        before = fixture.snapshot(), fixture.state()
        result = fixture.run(labels=[MONITOR])
        self.assert_exit(result, 1)
        self.assertIn("rendered plist failed plutil validation", result.stderr)
        self.assertEqual((fixture.snapshot(), fixture.state()), before)
        self.assertEqual(fixture.mutations(), [])

    def test_unknown_registration_cannot_trigger_bootstrap(self):
        fixture = self.fixture()
        self.assert_exit(fixture.run(labels=[MONITOR], extra_env={"MOCK_QUERY_RC": "64"}), 1)
        self.assertEqual(fixture.mutations(), [])

    def test_status_and_uninstall_are_mutually_exclusive(self):
        fixture = self.fixture()
        fixture.seed(MONITOR)
        before = fixture.snapshot(), fixture.state()
        result = fixture.run("--status", "--uninstall")
        self.assert_exit(result, 2)
        self.assertIn("mutually exclusive", result.stderr)
        self.assertEqual((fixture.snapshot(), fixture.state()), before)
        self.assertEqual(fixture.calls(), [])


if __name__ == "__main__":
    unittest.main()
