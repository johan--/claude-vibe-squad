#!/usr/bin/env python3
"""Focused tests for the inert Chrono turn-boundary hook."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from chrono_state import focus_gate, workboard  # noqa: E402


HOOK = ROOT / "bin" / "chrono-focus-gate.sh"


class FocusGateHookTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tempdir = Path(temporary.name)
        self.ledger = self.tempdir / workboard.WORKBOARD_REL
        self.ledger.parent.mkdir(parents=True)
        self.event_index = 0

    def append(self, kind: str, **facts: str) -> workboard.WorkEvent:
        self.event_index += 1
        return workboard.append_event(
            kind,
            path=self.ledger,
            event_id=f"EV-HOOK-{self.event_index:02d}",
            at=f"2026-09-05T21:00:{self.event_index:02d}Z",
            **facts,
        )

    def active_board(self) -> workboard.WorkEvent:
        return self.append(
            "start",
            item_id="FOCUS-A",
            summary="ship the focus hook",
            why="the three boundary decisions are tested and remain inert",
            next_action="run the literal hook test",
        )

    def invalid_zero_board(self) -> workboard.WorkboardProjection:
        active = self.active_board()
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(
                workboard.format_event(
                    "complete",
                    event_id="EV-HOOK-RAW-COMPLETE",
                    at="2026-09-05T21:01:00Z",
                    work_id=active.fields["work_id"],
                )
            )
        projection = workboard.load_workboard(self.ledger)
        self.assertIn("found 0", " ".join(projection.issues))
        return projection

    def waiting_board(self) -> None:
        active = self.active_board()
        queued = self.append(
            "queue",
            item_id="FOCUS-B",
            summary="resume after operator input",
            why="the pause is explicit",
            resume_action="wait for the operator",
        )
        self.append(
            "complete",
            work_id=active.fields["work_id"],
            waiting_work_id=queued.fields["work_id"],
            resume_action="wait for the operator",
        )

    def run_entrypoint(
        self,
        payload: dict[str, object],
        *,
        helper_body: str,
        cwd: Path | None = None,
        project_dir: Path | None = None,
        python_bin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        helper = self.tempdir / "chrono-pane-fixture.sh"
        helper.write_text(helper_body, encoding="utf-8")
        environment = {
            **os.environ,
            "CHRONO_FOCUS_GATE_PANE_HELPER": str(helper),
            "CHRONO_FOCUS_GATE_LEDGER": str(self.ledger),
            "CHRONO_FOCUS_GATE_PYTHON": python_bin or sys.executable,
            "CLAUDE_PROJECT_DIR": str(project_dir or cwd or self.tempdir),
            "TMUX_PANE": "%fixture",
        }
        return subprocess.run(
            ["/bin/bash", str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=cwd or self.tempdir,
            env=environment,
            timeout=10,
        )

    def test_valid_active_focus_allows_everything_and_injects_anchor(self) -> None:
        self.active_board()

        write = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            ledger_path=self.ledger,
            tool_name="Write",
            tool_input={"file_path": "/tmp/example"},
        )
        stop = focus_gate.decide(focus_gate.STOP, ledger_path=self.ledger)
        prompt = focus_gate.decide(
            focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger
        )

        self.assertTrue(write.allow)
        self.assertTrue(stop.allow)
        self.assertTrue(prompt.allow)
        self.assertIn("ACTIVE ITEM: ship the focus hook", prompt.message)
        self.assertIn(
            "WHY: the three boundary decisions are tested",
            prompt.message,
        )
        self.assertIn("NEXT ACTION: run the literal hook test", prompt.message)
        self.assertIn(
            "NEXT ACTION SOURCE: workboard (active charters: 0, need exactly 1)",
            prompt.message,
        )
        self.assertNotIn("THE ASK:", prompt.message)
        self.assertNotIn("DONE-WHEN:", prompt.message)
        self.assertIn("FOLD / QUEUE / DROP", prompt.message)
        self.assertIn(focus_gate.ANCHOR_MECHANISM, prompt.message)
        self.assertIn("quote the operator sentence that asked for it", prompt.message)
        self.assertNotIn("Do not dispatch or mutate until", prompt.message)

    def harness_notices(self) -> tuple[str, ...]:
        task = "TASK-2026-09-22-1555-7f5479e7"
        notices = (
            f"auto-closed: {task}; details: _state/chrono-queue.md.",
            f"review-required: {task}; details: _state/chrono-queue.md.",
            f"coordination-requested: {task}; details: _state/chrono-queue.md. Coordination requested; see queue.",
            "BOARD DIGEST: complete=4; details: _state/chrono-queue.md.",
            f"BOARD DIGEST: blocked=1, coordination-requested=1; action: blocked: {task}; coordination-requested: TASK-other; details: _state/chrono-queue.md. Coordination requested in 1 notice(s); see queue.",
            f"MONITOR: task {task} → gpt-codex lane (GPT/Codex) idle 30m, no response yet (dispatched 40m ago); pending in coding/inbox",
            f"MONITOR: observed task {task} → claude lane (Claude): live board spawn attempt log idle 1200s, no receipt (threshold 900s; dispatched 30m ago); a long model turn or tool call may still be healthy",
            f"MONITOR: REVIEW-REQUIRED: coding/{task}.md parked 6m (needs_review); coordinator action overdue",
            f"MONITOR: coding namespace has stale active task ({task}.md, 31m old)",
            f"MONITOR: coding namespace received same task 3x in 10m - real thrash (repeated task id {task})",
            "MONITOR: coding namespace received same task 3x in 10m - real thrash (duplicate body)",
            "MONITOR: NOT_MEASURED thrash coding: skipped_task_ids=2 measured_task_ids=0 skipped_log_rows=1",
        )
        return notices + tuple(f"[vibesquad] {notice}" for notice in notices)

    def test_repeated_or_malformed_notice_tags_keep_anchor(self) -> None:
        self.active_board()
        baseline = focus_gate.decide(focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger)
        notice = self.harness_notices()[0]
        for prefix in ("[vibesquad] [vibesquad] ", "[vibesquad]", "[vibesquad]  ",
                       "[vibesquad]\t", "[VIBESQUAD] "):
            with self.subTest(prefix=prefix):
                prompt = prefix + notice
                self.assertFalse(focus_gate.is_harness_notice(prompt))
                self.assertEqual(focus_gate.decide(
                    focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger, prompt=prompt
                ), baseline)

    def test_notice_only_cli_prompts_emit_no_anchor(self) -> None:
        self.active_board()
        for notice in self.harness_notices():
            with self.subTest(notice=notice):
                result = self.run_entrypoint(
                    {"hook_event_name": focus_gate.USER_PROMPT_SUBMIT, "prompt": notice},
                    helper_body="chrono_pane_has_coordinator() { return 0; }\n",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")

    def test_operator_text_with_notice_keeps_identical_anchor(self) -> None:
        self.active_board()
        baseline = focus_gate.decide(focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger)
        for notice in self.harness_notices():
            for prompt in ("Please investigate. " + notice, notice + " Please investigate.",
                           notice + "\nPlease investigate.", "Please investigate.\n" + notice):
                with self.subTest(prompt=prompt):
                    decision = focus_gate.decide(
                        focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger, prompt=prompt
                    )
                    self.assertEqual(decision, baseline)
        result = self.run_entrypoint(
            {"hook_event_name": focus_gate.USER_PROMPT_SUBMIT,
             "prompt": "[vibesquad] " + self.harness_notices()[0] + " Please investigate."},
            helper_body="chrono_pane_has_coordinator() { return 0; }\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"], baseline.message)

    def test_unrecognized_or_malformed_prompts_keep_anchor(self) -> None:
        self.active_board()
        baseline = focus_gate.decide(focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger)
        for prompt in (None, {}, [], 1, "", "hello", "MONITOR: please change focus",
                       "BOARD DIGEST: please inspect these tasks", "review-required: TASK-X",
                       "auto-closed: TASK-X; details: elsewhere.md.",
                       self.harness_notices()[0] + "\n" + self.harness_notices()[1]):
            with self.subTest(prompt=prompt):
                self.assertEqual(focus_gate.decide(
                    focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger, prompt=prompt
                ), baseline)

    def test_notice_classifier_error_keeps_anchor_and_allows_prompt(self) -> None:
        self.active_board()
        baseline = focus_gate.decide(focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger)
        with mock.patch.object(focus_gate, "is_harness_notice", side_effect=ValueError("fixture")):
            self.assertEqual(focus_gate.decide(
                focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger, prompt=self.harness_notices()[0]
            ), baseline)

    def test_current_reconciler_emitters_match_notice_grammar(self) -> None:
        from registry_reconciler import compact_notice, notification_digest

        statuses = ("complete", "REVIEW-REQUIRED", "needs_human", "needs_review", "blocked", "COORDINATION-REQUESTED")
        events = [(status, f"coding/TASK-fixture-{index}", "detail", "Coordination requested")
                  for index, status in enumerate(statuses)]
        notices = [compact_notice(status, task, detail) for status, task, _, detail in events]
        notices.append(notification_digest(events))
        for notice in notices:
            with self.subTest(notice=notice):
                self.assertTrue(focus_gate.is_harness_notice(notice))
                self.assertFalse(focus_gate.is_harness_notice(notice + " Please investigate."))

    def test_notice_does_not_bypass_tool_or_stop_focus_checks(self) -> None:
        projection = self.invalid_zero_board()
        for event in (focus_gate.PRE_TOOL_USE, focus_gate.STOP):
            with self.subTest(event=event):
                self.assertFalse(focus_gate.decide(
                    event, projection=projection, tool_name="Write", prompt=self.harness_notices()[0]
                ).allow)

    def test_undeclared_zero_blocks_mutation_but_allows_read(self) -> None:
        projection = self.invalid_zero_board()

        mutation = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="Write",
            tool_input={"file_path": "/tmp/example"},
        )
        dispatch = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="Task",
            tool_input={"description": "drift"},
        )
        read = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="Read",
            tool_input={"file_path": str(self.ledger)},
        )
        shell_read = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="Bash",
            tool_input={"command": f"rg 'next_action|resume_action' {self.ledger}"},
        )

        self.assertFalse(mutation.allow)
        self.assertFalse(dispatch.allow)
        self.assertTrue(read.allow)
        self.assertTrue(shell_read.allow)
        self.assertIn("NEXT ACTION: <none projected", mutation.message)
        self.assertNotIn("next_action", mutation.message)

    def test_invalid_focus_allows_only_canonical_append_repair(self) -> None:
        projection = self.invalid_zero_board()
        repair = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="Bash",
            tool_input={
                "command": (
                    "python -c \"from chrono_state import workboard; "
                    "workboard.append_event('idle')\""
                )
            },
        )
        unrelated_python = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="Bash",
            tool_input={"command": "python -c \"print('not a repair')\""},
        )
        repair_with_extra_mutation = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="Bash",
            tool_input={
                "command": (
                    "python -c \"from chrono_state import workboard; "
                    "workboard.append_event('idle'); "
                    "__import__('os').system('touch /tmp/not-a-repair')\""
                )
            },
        )
        repair_with_other_path = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="Bash",
            tool_input={
                "command": (
                    "python -c \"from chrono_state import workboard; "
                    "workboard.append_event('idle', path='/tmp/other-ledger')\""
                )
            },
        )
        read_named_mutator = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            projection=projection,
            tool_name="mcp__store__get_or_create",
        )
        self.assertTrue(repair.allow)
        self.assertIn(focus_gate.REPAIR, repair.message)
        self.assertFalse(unrelated_python.allow)
        self.assertFalse(repair_with_extra_mutation.allow)
        self.assertFalse(repair_with_other_path.allow)
        self.assertFalse(read_named_mutator.allow)

    def test_emitted_repair_command_runs_clean_and_changes_the_ledger(self) -> None:
        projection = self.invalid_zero_board()
        decision = focus_gate.decide(
            focus_gate.USER_PROMPT_SUBMIT,
            projection=projection,
        )
        prefix = "REPAIR COMMAND: "
        command = next(
            line.removeprefix(prefix)
            for line in decision.message.splitlines()
            if line.startswith(prefix)
        )
        self.assertEqual(
            focus_gate.tool_disposition("Bash", {"command": command}),
            focus_gate.REPAIR,
        )

        scripts_dir = self.tempdir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "python").symlink_to(PYTHON_DIR, target_is_directory=True)

        command_words = focus_gate._simple_shell_words(command)
        self.assertIsNotNone(command_words)
        assert command_words is not None
        ledger_repo = self.tempdir.resolve()
        self.assertEqual(
            command_words[:3],
            [
                f"VAULT_ROOT={ledger_repo}",
                f"PYTHONPATH={ledger_repo / 'scripts' / 'python'}",
                sys.executable,
            ],
        )

        outside = tempfile.TemporaryDirectory(
            dir=self.tempdir.parent,
            prefix=f"{self.tempdir.name}-outside-",
        )
        self.addCleanup(outside.cleanup)
        outside_cwd = Path(outside.name)
        stray_ledger = outside_cwd / workboard.WORKBOARD_REL

        before_count = len(projection.document.records)
        clean_environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.assertNotIn("PYTHONPATH", clean_environment)
        self.assertNotIn("VAULT_ROOT", clean_environment)
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            cwd=outside_cwd,
            env=clean_environment,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        repaired = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(len(repaired.document.records), before_count + 1)
        self.assertIsInstance(repaired.document.records[-1], workboard.WorkEvent)
        self.assertEqual(repaired.document.records[-1].kind, "idle")
        self.assertTrue(repaired.idle)
        self.assertFalse(stray_ledger.exists())

    def test_every_focus_validator_issue_is_classified(self) -> None:
        self.active_board()
        base = workboard.load_workboard(self.ledger, strict=True)
        active = base.items[0]
        queued = replace(active, state="queued")
        waiting = replace(active, state="waiting")
        missing_work_id = "W-" + "f" * 32
        cases = {
            "zero active": replace(
                base,
                items=(queued,),
                active_work_id=None,
                active_item_id=None,
                next_action=None,
            ),
            "missing next action": replace(base, next_action=None),
            "blank next action": replace(base, next_action=" "),
            "idle and waiting": replace(
                base,
                items=(waiting,),
                active_work_id=None,
                active_item_id=None,
                next_action=None,
                idle=True,
                waiting_work_id=active.work_id,
            ),
            "missing waiting item": replace(
                base,
                items=(queued,),
                active_work_id=None,
                active_item_id=None,
                next_action=None,
                waiting_work_id=missing_work_id,
            ),
        }

        seen: set[str] = set()
        for label, projection in cases.items():
            with self.subTest(issue=label):
                issues = workboard.validate_workboard(
                    projection.document,
                    projection,
                )
                self.assertEqual(len(issues), 1, issues)
                self.assertTrue(focus_gate._focus_issue(issues[0]), issues[0])
                seen.add(issues[0])
        self.assertEqual(len(seen), 5)

    def test_declared_idle_allows_everything(self) -> None:
        self.append("idle")
        projection = workboard.load_workboard(self.ledger, strict=True)
        self.assertTrue(projection.idle)
        self.assertTrue(
            focus_gate.decide(
                focus_gate.PRE_TOOL_USE,
                projection=projection,
                tool_name="Task",
            ).allow
        )
        self.assertTrue(focus_gate.decide(focus_gate.STOP, projection=projection).allow)

    def test_declared_waiting_allows_everything(self) -> None:
        self.waiting_board()
        projection = workboard.load_workboard(self.ledger, strict=True)
        self.assertIsNotNone(projection.waiting_work_id)
        self.assertTrue(
            focus_gate.decide(
                focus_gate.PRE_TOOL_USE,
                projection=projection,
                tool_name="Write",
            ).allow
        )
        prompt = focus_gate.decide(
            focus_gate.USER_PROMPT_SUBMIT, projection=projection
        )
        self.assertIn("declared waiting on FOCUS-B", prompt.message)

    def test_unreadable_or_malformed_ledger_fails_open_loudly(self) -> None:
        missing = focus_gate.decide(
            focus_gate.PRE_TOOL_USE,
            ledger_path=self.tempdir / "missing.md",
            tool_name="Write",
        )
        self.ledger.write_text(
            "- workboard-event/v1 this is not a valid structured event\n",
            encoding="utf-8",
        )
        malformed = focus_gate.decide(
            focus_gate.STOP,
            ledger_path=self.ledger,
        )

        for decision in (missing, malformed):
            with self.subTest(decision=decision):
                self.assertTrue(decision.allow)
                self.assertTrue(decision.fail_open)
                self.assertIn("FOCUS GATE WARNING", decision.message)

    def test_stop_refuses_invalid_focus_and_returns_literal_next_action(self) -> None:
        self.active_board()
        valid = workboard.load_workboard(self.ledger, strict=True)
        invalid = replace(valid, idle=True)

        decision = focus_gate.decide(focus_gate.STOP, projection=invalid)
        rendered = focus_gate.render_hook_output(focus_gate.STOP, decision)

        self.assertFalse(decision.allow)
        self.assertEqual(rendered["decision"], "block")
        self.assertIn("NEXT ACTION: run the literal hook test", rendered["reason"])
        self.assertNotIn("next_action", rendered["reason"])

        recursive = focus_gate.decide(
            focus_gate.STOP,
            projection=invalid,
            stop_hook_active=True,
        )
        self.assertTrue(recursive.allow)
        self.assertIsNone(focus_gate.render_hook_output(focus_gate.STOP, recursive))

    def test_cli_emits_current_claude_json_shapes(self) -> None:
        self.active_board()
        prompt = self.run_entrypoint(
            {
                "cwd": str(self.tempdir),
                "hook_event_name": focus_gate.USER_PROMPT_SUBMIT,
                "prompt": "new operator message",
            },
            helper_body="chrono_pane_has_coordinator() { return 0; }\n",
        )
        self.assertEqual(prompt.returncode, 0, prompt.stderr)
        payload = json.loads(prompt.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"],
            focus_gate.USER_PROMPT_SUBMIT,
        )

        self.ledger = self.tempdir / "invalid-OPEN-WORK.md"
        self.event_index = 0
        self.invalid_zero_board()
        blocked = self.run_entrypoint(
            {
                "cwd": str(self.tempdir),
                "hook_event_name": focus_gate.PRE_TOOL_USE,
                "tool_name": "Write",
                "tool_input": {"file_path": "/tmp/example"},
            },
            helper_body="chrono_pane_has_coordinator() { return 0; }\n",
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        payload = json.loads(blocked.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["permissionDecision"], "deny"
        )

        stopped = self.run_entrypoint(
            {
                "cwd": str(self.tempdir),
                "hook_event_name": focus_gate.STOP,
                "stop_hook_active": False,
            },
            helper_body="chrono_pane_has_coordinator() { return 0; }\n",
        )
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        payload = json.loads(stopped.stdout)
        self.assertEqual(payload["decision"], "block")

        recursive_stop = self.run_entrypoint(
            {
                "cwd": str(self.tempdir),
                "hook_event_name": focus_gate.STOP,
                "stop_hook_active": True,
            },
            helper_body="chrono_pane_has_coordinator() { return 0; }\n",
        )
        self.assertEqual(recursive_stop.returncode, 0, recursive_stop.stderr)
        self.assertEqual(recursive_stop.stdout, "")

    def test_worktree_self_guard_is_a_true_noop(self) -> None:
        worker_cwd = self.tempdir / "_state" / "board-worktrees" / "worker-1"
        worker_cwd.mkdir(parents=True)
        cases = (
            ("working-directory", worker_cwd, worker_cwd),
            ("project-directory", self.tempdir, worker_cwd),
        )
        for label, cwd, project_dir in cases:
            with self.subTest(guard=label):
                marker = self.tempdir / f"pane-helper-called-{label}"
                result = self.run_entrypoint(
                    {
                        "cwd": str(cwd),
                        "hook_event_name": focus_gate.PRE_TOOL_USE,
                        "tool_name": "Write",
                        "tool_input": {"file_path": "/tmp/example"},
                    },
                    cwd=cwd,
                    project_dir=project_dir,
                    python_bin="/definitely/not/a/python",
                    helper_body=(
                        "chrono_pane_has_coordinator() { "
                        f"touch {shlex_quote(marker)}; return 0; }}\n"
                    ),
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
                self.assertFalse(marker.exists())
        self.assertTrue(focus_gate.is_board_worktree_path(worker_cwd))

    def test_non_coordinator_context_is_a_true_noop(self) -> None:
        self.active_board()
        result = self.run_entrypoint(
            {
                "cwd": str(self.tempdir),
                "hook_event_name": focus_gate.USER_PROMPT_SUBMIT,
                "prompt": "hello",
            },
            helper_body="chrono_pane_has_coordinator() { return 1; }\n",
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_entrypoint_python_failure_is_loud_but_open(self) -> None:
        self.active_board()
        broken_python = self.tempdir / "broken-python"
        broken_python.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        broken_python.chmod(0o755)
        result = self.run_entrypoint(
            {
                "cwd": str(self.tempdir),
                "hook_event_name": focus_gate.PRE_TOOL_USE,
                "tool_name": "Write",
            },
            helper_body="chrono_pane_has_coordinator() { return 0; }\n",
            python_bin=str(broken_python),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("FOCUS GATE WARNING", result.stdout)
        self.assertIn("Python hook exited 7", result.stderr)


def shlex_quote(path: Path) -> str:
    """Quote one fixture path without importing a shell in production code."""

    import shlex

    return shlex.quote(str(path))


if __name__ == "__main__":
    unittest.main()
