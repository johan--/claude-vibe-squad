"""Watcher recipient contract: shell fixtures and a private tmux server only."""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[3]
WATCHER = ROOT / "bin/outbox-watcher.sh"
HELPER = ROOT / "shared/chrono-pane.sh"


class WatcherPaneTargetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="watcher-pane-", dir="/tmp")))
        (self.root / "shared").mkdir()
        self.helper = self.root / "shared/chrono-pane.sh"
        self.log = self.root / "sends"
        self.resolutions = self.root / "resolutions"
        self.observations = self.root / "observations"
        self.tmux = self.root / "tmux"
        self.tmux.write_text(
            '#!/bin/bash\n'
            'case "$1" in\n'
            '  has-session) exit 0 ;;\n'
            '  list-windows) echo chrono ;;\n'
            '  send-keys) printf "<%s>" "$@" >> "$SEND_LOG"; printf "\\n" >> "$SEND_LOG"; exit "${SEND_STATUS:-0}" ;;\n'
            '  *) exit 99 ;;\n'
            'esac\n'
        )
        self.tmux.chmod(0o755)
        self.helper.write_text(
            'chrono_pane_resolve_coordinator_id() {\n'
            '  printf "%s\\n" "$1" >> "$RESOLVE_LOG"\n'
            '  printf "%s" "$RESOLVED_ID"\n'
            '  return "$RESOLVE_STATUS"\n'
            '}\n'
            'chrono_pane_has_coordinator() { chrono_pane_resolve_coordinator_id "$1" >/dev/null; }\n'
            'chrono_pane_observed_command() {\n'
            '  printf "%s\\n" "$1" >> "$OBSERVE_LOG"\n'
            '  echo fixture-command\n'
            '}\n'
        )
        self.env = {**os.environ, "VAULT_ROOT": str(self.root), "SQUAD_SESSION": "watcher-fixture",
                    "TMUX_BIN": str(self.tmux), "SEND_LOG": str(self.log),
                    "RESOLVE_LOG": str(self.resolutions), "RESOLVED_ID": "%7", "RESOLVE_STATUS": "0",
                    "OBSERVE_LOG": str(self.observations),
                    "TMUX_TMPDIR": str(self.root)}
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)

    def receipt(self, event="watcher-event"):
        name = hashlib.sha256(event.encode()).hexdigest() + ".sent"
        return self.root / "_state/chrono-notify-receipts" / name

    def notify(self, event="watcher-event", message="WATCHER-RECIPIENT-CONTROL"):
        return subprocess.run(["/bin/bash", str(WATCHER), "--notify-once", event, message],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_resolves_once_and_pins_both_sends_and_receipt(self):
        result = self.notify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.resolutions.read_text().splitlines(), ["watcher-fixture:chrono"])
        self.assertEqual(self.log.read_text().splitlines(), [
            "<send-keys><-l><-t><%7><[vibesquad] WATCHER-RECIPIENT-CONTROL>",
            "<send-keys><-t><%7><Enter>",
        ])
        self.assertIn("\ntarget=%7\n", self.receipt().read_text())
        self.assertEqual(self.notify().returncode, 0)
        self.assertEqual(len(self.log.read_text().splitlines()), 2)
        self.assertEqual(len(self.resolutions.read_text().splitlines()), 1)
        self.assertFalse(self.observations.exists(), "successful sends/replays must not probe diagnostics")

    def test_each_new_nudge_resolves_current_coordinator(self):
        self.assertEqual(self.notify().returncode, 0)
        self.env["RESOLVED_ID"] = "%8"
        self.assertEqual(self.notify("second-event").returncode, 0)
        self.assertEqual(self.resolutions.read_text().splitlines(), ["watcher-fixture:chrono"] * 2)
        self.assertIn("\ntarget=%8\n", self.receipt("second-event").read_text())

    def assert_skipped(self, result, event="watcher-event"):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("chrono pane nudge skipped:", result.stderr)
        self.assertIn("no notification lost: registry/outbox records persist", result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.receipt(event).exists())

    def test_failed_resolution_with_plausible_id_skips(self):
        self.env["RESOLVE_STATUS"] = "1"
        result = self.notify()
        self.assert_skipped(result)
        self.assertIn("observed command: fixture-command", result.stderr)
        self.assertEqual(self.observations.read_text().splitlines(), ["watcher-fixture:chrono"])

    def test_malformed_or_ambiguous_success_skips(self):
        for index, pane in enumerate(("", "watcher-fixture:chrono", "%7\n%8", "%x", "%7 extra", " %7")):
            with self.subTest(pane=pane):
                self.env["RESOLVED_ID"] = pane
                event = f"malformed-{index}"
                result = self.notify(event)
                self.assert_skipped(result, event)
                self.assertIn("observed command: fixture-command", result.stderr)
                self.assertEqual(self.observations.read_text().splitlines(), ["watcher-fixture:chrono"] * (index + 1))

    def test_failed_send_never_writes_receipt(self):
        self.env["SEND_STATUS"] = "1"
        result = self.notify()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertFalse(self.receipt().exists())
        self.assertFalse(self.observations.exists(), "send failures must not probe diagnostics")

    def rehearse(self, *, valid_identity=True):
        tmux = shutil.which("tmux")
        if tmux is None:
            self.skipTest("tmux executable unavailable")
        base = [tmux, "-L", "watcher-rehearsal", "-f", "/dev/null"]

        def call(*args, check=True):
            return subprocess.run(base + list(args), env=self.env, capture_output=True,
                                  text=True, timeout=10, check=check)

        receiver = "stty -echo; printf 'READY\\n'; exec /bin/cat"
        start = call("new-session", "-d", "-s", "watcher-fixture", "-n", "chrono", receiver, check=False)
        if start.returncode:
            self.skipTest(f"isolated tmux unavailable: {start.stderr.strip()}")
        self.stack.callback(lambda: call("kill-server", check=False))
        other = call("display-message", "-p", "-t", "watcher-fixture:chrono.0", "#{pane_id}").stdout.strip()
        pane = call("split-window", "-h", "-P", "-F", "#{pane_id}", "-t", other, receiver).stdout.strip()
        self.assertEqual(call("display-message", "-p", "-t", pane, "#{pane_index}").stdout.strip(), "1")
        pid = call("display-message", "-p", "-t", pane, "#{pane_pid}").stdout.strip()
        for target in (pane, other):
            deadline = time.monotonic() + 2
            while "READY" not in call("capture-pane", "-p", "-t", target).stdout:
                self.assertLess(time.monotonic(), deadline, "receiver did not start")
                time.sleep(0.02)
        call("select-pane", "-t", other)
        command = shlex.join(base)
        self.tmux.write_text(
            '#!/bin/bash\n'
            'if [[ "$1" == send-keys ]]; then\n'
            '  { printf "<%s>" "$@"; printf "\\n"; } >> "$SEND_LOG"\n'
            'fi\n'
            f'{command} "$@" || exit $?\n'
            'if [[ "$1" == send-keys && "$2" == -l ]]; then\n'
            f'  {command} select-pane -t {shlex.quote(other)}\n'
            'fi\n'
        )
        self.helper.write_text(HELPER.read_text())
        ps = self.root / "ps"
        ps.write_text(f'#!/bin/bash\nif [[ "$1" == -o ]]; then echo fixture-start; else echo "{pid} 1 S+ /fixture/claude"; fi\n')
        ps.chmod(0o755)
        pidfile = self.root / "coordinator.pid"
        pidfile.write_text(f"pid {pid}\nshape pane\ntarget watcher-fixture:chrono\nstart fixture-start\n" if valid_identity else "")
        self.env.update(PATH=f"{self.root}:{self.env['PATH']}", CHRONO_COORDINATOR_PIDFILE=str(pidfile))

        # Positive control uses the real helper before exercising the watcher.
        control = subprocess.run(["/bin/bash", "-c", 'source "$1"; chrono_pane_resolve_coordinator_id "$2"',
                                  "control", str(self.helper), "watcher-fixture:chrono"],
                                 env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(control.returncode, 0 if valid_identity else 1, control.stderr)
        self.assertEqual(control.stdout.strip(), pane if valid_identity else "")
        result = self.notify()
        if valid_identity:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.log.read_text().splitlines(), [
                f"<send-keys><-l><-t><{pane}><[vibesquad] WATCHER-RECIPIENT-CONTROL>",
                f"<send-keys><-t><{pane}><Enter>",
            ])
            self.assertIn(f"\ntarget={pane}\n", self.receipt().read_text())
            deadline = time.monotonic() + 2
            while "WATCHER-RECIPIENT-CONTROL" not in call("capture-pane", "-p", "-t", pane).stdout:
                self.assertLess(time.monotonic(), deadline, "coordinator did not receive text and Enter")
                time.sleep(0.02)
        else:
            self.assert_skipped(result)
            self.assertNotIn("WATCHER-RECIPIENT-CONTROL", call("capture-pane", "-p", "-t", pane).stdout)
        self.assertNotIn("WATCHER-RECIPIENT-CONTROL", call("capture-pane", "-p", "-t", other).stdout)

    def test_coordinator_in_pane_one_receives_text_enter_and_receipt(self):
        self.rehearse()

    def test_missing_identity_keeps_durable_skip_without_sending(self):
        self.rehearse(valid_identity=False)


if __name__ == "__main__":
    unittest.main()
