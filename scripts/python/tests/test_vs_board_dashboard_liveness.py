"""A dashboard card must never call a running lane idle.

Regression for 2026-09-22: the card read one signal, the mtime of the lane's
log, and printed "⚠ idle Xm" after 120s of silence. Claude streams to its log so
that held there, but the agy lane writes nothing until it finishes and codex
writes only when a step completes -- so both showed "idle" while working
normally. The operator asked about it three times in one day; every one of those
lanes was alive.

The second signal was already on the card's own record: the spawn's pid. A card
exists at all only because vs-board-snapshot.py found no terminal receipt, so a
pid that is GONE means the CLI died without writing one -- that is "stopped",
which the old rendering also could not say.

No tmux, no board state: the activity line is exercised directly, plus one
rendered card to prove the wiring.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import time
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD = REPO_ROOT / "bin" / "vs-board-dashboard.py"


def _load_dashboard():
    """Import bin/vs-board-dashboard.py with colour off.

    Both VAULT_ROOT (the module exits without it) and NO_COLOR are read at
    import time, so they are set before exec and restored afterwards."""
    saved = {key: os.environ.get(key) for key in ("VAULT_ROOT", "NO_COLOR")}
    os.environ["VAULT_ROOT"] = str(REPO_ROOT)
    os.environ["NO_COLOR"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("vs_board_dashboard", DASHBOARD)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


DASH = _load_dashboard()
WIDE = 40  # display cells available to the activity line on a roomy card


def _legacy_activity(idle_s):
    """The pre-fix rule, kept so the defect is reproduced rather than described."""
    if idle_s < 0:
        return ""
    return f"⚠ idle {idle_s // 60}m{idle_s % 60:02d}" if idle_s > 120 else "● live"


class ActivityLineTest(unittest.TestCase):
    def test_the_defect_the_old_rule_called_a_quiet_lane_idle(self) -> None:
        self.assertIn("idle", _legacy_activity(600))

    def test_quiet_log_plus_live_process_reads_as_working(self) -> None:
        line = DASH._activity_line(600, True, WIDE)
        self.assertIn("working", line)
        self.assertIn("quiet", line)
        self.assertNotIn("idle", line)

    def test_quiet_log_plus_dead_process_reads_as_stopped(self) -> None:
        line = DASH._activity_line(600, False, WIDE)
        self.assertIn("stopped", line)
        self.assertNotIn("idle", line)
        self.assertNotIn("working", line)

    def test_growing_log_still_reads_as_live_whatever_the_process_says(self) -> None:
        for alive in (True, False, None):
            with self.subTest(alive=alive):
                self.assertIn("live", DASH._activity_line(5, alive, WIDE))

    def test_unprobeable_pid_claims_nothing_about_the_process(self) -> None:
        """Control: an unmeasured process is reported as unmeasured, not as
        stopped and not as working."""
        line = DASH._activity_line(600, None, WIDE)
        self.assertIn("pid unknown", line)
        self.assertNotIn("working", line)
        self.assertNotIn("stopped", line)
        # An unreadable log AND an unprobeable pid measured nothing at all.
        self.assertEqual(DASH._activity_line(-1, None, WIDE), "")

    def test_unreadable_log_still_trusts_the_process(self) -> None:
        self.assertIn("working", DASH._activity_line(-1, True, WIDE))
        self.assertIn("stopped", DASH._activity_line(-1, False, WIDE))

    def test_working_label_degrades_instead_of_truncating_on_a_narrow_card(self) -> None:
        for width in (9, 14, 18, 40):
            with self.subTest(width=width):
                line = DASH._activity_line(600, True, width)
                self.assertIn("working", line)
                self.assertNotIn("…", line)
                self.assertLessEqual(DASH._dwidth(line), width)


class ProcessAliveTest(unittest.TestCase):
    def test_a_running_pid_is_alive_and_a_reaped_one_is_not(self) -> None:
        proc = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertIs(DASH._process_alive(proc.pid), True)
        self.assertIs(DASH._process_alive(str(proc.pid)), True)

        gone = subprocess.Popen(["/bin/sleep", "0"])
        gone.wait()
        self.assertIs(DASH._process_alive(gone.pid), False)

    def test_pids_that_cannot_be_probed_return_none(self) -> None:
        for pid in (0, -1, "", None, "not-a-pid"):
            with self.subTest(pid=pid):
                self.assertIsNone(DASH._process_alive(pid))


class RenderedCardTest(unittest.TestCase):
    """The wiring, not just the rule: the pid on the spawn record reaches it."""

    def _card_text(self, pid, log_age_s):
        log = Path(self.tmp) / "lane.log"
        log.write_text("...")
        stamp = time.time() - log_age_s
        os.utime(log, (stamp, stamp))
        spawn = {
            "task_id": "TASK-TEST", "lane": "gemini", "specialist": "backend-engineer",
            "started": str(int(time.time()) - 900), "pid": pid, "log": str(log),
            "summary": "a task", "model": "gemini",
        }
        # A CLI render starts a fresh clock; discovery may import this module
        # many minutes before this fixture runs.
        with mock.patch.object(DASH, "NOW", int(time.time())):
            return "\n".join(DASH._render_card(spawn, 46))

    def setUp(self) -> None:
        import tempfile
        import shutil
        self.tmp = tempfile.mkdtemp(prefix="vs-dash-liveness-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_silent_but_living_lane_renders_working_not_idle(self) -> None:
        proc = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        text = self._card_text(proc.pid, 900)
        self.assertIn("working", text)
        self.assertNotIn("idle", text)

    def test_a_dead_spawn_with_no_receipt_renders_stopped(self) -> None:
        gone = subprocess.Popen(["/bin/sleep", "0"])
        gone.wait()
        text = self._card_text(gone.pid, 900)
        self.assertIn("stopped", text)
        self.assertNotIn("idle", text)

    def test_a_streaming_lane_still_renders_live(self) -> None:
        proc = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        self.assertIn("live", self._card_text(proc.pid, 3))


if __name__ == "__main__":
    unittest.main()
