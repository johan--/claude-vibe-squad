from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SEGMENT = ROOT / "bin" / "vs-needs-you-segment.sh"


class NeedsYouSegmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="needs-you-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "vault with spaces"
        for directory in ("bin", "shared", "_state/chrono"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.script = self.root / "bin" / SEGMENT.name
        shutil.copy2(SEGMENT, self.script)
        shutil.copy2(ROOT / "shared/repo-root.sh", self.root / "shared/repo-root.sh")
        self.questions = self.root / "_state/chrono/awaiting-operator.md"
        self.environment = {**os.environ, "VAULT_ROOT": str(self.root), "TZ": "Pacific/Honolulu"}
        self.now = datetime.now(timezone.utc)

    def question(self, minutes: int, text: str = "Choose the region?") -> str:
        # Half-minute margin keeps the real-clock integration check away from
        # minute boundaries without adding a production clock override.
        asked = self.now - timedelta(minutes=minutes, seconds=30)
        return f"- {asked.strftime('%Y-%m-%dT%H:%M:%SZ')} | {text} | chrono pane\n"

    def render(self) -> str:
        completed = subprocess.run(
            ["/bin/bash", str(self.script)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            cwd=self.temporary.name,
            env=self.environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        return completed.stdout

    def assert_badge(self, count: int, minutes: int) -> None:
        self.assertEqual(
            self.render(),
            f"#[fg=colour213,bold]◆ needs you: {count} · oldest {minutes}m#[default] ",
        )

    def test_absent_file_is_silent(self) -> None:
        self.assertFalse(self.questions.exists())
        self.assertEqual(self.render(), "")
        # Control: the same checkout can read a real open question.
        self.questions.write_text(self.question(2), encoding="utf-8")
        self.assert_badge(1, 2)

    def test_empty_file_is_silent_and_answering_clears_the_badge(self) -> None:
        self.questions.write_text("", encoding="utf-8")
        self.assertEqual(self.render(), "")
        self.questions.write_text(self.question(3), encoding="utf-8")
        self.assert_badge(1, 3)
        self.questions.write_text("", encoding="utf-8")
        self.assertEqual(self.render(), "")

    def test_two_questions_use_oldest_timestamp_not_file_order(self) -> None:
        newest, oldest = self.question(2), self.question(14)
        self.questions.write_text(newest, encoding="utf-8")
        self.assert_badge(1, 2)  # control before the older question is appended
        self.questions.write_text(newest + oldest, encoding="utf-8")
        self.assert_badge(2, 14)
        self.questions.write_text(oldest + newest, encoding="utf-8")
        self.assert_badge(2, 14)

    def test_malformed_lines_are_skipped_with_a_valid_control(self) -> None:
        valid = self.question(4)
        malformed = [
            "# Chrono questions\n\n",
            "[vibesquad] specialist needs_human\n",
            "- not-a-date | question? | pane\n",
            "- 2026-02-30T12:00:00Z | impossible date | pane\n",
            "- 2026-01-01T25:00:00Z | impossible time | pane\n",
            "- 2026-01-01T12:00:00 | no timezone | pane\n",
            "- 2026-01-01T12:00:00+02:00 | not UTC | pane\n",
            "- 2026-01-01T12:00:00Z | missing location\n",
            "- 2026-01-01T12:00:00Z |   | pane\n",
            "- 2026-01-01T12:00:00Z | question? |   \n",
            "- 2026-01-01T12:00:00Z | question? | pane | extra\n",
        ]
        for line in malformed:
            with self.subTest(line=line):
                self.questions.write_text(line, encoding="utf-8")
                self.assertEqual(self.render(), "")
                self.questions.write_text(line + valid, encoding="utf-8")
                self.assert_badge(1, 4)

    def test_utc_variants_whitespace_and_missing_final_newline(self) -> None:
        self.questions.write_text(self.question(6), encoding="utf-8")
        self.assert_badge(1, 6)
        fractional = self.question(14).replace("Z |", ".123456+00:00 |")
        self.questions.write_text(" \t" + fractional.rstrip() + " \t\r\n", encoding="utf-8")
        self.assert_badge(1, 14)
        self.questions.write_text(fractional.rstrip(), encoding="utf-8")
        self.assert_badge(1, 14)

    def test_future_timestamp_does_not_show_negative_age(self) -> None:
        self.questions.write_text(self.question(1), encoding="utf-8")
        self.assert_badge(1, 1)
        future = self.now + timedelta(minutes=10)
        self.questions.write_text(f"- {future.isoformat()} | Future? | pane\n", encoding="utf-8")
        self.assert_badge(1, 0)

    def test_specialist_state_never_counts_as_operator_questions(self) -> None:
        (self.root / "_state/active-tasks.json").write_text(
            json.dumps({"TASK-specialist": {"status": "needs_human"}}), encoding="utf-8"
        )
        self.assertEqual(self.render(), "")
        # Control: a real Chrono question contributes one, not two.
        self.questions.write_text(self.question(7), encoding="utf-8")
        self.assert_badge(1, 7)

    def test_checkout_root_is_resolved_without_an_override(self) -> None:
        self.environment.pop("VAULT_ROOT")
        self.assertEqual(self.render(), "")
        self.questions.write_text(self.question(3), encoding="utf-8")
        self.assert_badge(1, 3)

    def test_question_text_is_never_rendered_as_tmux_format(self) -> None:
        self.questions.write_text(self.question(2, "Approve #[fg=red]this?"), encoding="utf-8")
        self.assert_badge(1, 2)  # only the controlled label is emitted
        self.questions.write_text(self.question(2, "Ordinary question?"), encoding="utf-8")
        self.assert_badge(1, 2)


if __name__ == "__main__":
    unittest.main()
