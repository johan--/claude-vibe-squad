"""Monitor recipient controls: fixture resolution and a private tmux server.

Only send_alert is extracted; the monitor's detectors and host state are never
run. Real tmux routing uses fixture ps observations, never the host process table.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[3]
MONITOR = ROOT / "bin" / "squad-monitor.sh"
HELPER = ROOT / "shared" / "chrono-pane.sh"


def monitor_env(root):
    return {**os.environ, "VAULT_ROOT": str(root), "now": "1800000000",
            "sent_alerts": "0", "SQUAD_TEST_ISOLATION": "1",
            "NOTIFICATION_MODULE": str(ROOT / "scripts/python/chrono_state/notifications.py")}


def monitor_send_alert():
    match = re.search(r"(?ms)^send_alert\(\) \{\n.*?^\}\n", MONITOR.read_text())
    if match is None:
        raise AssertionError("send_alert is missing from squad-monitor.sh")
    return match.group(0)


class MonitorPaneTargetTests(unittest.TestCase):
    def run_alert(self, pane_id="%7", status=0, *, again=False):
        shell = r'''
set -uo pipefail
alerts=0
CHRONO_PANE=monitor-fixture:chrono
chrono_pane_resolve_coordinator_id() {
    [[ "$1" == "$CHRONO_PANE" ]] || return 2
    printf '%s' "$RESOLVED_ID"
    return "$RESOLVE_STATUS"
}
chrono_pane_has_coordinator() { chrono_pane_resolve_coordinator_id "$1" >/dev/null; }
chrono_pane_observed_command() { printf fixture-command; }
tmux() { printf '<%s>' "$@"; printf '\n'; }
''' + monitor_send_alert() + '\nsend_alert "fixture alert" first\n'
        if again:
            shell += 'RESOLVED_ID=%8\nsend_alert "second alert" second\n'
        shell += 'printf "TOTAL=%s\\n" "$alerts"\n'
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["bash", "-c", shell], capture_output=True, text=True, timeout=10,
                env={**monitor_env(Path(directory)), "RESOLVED_ID": pane_id,
                     "RESOLVE_STATUS": str(status)},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_skipped(self, output):
        self.assertNotIn("<send-keys>", output)
        self.assertIn("pane nudge skipped:", output)
        self.assertIn("coordinator pane unresolved or ambiguous", output)
        self.assertIn("ALERT: fixture alert", output)
        self.assertIn("TOTAL=1", output)

    def test_both_keystrokes_use_validated_pane_id(self):
        output = self.run_alert()
        self.assertIn("<send-keys><-l><-t><%7><[vibesquad] MONITOR: fixture alert>", output)
        self.assertIn("<send-keys><-t><%7><><Enter>", output)
        self.assertIn("ALERT: fixture alert", output)
        self.assertIn("TOTAL=1", output)
        self.assertNotIn("pane nudge skipped", output)

    def test_coordinator_is_resolved_again_for_each_alert(self):
        output = self.run_alert(again=True)
        self.assertIn("<send-keys><-l><-t><%7><[vibesquad] MONITOR: fixture alert>", output)
        self.assertIn("<send-keys><-t><%7><><Enter>", output)
        self.assertIn("<send-keys><-l><-t><%8><[vibesquad] MONITOR: second alert>", output)
        self.assertIn("<send-keys><-t><%8><><Enter>", output)
        self.assertIn("TOTAL=2", output)

    def test_failed_resolution_never_sends_even_if_stdout_has_an_id(self):
        self.assert_skipped(self.run_alert(status=1))

    def test_missing_or_ambiguous_coordinator_preserves_durable_alert(self):
        self.assert_skipped(self.run_alert(pane_id="", status=1))

    def test_success_without_one_exact_pane_id_fails_closed(self):
        for pane_id in ("", "monitor-fixture:chrono", "%7\n%8", "%x", "%7 extra"):
            with self.subTest(pane_id=pane_id):
                self.assert_skipped(self.run_alert(pane_id=pane_id))


class IsolatedMonitorTmuxRehearsal(unittest.TestCase):
    def rehearse(self, *, bystander_active=False, valid_identity=True):
        tmux = shutil.which("tmux")
        if tmux is None:
            self.skipTest("tmux executable unavailable")
        with tempfile.TemporaryDirectory(prefix="monitor-tmux-", dir="/tmp") as directory, ExitStack() as stack:
            root = Path(directory)
            env = {**monitor_env(root), "TMUX_TMPDIR": str(root)}
            env.pop("TMUX", None)
            env.pop("TMUX_PANE", None)
            base = [tmux, "-L", "monitor-rehearsal", "-f", "/dev/null"]

            def call(*args, check=True):
                return subprocess.run(base + list(args), env=env, capture_output=True,
                                      text=True, timeout=10, check=check)

            # No terminal echo: a captured alert proves Enter reached cat too.
            receiver = "stty -echo; printf 'READY\\n'; exec /bin/cat"
            start = call("new-session", "-d", "-s", "monitor-fixture", "-n", "chrono",
                         receiver, check=False)
            if start.returncode:
                self.skipTest(f"isolated tmux unavailable (rc={start.returncode}): {start.stderr.strip()}")
            stack.callback(lambda: call("kill-server", check=False))
            pane = call("display-message", "-p", "-t", "monitor-fixture:chrono.0", "#{pane_id}").stdout.strip()
            pid = call("display-message", "-p", "-t", pane, "#{pane_pid}").stdout.strip()
            other = call("split-window", "-h", "-P", "-F", "#{pane_id}", "-t", pane, receiver).stdout.strip()
            for target in (pane, other):
                deadline = time.monotonic() + 2
                while "READY" not in call("capture-pane", "-p", "-t", target).stdout:
                    self.assertLess(time.monotonic(), deadline, "receiver did not start")
                    time.sleep(0.02)
            call("select-pane", "-t", other if bystander_active else pane)

            wrapper = root / "tmux"
            log = root / "sends"
            command = shlex.join(base)
            wrapper.write_text(
                '#!/bin/bash\n'
                'if [[ "$1" == send-keys ]]; then\n'
                f'    {{ printf "<%s>" "$@"; printf "\\n"; }} >> {shlex.quote(str(log))}\n'
                'fi\n'
                f'{command} "$@" || exit $?\n'
                'if [[ "$1" == send-keys && "$2" == -l ]]; then\n'
                f'    {command} select-pane -t {shlex.quote(other)}\n'
                'fi\n'
            )
            wrapper.chmod(0o755)
            ps = root / "ps"
            ps.write_text(f'#!/bin/bash\nif [[ "$1" == -o ]]; then echo fixture-start; else echo "{pid} 1 S+ /fixture/claude"; fi\n')
            ps.chmod(0o755)
            pidfile = root / "coordinator.pid"
            pidfile.write_text(
                f"pid {pid}\nshape pane\ntarget monitor-fixture:chrono\nstart fixture-start\n"
                if valid_identity else ""
            )
            env.update(PATH=f"{root}:{env['PATH']}", TMUX_BIN=str(wrapper),
                       CHRONO_COORDINATOR_PIDFILE=str(pidfile))
            shell = ('set -uo pipefail\nsource "$1"\n'
                     'CHRONO_PANE=monitor-fixture:chrono\nalerts=0\n'
                     + monitor_send_alert() + '\nsend_alert MONITOR-RECIPIENT-CONTROL\n')
            result = subprocess.run(["bash", "-c", shell, "monitor-rehearsal", str(HELPER)],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ALERT: MONITOR-RECIPIENT-CONTROL", result.stdout)
            if valid_identity:
                expected = [
                    f"<send-keys><-l><-t><{pane}><[vibesquad] MONITOR: MONITOR-RECIPIENT-CONTROL>",
                    f"<send-keys><-t><{pane}><><Enter>",
                ]
                self.assertTrue(log.exists(), result.stdout)
                self.assertEqual(log.read_text().splitlines(), expected)
                deadline = time.monotonic() + 2
                while "MONITOR: MONITOR-RECIPIENT-CONTROL" not in call("capture-pane", "-p", "-J", "-t", pane).stdout:
                    self.assertLess(time.monotonic(), deadline, "coordinator did not receive text and Enter")
                    time.sleep(0.02)
                self.assertNotIn("pane nudge skipped", result.stdout)
            else:
                self.assertFalse(log.exists(), log.read_text() if log.exists() else "")
                self.assertIn("pane nudge skipped:", result.stdout)
                self.assertNotIn("MONITOR-RECIPIENT-CONTROL", call("capture-pane", "-p", "-J", "-t", pane).stdout)
            self.assertNotIn("MONITOR-RECIPIENT-CONTROL", call("capture-pane", "-p", "-J", "-t", other).stdout)

    def test_active_pane_switch_between_text_and_enter_keeps_recipient(self):
        self.rehearse()

    def test_bystander_already_active_never_receives_alert(self):
        self.rehearse(bystander_active=True)

    def test_missing_identity_sends_no_keys_but_keeps_durable_echo(self):
        self.rehearse(valid_identity=False)


if __name__ == "__main__":
    unittest.main()
