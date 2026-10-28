"""Exercise supervisor recovery, blocked publication, and real launch timeouts."""

from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/python"))
import dispatch_context_builder as dcb  # noqa: E402
from board_process_truth import ProcessTruthError  # noqa: E402


SOURCE = (ROOT / "bin/board-supervisor.sh").read_text(encoding="utf-8")
MARKER = "validated-artifact-after-cli-transport-failure/v1"
TASK = "TASK-2026-09-23-0242-f13e35dc"
ARTIFACT = f"departments/coding/outbox/{TASK}-response.md"


def recovery_namespace():
    region = SOURCE.split("# BEGIN transport-output-quarantine", 1)[1].split(
        "# END transport-output-quarantine", 1
    )[0]
    namespace = {
        "replace": replace,
        "signal": signal,
        "DispatchContextError": dcb.DispatchContextError,
        "ModeExitVerificationError": dcb.ModeExitVerificationError,
        "CONTROLLER_QUARANTINE_MARKER": MARKER,
        "CLI_DIAGNOSTIC_EXCERPT_BYTES": 2048,
        "write_board_note": mock.Mock(),
    }
    exec(compile(region, "board-supervisor.sh:recovery", "exec"), namespace)
    return namespace


class RecoveryStatusTests(unittest.TestCase):
    def check_recovery(self, status, *, held):
        summary = (
            "Initial response placeholder; investigation unfinished."
            if held else
            "Implemented the requested change and verified its regression tests."
        )
        envelope = (
            f"---\nid: {TASK}-response\nin_response_to: {TASK}\n"
            f"from: gpt-codex\nto: chrono\ntype: RESULT\nstatus: {status}\n"
            f"return_artifact: {ARTIFACT}\n---\n\n{summary}\n"
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            repo, worker = Path(directory) / "repo", Path(directory) / "worker"
            repo.mkdir()
            response = worker / ARTIFACT
            response.parent.mkdir(parents=True)
            response.write_bytes(envelope)
            authority = {
                "task_id": TASK, "lane": "codex", "write_paths": [ARTIFACT],
                "expected_result_path": ARTIFACT, "expected_outbox_path": ARTIFACT,
            }
            namespace = recovery_namespace()
            namespace.update(
                repo_path=repo,
                prepare_current_worktree_outputs=lambda: dcb.prepare_worktree_outputs(
                    repo, worker, authority
                ),
                publish_prepared_worktree_outputs=mock.Mock(
                    wraps=dcb.publish_prepared_worktree_outputs
                ),
            )
            receipt = namespace["recover_validated_outputs_for_transport_failure"]()
            self.assertEqual(response.read_bytes(), envelope)
            if held:
                self.assertIsNone(receipt)
                namespace["publish_prepared_worktree_outputs"].assert_not_called()
                self.assertFalse((repo / ARTIFACT).exists())
                note = namespace["write_board_note"].call_args.args[0]
                self.assertIn("outputs were not promoted", note)
                self.assertNotIn("retained worktree", note)
            else:
                self.assertIsNotNone(receipt, namespace["write_board_note"].call_args)
                self.assertEqual(receipt["response_status"], "needs_review")
                self.assertEqual(receipt["controller_quarantine"], MARKER)
                published = (repo / ARTIFACT).read_text()
                self.assertIn("status: needs_review\n", published)
                self.assertIn(summary, published)
                self.assertIn(MARKER, published)

    def test_worker_holds_and_aliases_are_not_promoted(self):
        for status in (
            "blocked", "needs_human", "BLOCKED", "failed", "blocked_on_review",
            "needs human", "needs human review", "awaiting operator approval", "garbage",
        ):
            with self.subTest(status=status):
                self.check_recovery(status, held=True)

    def test_completed_spellings_and_review_candidates_are_published(self):
        # Real prepare/publish helpers reproduce the review's completed regression.
        for status in (
            "completed", "COMPLETED", "complete", "done",
            "complete_with_scoped_exclusion", "needs_review",
        ):
            with self.subTest(status=status):
                self.check_recovery(status, held=False)


def blocked_detail(receipt):
    code = SOURCE.split("<<'PYBLOCKED'", 1)[1].split("\n", 1)[1].split(
        "\nPYBLOCKED", 1
    )[0]
    output = io.StringIO()
    with (
        mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(receipt))),
        mock.patch.object(sys, "argv", ["-", "receipt.json", ARTIFACT]),
        redirect_stdout(output),
    ):
        exec(compile(code, "board-supervisor.sh:PYBLOCKED", "exec"), {})
    return output.getvalue().strip()


class BlockedDetailTests(unittest.TestCase):
    def test_real_preservation_shapes_remain_distinct_in_blocked_envelopes(self):
        ref = "refs/heads/worktree/" + TASK
        commit = "b" * 40
        worktree = "/retained/attempt"
        details = {}
        for status in (
            "preserved", "preserved_existing", "preserved_partial",
            "none", "retained_bounded", "error",
        ):
            with self.subTest(status=status):
                snapshot = status.startswith("preserved")
                preservation = {
                    "status": status,
                    "evidence_ref": ref,
                    "evidence_commit": commit if snapshot else "",
                    "evidence_location": f"{ref}@{commit}" if snapshot else (
                        worktree if status == "retained_bounded" else ""
                    ),
                    "worktree_location": worktree,
                }
                if status == "error":
                    # Production salvage errors have no ref/commit fields.
                    preservation.pop("evidence_ref")
                    preservation.pop("evidence_commit")
                    preservation["reason"] = "cannot commit terminal evidence"
                detail = blocked_detail({
                    "reason": "diagnostic " * 1000,
                    "evidence_preservation": preservation,
                })
                details[status] = detail
                self.assertIn(f"preservation status: {status}", detail)
                self.assertIn("unfinished work", detail)
                self.assertIn(f"declared artifact: {ARTIFACT}", detail)
                self.assertIn(f"retained worktree: {worktree}", detail)
                self.assertLess(detail.index(ARTIFACT), detail.index("diagnostic"))
                self.assertLessEqual(len(detail.split(" | ")[-1]), 600)
                if snapshot:
                    self.assertIn(f"snapshot: {ref}@{commit}", detail)
                else:
                    self.assertNotIn("snapshot:", detail)
                    self.assertNotIn(ref, detail)
                if status == "none":
                    self.assertIn("nothing was preserved", detail)
                if status == "error":
                    self.assertIn("salvage failed", detail)
                with tempfile.TemporaryDirectory() as directory:
                    receipt = dcb.publish_blocked_completion(
                        repo_root=Path(directory), task_id=TASK, lane="codex",
                        return_artifact=ARTIFACT, reason=detail,
                    )
                    published = Path(receipt["envelope_path"]).read_text()
                    self.assertIn("status: blocked\n", published)
                    self.assertIn(detail, published)
                    self.assertNotIn("status: needs_review", published)
        self.assertEqual(len(set(details.values())), len(details))
        # Absence controls: a bare failure must not claim preserved work.
        self.assertEqual(blocked_detail({"reason": "x" * 1000}), "x" * 600)
        self.assertEqual(blocked_detail({}), "")


class FixtureReaper:
    """Reap only session leaders registered by this test's production launcher.

    Host process-identity discovery is outside this timeout-reason test. Actual
    child processes, pipes, drain threads, deadlines and waits are used unchanged.
    """

    def __init__(self):
        self.groups = set()

    def register(self, pid):
        self.groups.add(pid)

    def terminate(self, pid):
        if pid in self.groups:
            os.killpg(pid, signal.SIGKILL)

    def unregister(self, pid):
        self.groups.discard(pid)


class TimeoutReasonTests(unittest.TestCase):
    def test_real_pipe_and_remaining_wait_timeouts_report_configured_budget(self):
        launcher = textwrap.dedent("    def trusted_real_launcher(" + SOURCE.split(
            "    def trusted_real_launcher(", 1
        )[1].split("    selected_launcher = (", 1)[0])
        handler = textwrap.dedent(SOURCE.split(
            "    except subprocess.TimeoutExpired as exc:\n", 1
        )[1].split("    except (FileNotFoundError", 1)[0])
        command_prompt = "PRIVATE_PROMPT_SENTINEL " * 1000
        for close_pipes in (False, True):
            with self.subTest(close_pipes=close_pipes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                transcript = root / "transcript.log"
                child_code = (
                    "import os,sys,time\n"
                    "print('partial output', flush=True)\n"
                    "print('diagnostic', file=sys.stderr, flush=True)\n"
                    + ("os.close(1); os.close(2)\n" if close_pipes else "")
                    + "time.sleep(30)\n"
                )
                command = (sys.executable, "-c", child_code, "--prompt", command_prompt)
                prepared = SimpleNamespace(canary=SimpleNamespace(passed=True))
                reaper = FixtureReaper()
                namespace = recovery_namespace()
                namespace.update(
                    subprocess=subprocess, sys=sys, os=os, time=time,
                    select=select, threading=threading,
                    ProcessGroupReaper=lambda: reaper, ProcessTruthError=ProcessTruthError,
                    worker_cwd=root, prepared=prepared, trusted_environment=dict(os.environ),
                    task_id=TASK, attempt_id="d-timeout", repo_path=root,
                    launch_timeout=float(1), command=command,
                    _board_stdout_streamed=[False], write_board_transcript=mock.Mock(),
                    recover_validated_outputs_for_transport_failure=mock.Mock(return_value=None),
                    block_after_provision=mock.Mock(),
                )
                with transcript.open("wb") as mirror, mock.patch.dict(
                    os.environ, {"BOARD_TRANSCRIPT_FD_VALUE": str(mirror.fileno())}
                ):
                    exec(compile(launcher, "board-supervisor.sh:launcher", "exec"), namespace)
                    # The exception comes from production's real deadline/wait;
                    # do not construct TimeoutExpired with a hand-picked number.
                    run = (
                        "try:\n"
                        "    trusted_real_launcher(lambda: prepared, command, timeout=launch_timeout)\n"
                        "except subprocess.TimeoutExpired as exc:\n"
                        "    observed_timeout = exc.timeout\n"
                        + textwrap.indent(handler, "    ")
                    )
                    try:
                        exec(compile(run, "board-supervisor.sh:timeout", "exec"), namespace)
                    finally:
                        for pid in tuple(reaper.groups):
                            reaper.terminate(pid)
                self.assertIn("partial output", transcript.read_text())
                self.assertEqual(reaper.groups, set())
                self.assertIsInstance(namespace["observed_timeout"], float)
                if close_pipes:
                    self.assertLess(namespace["observed_timeout"], namespace["launch_timeout"])
                else:
                    self.assertEqual(namespace["observed_timeout"], namespace["launch_timeout"])
                call = namespace["block_after_provision"].call_args
                self.assertIsNotNone(call)
                reason = call.args[0]
                self.assertEqual(
                    reason, f"fresh lane CLI timed out after 1 seconds; task {TASK}; "
                    f"attempt d-timeout; inspect {root}/_state/board-dispatch/{TASK}.d-timeout.log",
                )
                self.assertLess(len(reason), 600)
                self.assertNotIn("PRIVATE_PROMPT_SENTINEL", reason)
                self.assertNotIn("--prompt", reason)
                self.assertEqual(call.kwargs["failure_class"], "cli_timeout")
                namespace["recover_validated_outputs_for_transport_failure"].assert_called_once_with()
                namespace["write_board_transcript"].assert_called_once_with("", "")


if __name__ == "__main__":
    unittest.main()
