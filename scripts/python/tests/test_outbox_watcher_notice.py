"""Prove missing registration, rather than resume flags, stops pane notices."""

import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.python.tests.test_chrono_pane import CoordinatorFixture, HELPER, ROOT


class OutboxWatcherResumeNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="outbox-resume-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = CoordinatorFixture(self.root)
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "chrono-pane.sh").write_text(HELPER.read_text())
        self.sends = self.root / "sends"
        original = self.fixture.tmux.read_text()
        self.fixture.tmux.write_text(
            '#!/bin/bash\n'
            'case "$1" in\n'
            '  has-session) exit 0 ;;\n'
            '  list-windows) echo chrono; exit 0 ;;\n'
            '  send-keys) printf "<%s>" "$@" >> "$SEND_LOG"; '
            'printf "\\n" >> "$SEND_LOG"; exit 0 ;;\n'
            'esac\n' + original
        )
        self.fixture.env.update(VAULT_ROOT=str(self.root), SQUAD_SESSION="squad", SEND_LOG=str(self.sends))

    def notify(self, event="resume-control"):
        return subprocess.run(
            ["/bin/bash", str(ROOT / "bin/outbox-watcher.sh"), "--notify-once", event, "[vibesquad] test"],
            env=self.fixture.env, capture_output=True, text=True, timeout=10,
        )

    def receipt(self, event="resume-control"):
        digest = hashlib.sha256(event.encode()).hexdigest()
        return self.root / "_state/chrono-notify-receipts" / f"{digest}.sent"

    def test_missing_record_skips_without_receipt_and_registration_restores_same_event(self):
        self.fixture.env["CHRONO_COORDINATOR_PIDFILE"] = str(self.root / "absent.pid")
        result = self.notify()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("chrono pane identity unavailable:", result.stderr)
        self.assertFalse(self.sends.exists())
        self.assertFalse(self.receipt().exists())

        # Same resumed process, target, message and event. Only registration changes.
        self.fixture.env["CHRONO_COORDINATOR_PIDFILE"] = str(self.fixture.pidfile)
        result = self.notify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.sends.read_text().splitlines(), [
            "<send-keys><-l><-t><%0><[vibesquad] test>",
            "<send-keys><-t><%0><Enter>",
        ])
        self.assertIn("\ntarget=%0\n", self.receipt().read_text())
        result = self.notify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.sends.read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
