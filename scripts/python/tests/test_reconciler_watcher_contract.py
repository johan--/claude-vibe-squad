#!/usr/bin/env python3
"""The reconciler's stdout is an API, and bin/outbox-watcher.sh is its consumer.

outbox-watcher.sh decides whether the reconciler acknowledged a response by
grepping the targeted reconcile's stdout for one of five keyword lines. Nothing
in registry_reconciler.py names that coupling, so a refactor can delete the
acknowledgement while every reconciler test stays green -- which is exactly what
the reverted commit d4859dbf3 did. At `reconciler_handled=0` the watcher
re-queues settled work to Chrono on every restart and leaves the task packet in
the live mailbox for the inbox watcher to redeliver.

These tests read the accepted forms out of the watcher itself rather than
restating them, so the two files cannot drift apart silently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import test_registry_reconciler_board_receipt as fixtures


reconciler = fixtures.reconciler

REPO = Path(__file__).resolve().parents[3]
WATCHER = REPO / "bin" / "outbox-watcher.sh"
RECONCILER_MODULE = REPO / "scripts" / "python" / "registry_reconciler.py"

TASK_ID = "TASK-2026-09-22-1300-watcher-contract"
ATTEMPT_ID = "d-" + "7" * 32

# The literal stdout the reverted hardening produced for a settled row: the
# summary line alone, carrying no task id. Used as a negative control on the
# keyword parse and as the positive control for the end-to-end harness.
REVERTED_RECONCILER_STDOUT = "registry-reconciler write: changes=1"


def watcher_handled_keywords() -> list[str]:
    """Return the grep patterns that set reconciler_handled=1, from the source.

    Walks back from the `reconciler_handled=1` assignment to the `if grep -Fq`
    that opens its condition, so the `v2-settlement-hold` grep in the following
    block is not mistaken for an acknowledgement form.
    """

    lines = WATCHER.read_text(encoding="utf-8").splitlines()
    assignment = next(
        index
        for index, line in enumerate(lines)
        if line.strip() == "reconciler_handled=1"
    )
    start = assignment
    while start > 0 and not lines[start].strip().startswith("if grep -Fq"):
        start -= 1
    pattern = re.compile(r'grep -Fq "([^"]*)" <<<"\$reconcile_output"')
    forms: list[str] = []
    for line in lines[start:assignment]:
        forms.extend(pattern.findall(line))
    return forms


class WatcherKeywordParseTests(unittest.TestCase):
    """Positive controls: the parse above can actually fail."""

    def test_parse_finds_the_five_acknowledgement_forms(self) -> None:
        self.assertEqual(
            watcher_handled_keywords(),
            [
                "reconciled ${task_id} ->",
                "already-settled ${task_id} ->",
                "review-required ${task_id} ->",
                "review-held ${task_id} ->",
                "auto-closed ${task_id} from",
            ],
        )

    def test_reverted_summary_line_matches_no_acknowledgement_form(self) -> None:
        # Without this the membership assertion below could pass on a parse that
        # silently matched everything.
        forms = [
            form.replace("${task_id}", TASK_ID)
            for form in watcher_handled_keywords()
        ]
        self.assertTrue(forms)
        self.assertFalse(
            any(form in REVERTED_RECONCILER_STDOUT for form in forms),
            REVERTED_RECONCILER_STDOUT,
        )


def _settled_entry(root: Path, state: Path) -> dict:
    """A settled-complete row carrying a quarantined terminal board receipt."""

    entry = fixtures.BoardReceiptSettlementTests._v2_entry(
        TASK_ID,
        ATTEMPT_ID,
        status="complete",
        delivery_state="terminal",
        mandatory_review="true",
        review_triggers=["blast_radius"],
        review_model="claude",
        compatibility_namespace="coding",
        review_settled_at="2026-09-22T16:43:23Z",
        review_settled_by="chrono-explicit",
        completed_at="2026-09-22T16:43:23Z",
        reconciled_at="2026-09-22T16:43:23Z",
        cross_family_review_ref="departments/coding/outbox/review.md",
        review_ref="departments/coding/outbox/review.md",
        verdict="APPROVE",
        review_force_override=False,
    )
    descriptor = fixtures.BoardReceiptSettlementTests._write_v2_descriptor(
        state, TASK_ID, ATTEMPT_ID
    )
    receipt = fixtures.BoardReceiptSettlementTests._write_v2_receipt(
        descriptor,
        completed_at="2026-09-22T15:27:35Z",
        terminal_outcome="needs_review",
        status="blocked",
        failure_class="cli_timeout",
        cli_exec_succeeded=False,
        controller_quarantine=reconciler.CONTROLLER_QUARANTINE_MARKER,
        response_status="needs_review",
    )
    entry["terminal_receipt_path"] = str(receipt.relative_to(root))
    entry["terminal_receipt_status"] = "needs_review"
    reconciler.apply_receipt_diagnostics(
        entry, reconciler.receipt_failure_diagnostics(receipt)
    )
    return entry


class SettledReconcileEmitsWatcherKeywordTests(unittest.TestCase):
    def test_targeted_settled_reconcile_emits_a_watcher_keyword(self) -> None:
        """The in-process assertion: a settled row's targeted reconcile speaks."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "_state"
            registry_path = state / "active-tasks.json"
            entry = _settled_entry(root, state)
            registry_path.write_text(json.dumps({TASK_ID: entry}), encoding="utf-8")
            with fixtures.BoardReceiptSettlementTests._patch_runtime(
                root, state, registry_path
            ):
                _, messages = reconciler.reconcile(TASK_ID, dry_run=False)
            status = json.loads(registry_path.read_text(encoding="utf-8"))[TASK_ID][
                "status"
            ]

        forms = [
            form.replace("${task_id}", TASK_ID)
            for form in watcher_handled_keywords()
        ]
        stdout = "\n".join(messages)
        self.assertTrue(
            any(form in stdout for form in forms),
            f"no watcher acknowledgement form in {messages!r}",
        )
        self.assertEqual(status, "complete")


class WatcherEndToEndTests(unittest.TestCase):
    """The real bin/outbox-watcher.sh over the real reconciler.

    Only two things are stubbed, both for the same reason the replay-requeue
    test stubs them: fswatch (no event loop wanted) and the `uv run` wrapper in
    bin/registry-reconciler.sh (the Python module underneath it is the real one,
    pointed at the temp vault through VAULT_ROOT/STATE_DIR).
    """

    def _run_watcher(self, *, reconciler_stdout: str | None = None) -> tuple[str, dict]:
        with tempfile.TemporaryDirectory(prefix="watcher-contract-") as tmp:
            outer = Path(tmp)
            vault = outer / "vault"
            bin_dir = vault / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "outbox-watcher.sh").symlink_to(WATCHER)

            state = vault / "_state"
            state.mkdir()
            department = vault / "departments" / "coding"
            for subdirectory in ("inbox", "active", "outbox", "archive"):
                (department / subdirectory).mkdir(parents=True)

            registry_path = state / "active-tasks.json"
            entry = _settled_entry(vault, state)
            registry_path.write_text(json.dumps({TASK_ID: entry}), encoding="utf-8")

            shim = bin_dir / "registry-reconciler.sh"
            if reconciler_stdout is None:
                # Real reconcile(), real stdout. This is what is under test.
                shim.write_text(
                    "#!/bin/bash\n"
                    f'export VAULT_ROOT="{vault}"\n'
                    f'export STATE_DIR="{state}"\n'
                    f'exec python3 -B "{RECONCILER_MODULE}" "$@"\n',
                    encoding="utf-8",
                )
            else:
                # Positive control: replay a known stdout so the harness is shown
                # to distinguish an acknowledged reconcile from a silent one.
                shim.write_text(
                    "#!/bin/bash\n"
                    f"printf '%s\\n' {json.dumps(reconciler_stdout)}\n"
                    "exit 0\n",
                    encoding="utf-8",
                )
            shim.chmod(0o755)

            packet = department / "active" / f"{TASK_ID}.md"
            packet.write_text(
                "---\n"
                f"id: {TASK_ID}\n"
                "to_model: claude\n"
                "specialist: backend-engineer\n"
                "---\n\npacket body\n",
                encoding="utf-8",
            )
            (department / "outbox" / f"{TASK_ID}-response.md").write_text(
                "---\n"
                f"id: {TASK_ID}-response\n"
                f"in_response_to: {TASK_ID}\n"
                "from: claude\n"
                "to: chrono\n"
                "type: RESULT\n"
                "status: complete\n"
                "---\n\nfinished work.\n",
                encoding="utf-8",
            )

            fakebin = outer / "fakebin"
            fakebin.mkdir()
            fswatch = fakebin / "fswatch"
            fswatch.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
            fswatch.chmod(0o755)

            environment = {
                **os.environ,
                "VAULT_ROOT": str(vault),
                "STATE_DIR": str(state),
                "SQUAD_SESSION": "none",
                "TMUX_BIN": "/nonexistent/tmux",
                "RESPONSE_MIN_AGE_SECONDS": "0",
                "SQUAD_TEST_ISOLATION": "1",
                "PATH": f"{fakebin}:{os.environ['PATH']}",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            environment.pop("CHRONO_VAULT_ROOT", None)
            # Output goes to a FILE, not a pipe. The watcher's best-effort
            # helpers background children that inherit stdout, so a pipe stays
            # open long after the watcher itself exits and capture_output would
            # block until the test's own timeout.
            log = outer / "watcher.log"
            with log.open("w", encoding="utf-8") as handle:
                result = subprocess.run(
                    ["bash", str(bin_dir / "outbox-watcher.sh"), "coding"],
                    env=environment,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=120,
                    check=False,
                )
            queue_path = state / "chrono-queue.md"
            observed = {
                "returncode": result.returncode,
                "queue": queue_path.read_text(encoding="utf-8")
                if queue_path.exists()
                else "",
                "packet_still_active": packet.exists(),
                "packet_archived": (
                    department / "archive" / f"{TASK_ID}.md"
                ).exists(),
                "status": json.loads(registry_path.read_text(encoding="utf-8"))[
                    TASK_ID
                ]["status"],
            }
            return log.read_text(encoding="utf-8"), observed

    def test_settled_task_is_acknowledged_archived_and_not_requeued(self) -> None:
        output, observed = self._run_watcher()

        self.assertEqual(observed["returncode"], 0, output)
        self.assertIn(
            f"shared reconciler handled registry entry: {TASK_ID}", output
        )
        self.assertNotIn(TASK_ID, observed["queue"])
        self.assertFalse(observed["packet_still_active"], output)
        self.assertTrue(observed["packet_archived"], output)
        self.assertEqual(observed["status"], "complete")

    def test_silent_reconcile_requeues_and_strands_the_packet(self) -> None:
        """Positive control, and the shape of the reverted commit's regression.

        The only variable between this and the test above is the reconciler's
        stdout. If the harness could not tell the two apart, the assertions
        above would be worth nothing.
        """

        output, observed = self._run_watcher(
            reconciler_stdout=REVERTED_RECONCILER_STDOUT
        )

        self.assertEqual(observed["returncode"], 0, output)
        self.assertIn(
            "shared reconciler found no settled registry entry", output
        )
        self.assertIn(TASK_ID, observed["queue"])
        self.assertTrue(observed["packet_still_active"], output)
        self.assertFalse(observed["packet_archived"], output)


if __name__ == "__main__":
    unittest.main()
