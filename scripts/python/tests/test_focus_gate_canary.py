#!/usr/bin/env python3
"""End-to-end regression canary for the coordinator focus-gate deny path."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from chrono_state import focus_gate, thread_charters, workboard  # noqa: E402


HOOK = ROOT / "bin" / "chrono-focus-gate.sh"
OUTPUT_STANDARD = ROOT / "docs" / "standards" / "operator-facing-output-standard.md"
OPAQUE_WORK_ID = "W-11111111111111111111111111111111"


class FocusGateCanaryTests(unittest.TestCase):
    """Drive the real shell hook against guarded synthetic ledgers."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="focus-gate-canary-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = self.root / "chrono-pane-fixture.sh"
        self.helper.write_text(
            "chrono_pane_has_coordinator() { return 0; }\n",
            encoding="utf-8",
        )

        self.valid_ledger = self._ledger_path("valid")
        self.wedged_ledger = self._ledger_path("wedged")
        self.idle_ledger = self._ledger_path("idle")
        self.waiting_ledger = self._ledger_path("waiting")
        start = workboard.format_event(
            "start",
            event_id="EV-CANARY-START",
            at="2026-09-06T07:16:00Z",
            work_id=OPAQUE_WORK_ID,
            alias="focus-canary",
            summary="exercise the real focus-gate entrypoint",
            why="the deny path needs a permanent regression canary",
            next_action="run every canary verdict",
        )
        self._write_ledger(self.valid_ledger, start)

        # append_event correctly refuses this postcondition.  The fixture must
        # bypass it with a schema-valid terminal line, not malformed text.
        complete = workboard.format_event(
            "complete",
            event_id="EV-CANARY-RAW-COMPLETE",
            at="2026-09-06T07:16:01Z",
            work_id=OPAQUE_WORK_ID,
        )
        self._write_ledger(self.wedged_ledger, start + complete)
        self._write_ledger(
            self.idle_ledger,
            workboard.format_event(
                "idle",
                event_id="EV-CANARY-IDLE",
                at="2026-09-06T07:16:02Z",
            ),
        )
        waiting_id = "W-22222222222222222222222222222222"
        self._write_ledger(
            self.waiting_ledger,
            start
            + workboard.format_event(
                "queue",
                event_id="EV-CANARY-QUEUE",
                at="2026-09-06T07:16:03Z",
                work_id=waiting_id,
                alias="waiting-canary",
                summary="resume after operator input",
                why="waiting turns also need the output reminder",
                resume_action="wait for the operator",
            )
            + workboard.format_event(
                "complete",
                event_id="EV-CANARY-WAITING",
                at="2026-09-06T07:16:04Z",
                work_id=OPAQUE_WORK_ID,
                waiting_work_id=waiting_id,
                resume_action="wait for the operator",
            ),
        )

        self._assert_fixture_states()

    def _ledger_path(self, name: str) -> Path:
        return self.root / name / workboard.WORKBOARD_REL

    @staticmethod
    def _write_ledger(path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True)
        path.write_text(contents, encoding="utf-8")

    def _assert_fixture_states(self) -> None:
        wedged = workboard.load_workboard(self.wedged_ledger)
        self.assertEqual(wedged.document.issues, ())
        self.assertEqual(wedged.transition_issues, ())
        self.assertIsNone(wedged.active_work_id)
        self.assertGreaterEqual(len(wedged.issues), 1)
        self.assertIn(
            "structured workboard must project exactly one active item; found 0",
            wedged.issues,
        )

        valid = workboard.load_workboard(self.valid_ledger)
        self.assertEqual(valid.issues, ())
        self.assertEqual(valid.active_work_id, OPAQUE_WORK_ID)

        idle = workboard.load_workboard(self.idle_ledger)
        self.assertEqual(idle.issues, ())
        self.assertTrue(idle.idle)
        self.assertIsNone(idle.active_work_id)

        waiting = workboard.load_workboard(self.waiting_ledger)
        self.assertEqual(waiting.issues, ())
        self.assertEqual(waiting.waiting_work_id, "W-22222222222222222222222222222222")
        self.assertIsNone(waiting.active_work_id)

    def _run_entrypoint(
        self,
        ledger: Path,
        payload: dict[str, object],
        *,
        cwd: Path | None = None,
        helper_body: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if helper_body is not None:
            self.helper.write_text(helper_body, encoding="utf-8")
        run_cwd = cwd or self.root
        environment = {
            **os.environ,
            "CHRONO_FOCUS_GATE_LEDGER": str(ledger),
            "CHRONO_FOCUS_GATE_PANE_HELPER": str(self.helper),
            "CHRONO_FOCUS_GATE_PYTHON": sys.executable,
            "CLAUDE_PROJECT_DIR": str(run_cwd),
            "TMUX_PANE": "%focus-gate-canary",
        }
        return subprocess.run(
            ["/bin/bash", str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=run_cwd,
            env=environment,
            timeout=10,
        )

    def _invoke(
        self,
        ledger: Path,
        event: str,
        **fields: object,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
        payload = {
            "cwd": str(self.root),
            "hook_event_name": event,
            **fields,
        }
        result = self._run_entrypoint(ledger, payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = json.loads(result.stdout) if result.stdout else None
        if (event == "UserPromptSubmit" and not OUTPUT_STANDARD.is_file()
                and ledger in (self.valid_ledger, self.idle_ledger, self.waiting_ledger)):
            # Public clones keep the focus anchor and explicitly warn that the
            # optional private reminder is unavailable. Continue testing the anchor.
            self.assertIsNotNone(rendered)
            warning = rendered["systemMessage"]
            self.assertIn("TURN-BOUNDARY FOCUS ANCHOR", warning)
            self.assertIn("could not read the operator output reminder", warning)
            self.assertEqual(result.stderr, warning + "\n")
        else:
            self.assertEqual(result.stderr, "")
        return result, rendered

    def _assert_allowed(self, rendered: dict[str, object] | None) -> None:
        if rendered is None:
            return
        self.assertNotEqual(rendered.get("decision"), "block")
        specific = rendered.get("hookSpecificOutput", {})
        self.assertIsInstance(specific, dict)
        self.assertNotEqual(specific.get("permissionDecision"), "deny")

    def _required_output(
        self, rendered: dict[str, object] | None
    ) -> dict[str, object]:
        self.assertIsNotNone(rendered)
        return rendered or {}

    def test_wedged_mutation_is_denied(self) -> None:
        _, rendered = self._invoke(
            self.wedged_ledger,
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "touch /tmp/focus-gate-canary"},
        )
        rendered = self._required_output(rendered)
        specific = rendered["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific.get("permissionDecision"), "deny")
        self.assertIn("FOCUS GATE BLOCK", specific["permissionDecisionReason"])

    def test_wedged_read_is_allowed(self) -> None:
        _, rendered = self._invoke(
            self.wedged_ledger,
            "PreToolUse",
            tool_name="Read",
            tool_input={"file_path": str(self.wedged_ledger)},
        )
        self._assert_allowed(rendered)
        rendered = self._required_output(rendered)
        self.assertIn(
            "Allowed current tool as read-only: Read",
            rendered["hookSpecificOutput"]["additionalContext"],
        )

    def test_wedged_first_stop_is_blocked(self) -> None:
        _, rendered = self._invoke(
            self.wedged_ledger,
            "Stop",
            stop_hook_active=False,
        )
        rendered = self._required_output(rendered)
        self.assertEqual(rendered["decision"], "block")
        self.assertIn("FOCUS GATE BLOCK", rendered["reason"])

    def test_wedged_recursive_stop_is_allowed(self) -> None:
        _, rendered = self._invoke(
            self.wedged_ledger,
            "Stop",
            stop_hook_active=True,
        )
        self._assert_allowed(rendered)
        self.assertIsNone(rendered)

    def test_wedged_emitted_repair_is_allowed(self) -> None:
        _, prompt = self._invoke(self.wedged_ledger, "UserPromptSubmit")
        prompt = self._required_output(prompt)
        context = prompt["hookSpecificOutput"]["additionalContext"]
        prefix = "REPAIR COMMAND: "
        command = next(
            line.removeprefix(prefix)
            for line in context.splitlines()
            if line.startswith(prefix)
        )
        ledger_repo = self.wedged_ledger.resolve().parents[
            len(workboard.WORKBOARD_REL.parts) - 1
        ]
        repair_program = (
            "from chrono_state import workboard; workboard.append_event('idle')"
        )
        expected_command = (
            f"VAULT_ROOT={shlex.quote(str(ledger_repo))} "
            f"PYTHONPATH={shlex.quote(str(ledger_repo / 'scripts' / 'python'))} "
            f"{shlex.quote(sys.executable)} -c "
            f"{shlex.quote(repair_program)}"
        )
        self.assertEqual(command, expected_command)

        _, rendered = self._invoke(
            self.wedged_ledger,
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": command},
        )
        self._assert_allowed(rendered)
        rendered = self._required_output(rendered)
        self.assertIn(
            "Allowed current tool as workboard-repair: Bash",
            rendered["hookSpecificOutput"]["additionalContext"],
        )

    def test_valid_mutation_is_allowed(self) -> None:
        _, rendered = self._invoke(
            self.valid_ledger,
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "touch /tmp/focus-gate-canary"},
        )
        self._assert_allowed(rendered)
        self.assertIsNone(rendered)

    def test_valid_stop_is_allowed(self) -> None:
        _, rendered = self._invoke(self.valid_ledger, "Stop")
        self._assert_allowed(rendered)
        self.assertIsNone(rendered)

    def test_valid_prompt_is_allowed_with_context(self) -> None:
        _, rendered = self._invoke(self.valid_ledger, "UserPromptSubmit")
        self._assert_allowed(rendered)
        rendered = self._required_output(rendered)
        specific = rendered["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        self.assertIn("additionalContext", specific)
        self.assertIn(
            "ACTIVE ITEM: exercise the real focus-gate",
            specific["additionalContext"],
        )

    @unittest.skipUnless(OUTPUT_STANDARD.exists(), "private input is not published")
    def test_valid_prompt_includes_operator_output_reminder(self) -> None:
        _, rendered = self._invoke(self.valid_ledger, "UserPromptSubmit")
        self._assert_allowed(rendered)
        rendered = self._required_output(rendered)
        context = rendered["hookSpecificOutput"]["additionalContext"]
        excerpt = re.search(
            r"(?ms)^<!-- focus-gate-reminder:start -->\n(.+?)\n"
            r"<!-- focus-gate-reminder:end -->$",
            OUTPUT_STANDARD.read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(excerpt, "canonical reminder excerpt is missing")
        assert excerpt is not None
        sentences = excerpt.group(1).splitlines()
        self.assertEqual(len(sentences), 4)
        for category in ("Plans", "findings", "comparisons", "status", "decisions"):
            self.assertIn(category, sentences[0])
        self.assertIn("unless the operator must act", sentences[1])
        self.assertIn("never substance", sentences[2])
        self.assertIn("Go-deep", sentences[3])
        self.assertIn("suspend this standard", sentences[3])
        self.assertEqual(
            context.splitlines()[-1],
            f"OPERATOR OUTPUT ({OUTPUT_STANDARD.relative_to(ROOT).as_posix()}): "
            + " ".join(sentences),
        )

    @unittest.skipUnless(OUTPUT_STANDARD.exists(), "private input is not published")
    def test_idle_and_waiting_prompts_include_the_same_reminder(self) -> None:
        _, active = self._invoke(self.valid_ledger, "UserPromptSubmit")
        active = self._required_output(active)
        reminder = active["hookSpecificOutput"]["additionalContext"].splitlines()[-1]
        self.assertTrue(reminder.startswith("OPERATOR OUTPUT ("))
        for ledger, focus in (
            (self.idle_ledger, "focus: declared idle"),
            (self.waiting_ledger, "focus: declared waiting on waiting-canary"),
        ):
            with self.subTest(ledger=ledger):
                _, rendered = self._invoke(ledger, "UserPromptSubmit")
                self._assert_allowed(rendered)
                rendered = self._required_output(rendered)
                context = rendered["hookSpecificOutput"]["additionalContext"]
                self.assertIn(focus, context)
                self.assertEqual(context.splitlines()[-1], reminder)

    def test_canonical_edits_change_the_next_prompt_reminder(self) -> None:
        standard = self.root / "output-standard.md"
        with mock.patch.object(focus_gate, "_OUTPUT_STANDARD_PATH", standard):
            for revision in ("first canonical revision", "second canonical revision"):
                with self.subTest(revision=revision):
                    standard.write_text(
                        "<!-- focus-gate-reminder:start -->\n"
                        f"{revision}\n<!-- focus-gate-reminder:end -->\n",
                        encoding="utf-8",
                    )
                    decision = focus_gate.decide(
                        focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.valid_ledger
                    )
                    self.assertTrue(decision.allow)
                    self.assertFalse(decision.fail_open)
                    self.assertTrue(decision.message.endswith(": " + revision))

    def test_reminder_read_or_format_failure_is_loud_but_open(self) -> None:
        standard = self.root / "broken-output-standard.md"
        start = "<!-- focus-gate-reminder:start -->"
        end = "<!-- focus-gate-reminder:end -->"
        cases = (
            ("missing", None),
            ("no markers", "unmarked text"),
            ("missing end", start + "\nreminder"),
            ("empty", start + "\n \n" + end),
            ("reversed", end + "\nreminder\n" + start),
            ("duplicate", start + start + "\nreminder\n" + end),
        )
        with mock.patch.object(focus_gate, "_OUTPUT_STANDARD_PATH", standard):
            for label, contents in cases:
                with self.subTest(case=label):
                    if contents is not None:
                        standard.write_text(contents, encoding="utf-8")
                    for ledger in (self.valid_ledger, self.idle_ledger, self.waiting_ledger):
                        decision = focus_gate.decide(
                            focus_gate.USER_PROMPT_SUBMIT, ledger_path=ledger
                        )
                        self.assertTrue(decision.allow)
                        self.assertTrue(decision.fail_open)
                        rendered = focus_gate.render_hook_output(
                            focus_gate.USER_PROMPT_SUBMIT, decision
                        )
                        self._assert_allowed(rendered)
                        rendered = self._required_output(rendered)
                        self.assertIn("FOCUS GATE WARNING", rendered["systemMessage"])
                        self.assertIn(
                            "TURN-BOUNDARY FOCUS ANCHOR",
                            rendered["hookSpecificOutput"]["additionalContext"],
                        )
                        self.assertIn(
                            OUTPUT_STANDARD.relative_to(ROOT).as_posix(),
                            rendered["systemMessage"],
                        )

    @unittest.skipUnless(OUTPUT_STANDARD.exists(), "private input is not published")
    def test_broken_standard_marker_preserves_complete_focus_anchor(self) -> None:
        canonical = OUTPUT_STANDARD.read_text(encoding="utf-8")
        standard = self.root / "copied-output-standard.md"
        expected_anchor = (
            "TURN-BOUNDARY FOCUS ANCHOR",
            f"active_work_id: {OPAQUE_WORK_ID}",
            "active_item_id: focus-canary",
            "ACTIVE ITEM: exercise the real focus-gate entrypoint",
            "WHY: the deny path needs a permanent regression canary",
            "NEXT ACTION: run every canary verdict",
            "NEXT ACTION SOURCE: workboard (active charters: 0, need exactly 1)",
            "Before any dispatch or mutation, classify the incoming message "
            "as FOLD / QUEUE / DROP relative to this active thread.",
            "Before any packet or plan edit that adds a mechanism (a cap, key, "
            "approval, claim step, schedule, or safety layer), quote the operator "
            "sentence that asked for it in your message; if none exists, do not add it.",
        )
        for marker in ("start", "end"):
            with self.subTest(marker=marker):
                original = f"<!-- focus-gate-reminder:{marker} -->"
                self.assertEqual(canonical.count(original), 1)
                standard.write_text(
                    canonical.replace(original, f"<!-- renamed-reminder:{marker} -->"),
                    encoding="utf-8",
                )
                with mock.patch.object(focus_gate, "_OUTPUT_STANDARD_PATH", standard):
                    decision = focus_gate.decide(
                        focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.valid_ledger
                    )
                self.assertTrue(decision.allow)
                self.assertTrue(decision.fail_open)
                rendered = self._required_output(
                    focus_gate.render_hook_output(focus_gate.USER_PROMPT_SUBMIT, decision)
                )
                self._assert_allowed(rendered)
                specific = rendered["hookSpecificOutput"]
                self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
                context = specific["additionalContext"]
                for line in expected_anchor:
                    self.assertIn(line, context.splitlines())
                self.assertNotIn("OPERATOR OUTPUT (", context)
                warning = rendered["systemMessage"]
                self.assertIn("FOCUS GATE WARNING", warning)
                self.assertIn("could not read the operator output reminder", warning)
                self.assertIn(OUTPUT_STANDARD.relative_to(ROOT).as_posix(), warning)
                self.assertNotIn("could not render the active focus", warning)

    def test_anchor_render_failure_retains_existing_fail_open_warning(self) -> None:
        for ledger, renderer in (
            (self.valid_ledger, "_active_context"),
            (self.idle_ledger, "_pause_context"),
            (self.waiting_ledger, "_pause_context"),
        ):
            with self.subTest(ledger=ledger), mock.patch.object(
                focus_gate, renderer, side_effect=RuntimeError("anchor canary")
            ):
                decision = focus_gate.decide(
                    focus_gate.USER_PROMPT_SUBMIT, ledger_path=ledger
                )
                self.assertTrue(decision.allow)
                self.assertTrue(decision.fail_open)
                expected_warning = (
                    "FOCUS GATE WARNING — fail open: "
                    "could not render the active focus: anchor canary"
                )
                self.assertEqual(decision.message, expected_warning)
                rendered = self._required_output(
                    focus_gate.render_hook_output(focus_gate.USER_PROMPT_SUBMIT, decision)
                )
                self._assert_allowed(rendered)
                self.assertEqual(rendered["systemMessage"], expected_warning)

    def test_reminder_reader_is_unreachable_from_denying_events(self) -> None:
        with mock.patch.object(
            focus_gate, "_operator_output_reminder", side_effect=RuntimeError("canary")
        ) as reader:
            # Positive control: the same injection is reached on a valid prompt.
            prompt = focus_gate.decide(
                focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.valid_ledger
            )
            reader.assert_called_once_with()
            self.assertTrue(prompt.allow)
            self.assertTrue(prompt.fail_open)
            self.assertIn("canary", prompt.message)
            reader.reset_mock()
            for ledger in (self.valid_ledger, self.wedged_ledger):
                for event in (focus_gate.PRE_TOOL_USE, focus_gate.STOP):
                    with self.subTest(ledger=ledger, event=event):
                        decision = focus_gate.decide(
                            event, ledger_path=ledger, tool_name="Write"
                        )
                        self.assertEqual(decision.allow, ledger == self.valid_ledger)
                        self.assertFalse(decision.fail_open)
                        self.assertNotIn("OPERATOR OUTPUT", decision.message)
            reader.assert_not_called()

    def _charter_dir(self, ledger: Path) -> Path:
        return ledger.parent / "thread-charters" / "active"

    def _write_charter(self, ledger: Path, name: str, loops: str) -> Path:
        directory = self._charter_dir(ledger)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.md"
        path.write_text(
            f"## THE ASK\nProve the anchor.\n\n## OPEN LOOPS\n{loops}\n\n"
            "## DONE-WHEN\n- [ ] anchor equals the newest receipt\n",
            encoding="utf-8",
        )
        return path

    def _anchor_lines(self, ledger: Path) -> dict[str, str]:
        _, rendered = self._invoke(ledger, "UserPromptSubmit")
        rendered = self._required_output(rendered)
        context = rendered["hookSpecificOutput"]["additionalContext"]
        return {
            line.partition(": ")[0]: line.partition(": ")[2]
            for line in context.splitlines()
            if line.startswith("NEXT ACTION")
        }

    def test_next_action_is_the_active_charters_newest_receipt(self) -> None:
        # Negative control first: without a charter the workboard pointer shows.
        lines = self._anchor_lines(self.valid_ledger)
        self.assertEqual(lines["NEXT ACTION"], "run every canary verdict")
        self.assertEqual(
            lines["NEXT ACTION SOURCE"], "workboard (active charters: 0, need exactly 1)"
        )

        charter = self._write_charter(
            self.valid_ledger,
            "anchor-thread",
            "- 2026-10-27T10:00:00Z | FOLD | first — why: w; resume: read the audit\n"
            "- 2026-10-27T11:00:00Z | QUEUE Q-001 | later — why: separate; resume: write compact()",
        )
        lines = self._anchor_lines(self.valid_ledger)
        self.assertEqual(lines["NEXT ACTION"], "write compact()")
        self.assertEqual(
            lines["NEXT ACTION SOURCE"],
            "charter anchor-thread, receipt 2026-10-27T11:00:00Z",
        )
        _, rendered = self._invoke(self.valid_ledger, "UserPromptSubmit")
        context = self._required_output(rendered)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("run every canary verdict", context)
        self.assertIn("ACTIVE ITEM: exercise the real focus-gate entrypoint", context)

        # The next receipt moves the anchor on the next prompt, through the real
        # writer: no workboard event is needed for the resume point to change.
        receipt = thread_charters.append_receipt(
            charter, "FOLD resolves Q-001", "done — why: landed; resume: run the canary suite"
        )
        lines = self._anchor_lines(self.valid_ledger)
        self.assertEqual(lines["NEXT ACTION"], "run the canary suite")
        self.assertEqual(
            lines["NEXT ACTION SOURCE"], f"charter anchor-thread, receipt {receipt.stamp}"
        )

    def test_ambiguous_or_receiptless_charters_fall_back_to_the_pointer(self) -> None:
        self._write_charter(self.valid_ledger, "one", "- (none)")
        lines = self._anchor_lines(self.valid_ledger)
        self.assertEqual(lines["NEXT ACTION"], "run every canary verdict")
        self.assertEqual(
            lines["NEXT ACTION SOURCE"], "workboard (charter one has no parseable receipt)"
        )
        self._write_charter(
            self.valid_ledger, "two",
            "- 2026-10-27T10:00:00Z | FOLD | x — why: w; resume: second thread point",
        )
        lines = self._anchor_lines(self.valid_ledger)
        self.assertEqual(lines["NEXT ACTION"], "run every canary verdict")
        self.assertEqual(
            lines["NEXT ACTION SOURCE"], "workboard (active charters: 2, need exactly 1)"
        )
        # A malformed newest line is skipped in favour of the newest parseable one.
        (self._charter_dir(self.valid_ledger) / "one.md").unlink()
        charter = self._charter_dir(self.valid_ledger) / "two.md"
        with charter.open("a", encoding="utf-8") as handle:
            handle.write("")
        text = charter.read_text(encoding="utf-8").replace(
            "second thread point", "second thread point\n- malformed receipt line"
        )
        charter.write_text(text, encoding="utf-8")
        lines = self._anchor_lines(self.valid_ledger)
        self.assertEqual(lines["NEXT ACTION"], "second thread point")

    @mock.patch.object(focus_gate, "_operator_output_reminder", return_value="fixture reminder")
    def test_charter_rail_failure_degrades_to_the_pointer_not_a_warning(self, _reminder) -> None:
        with mock.patch.object(
            thread_charters, "load_active_charters", side_effect=OSError("rail down")
        ):
            decision = focus_gate.decide(
                focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.valid_ledger
            )
        self.assertTrue(decision.allow)
        self.assertFalse(decision.fail_open)
        self.assertIn("NEXT ACTION: run every canary verdict", decision.message)
        self.assertIn("active charter rail unreadable: rail down", decision.message)

    def test_declared_idle_mutation_is_allowed(self) -> None:
        _, rendered = self._invoke(
            self.idle_ledger,
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "touch /tmp/focus-gate-canary"},
        )
        self._assert_allowed(rendered)
        self.assertIsNone(rendered)

    def test_declared_idle_stop_is_allowed(self) -> None:
        _, rendered = self._invoke(self.idle_ledger, "Stop")
        self._assert_allowed(rendered)
        self.assertIsNone(rendered)

    def test_missing_or_unreadable_ledger_fails_open_with_warning(self) -> None:
        unreadable = self.root / "ledger-directory"
        unreadable.mkdir()
        for label, ledger in (
            ("missing", self.root / "missing.md"),
            ("unreadable", unreadable),
        ):
            with self.subTest(ledger=label):
                result = self._run_entrypoint(
                    ledger,
                    {
                        "cwd": str(self.root),
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "tool_input": {"command": "touch /tmp/focus-gate-canary"},
                    },
                )
                self.assertEqual(result.returncode, 0)
                rendered = json.loads(result.stdout)
                self._assert_allowed(rendered)
                self.assertIn("FOCUS GATE WARNING — fail open", rendered["systemMessage"])
                self.assertIn("FOCUS GATE WARNING — fail open", result.stderr)

    def test_symlink_ledger_fails_open_with_warning(self) -> None:
        symlink = self.root / "linked-ledger.md"
        symlink.symlink_to(self.valid_ledger)
        result = self._run_entrypoint(
            symlink,
            {
                "cwd": str(self.root),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "touch /tmp/focus-gate-canary"},
            },
        )
        self.assertEqual(result.returncode, 0)
        rendered = json.loads(result.stdout)
        self._assert_allowed(rendered)
        expected_warning = (
            "FOCUS GATE WARNING — fail open: workboard is not a readable "
            f"regular file: {symlink}"
        )
        self.assertEqual(rendered["systemMessage"], expected_warning)
        self.assertEqual(result.stderr, expected_warning + "\n")

    def test_board_worktree_cwd_is_a_silent_noop(self) -> None:
        worker_cwd = self.root / "_state" / "board-worktrees" / "worker-1"
        worker_cwd.mkdir(parents=True)
        marker = self.root / "pane-helper-was-sourced"
        result = self._run_entrypoint(
            self.wedged_ledger,
            {
                "cwd": str(worker_cwd),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "touch /tmp/focus-gate-canary"},
            },
            cwd=worker_cwd,
            helper_body=(
                f"touch {marker}\n"
                "chrono_pane_has_coordinator() { return 0; }\n"
            ),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
