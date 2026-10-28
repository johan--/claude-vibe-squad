#!/usr/bin/env python3
"""Resume canary — Phase 1 acceptance gate.

Proves that a confirmed decision, an open task, a superseding decision, and the exact
recent operator request all survive a simulated /compact + restart, recovered ONLY from
the append-only journals + snapshot (no history replay).

`CapsuleFreshnessCanary` guards the second failure mode, which is the one that actually
occurred: the capsule did not crash, it silently went stale for ten days while
`render_capsule` kept succeeding. A test that only asserts "renders without error"
passes throughout that. These assert the capsule's task set against the live registry's
live set, so divergence fails loudly.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
for entry in (str(ROOT), str(PYTHON_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from chrono_state import registry, decisions, compaction, resume  # noqa: E402
from scripts.python.tests.ci_host_independence import (  # noqa: E402
    skip_in_host_independent_ci,
)

TASK_REF = re.compile(r"\[(TASK-[^\]]+)\]")
OMITTED = re.compile(r"\(\+(\d+) more live")

# The live board writes a dict keyed by task id. EXPECTED_LIVE is written out by hand
# rather than derived from the loader — a canary that computes its expectation with the
# code under test can only prove the code agrees with itself.
LIVE_FIXTURE = {
    "TASK-2026-08-05-0001-alpha": {"status": "in-flight", "specialist": "planner"},
    "TASK-2026-08-05-0002-bravo": {"status": "review-required", "specialist": "skeptic"},
    "TASK-2026-08-05-0003-charlie": {"status": "queued", "specialist": "triage"},
    "TASK-2026-08-05-0004-delta": {"status": "complete", "specialist": "planner"},
    "TASK-2026-08-05-0005-echo": {"status": "closed", "specialist": "planner"},
    "TASK-2026-08-05-0006-foxtrot": {"status": "blocked", "specialist": "planner"},
    "TASK-2026-08-05-0007-golf": {"status": "superseded", "specialist": "planner"},
}
EXPECTED_LIVE = {
    "TASK-2026-08-05-0001-alpha",
    "TASK-2026-08-05-0002-bravo",
    "TASK-2026-08-05-0003-charlie",
}
# Deferred work is itemised in its own capsule section (0230 review: a count
# line is not a task line), so the fixture's blocked task must appear too.
EXPECTED_DEFERRED = {"TASK-2026-08-05-0006-foxtrot"}
DEFERRED_OMITTED = re.compile(r"\(\+(\d+) more deferred")
PENDING_OMITTED = re.compile(r"\((\d+) group\(s\) omitted for the token bound")


class TestResumeCanary(unittest.TestCase):
    def test_decision_task_supersession_survive_compact(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            decisions.DECISIONS_FILE = base / "decisions.jsonl"
            compaction.SNAP_DIR = base / "snap"
            registry.TASKS_DIR = base / "tasks"
            registry.LIVE_REGISTRY = base / "active-tasks.json"
            # Isolation (fix round 2): without this, pending_completions() reads
            # this HOST's real _state/chrono-queue.md — a path that doesn't exist
            # under `base` returns [], matching this test's fixture reality.
            resume.QUEUE_PATH = base / "chrono-queue.md"
            (base / "tasks").mkdir()
            (base / "tasks" / "active.json").write_text(
                '[{"id": "TASK-9", "state": "blocked", "next_action": "retry phase 2"}]'
            )
            # the open task now comes from the registry the board actually feeds
            (base / "active-tasks.json").write_text(
                json.dumps({"TASK-9": {"status": "in-flight", "specialist": "planner"}})
            )

            # confirm a decision, then supersede it (mirrors this very session)
            d1 = decisions.record("cap is 2", "confirmed", ["turn-1"], home="harness")
            decisions.record(
                "no fixed cap; dynamic gate", "confirmed", ["turn-9"], supersedes=[d1],
                home="harness",
            )

            # externalize load-bearing state (what compact-now does before /compact)
            compaction.snapshot(
                "sess-1",
                {"next_action": "resume phase 2", "latest_turn": "write the plan"},
            )

            # --- simulate compact + restart: recover ONLY from files ---
            recovered = compaction.recover("sess-1")
            active = [x["statement"] for x in decisions.active_decisions()]
            live_tasks = [t["id"] for t in registry.load_live_active()]

            self.assertEqual(recovered["next_action"], "resume phase 2")
            self.assertIn("no fixed cap; dynamic gate", active)
            self.assertNotIn("cap is 2", active)
            self.assertIn("TASK-9", live_tasks)

            # the regenerated capsule reflects all of it, source-tagged
            cap = resume.render_capsule("sess-1")
            self.assertIn("no fixed cap; dynamic gate", cap)
            self.assertIn("TASK-9", cap)
            # The snapshot's latest_turn is compaction's to keep; the capsule
            # never echoes it (the `## Latest operator instruction` section was
            # deleted under audit Rec 3 and the capsule takes no turn at all).
            self.assertNotIn("Latest operator instruction", cap)
            self.assertNotIn("write the plan", cap)


class CapsuleFreshnessCanary(unittest.TestCase):
    """The capsule on disk must equal the live registry's live set, always."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.capsule = self.base / "chrono" / "resume.md"

        # Rebind module state and restore it — these are process-wide globals and a
        # leak here makes unrelated tests order-dependent.
        for module, attr, value in (
            (registry, "LIVE_REGISTRY", self.base / "active-tasks.json"),
            (registry, "TASKS_DIR", self.base / "tasks"),
            (decisions, "DECISIONS_FILE", self.base / "decisions.jsonl"),
            (resume, "CAPSULE_PATH", self.capsule),
            # Isolation (fix round 2): the queue path defaults to a nonexistent
            # file under `self.base`, so every test in this class gets pending=[]
            # (i.e. no dependency on this HOST's real chrono-queue.md) unless it
            # explicitly opts in via write_queue().
            (resume, "QUEUE_PATH", self.base / "chrono-queue.md"),
            # Same trap, same fix: the archived-charter debt block would
            # otherwise read this HOST's real thread-charters/complete and
            # spend real tokens inside a fixture capsule's bound.
            (resume, "ARCHIVED_DEBT_ROOT", self.base),
            # NEEDS HUMAN lines read departments/*/outbox; point that at the
            # fixture too, so no test depends on this HOST's real outboxes.
            (resume, "DEPARTMENTS_ROOT", self.base / "departments"),
        ):
            self.addCleanup(setattr, module, attr, getattr(module, attr))
            setattr(module, attr, value)

    def write_registry(self, payload):
        registry.LIVE_REGISTRY.write_text(json.dumps(payload))

    def write_response(self, namespace, task_id, text):
        path = resume.DEPARTMENTS_ROOT / namespace / "outbox" / f"{task_id}-response.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def write_queue(self, lines):
        resume.QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        resume.QUEUE_PATH.write_text("\n".join(lines) + "\n")

    def capsule_task_ids(self):
        return set(TASK_REF.findall(self.capsule.read_text()))

    def section_ids(self, heading):
        """Task IDs inside one `## `-delimited capsule section."""
        text = self.capsule.read_text()
        if heading not in text:
            return set()
        body = text.split(heading, 1)[1].split("\n## ", 1)[0]
        return set(TASK_REF.findall(body))

    def test_written_capsule_task_set_matches_the_live_registry(self):
        self.write_registry(LIVE_FIXTURE)
        resume.write_capsule("sess-1")
        # read back from disk: this asserts the WRITER, not just the renderer
        self.assertEqual(self.section_ids(resume.TASKS_HEADING), EXPECTED_LIVE)
        self.assertEqual(
            self.section_ids(resume.DEFERRED_HEADING), EXPECTED_DEFERRED
        )
        self.assertEqual(
            self.capsule_task_ids(), EXPECTED_LIVE | EXPECTED_DEFERRED
        )

    def test_regeneration_evicts_a_task_the_board_has_closed(self):
        """The exact 2026-07-24 failure: a closed task frozen into the capsule."""
        stale = "TASK-2026-07-18-0965-arcadia-resweep"
        self.capsule.parent.mkdir(parents=True)
        self.capsule.write_text(
            f"# Chrono resume capsule\n\n{resume.TASKS_HEADING}\n"
            f"- in-flight: await completion / verify [{stale}]\n"
        )
        self.write_registry(LIVE_FIXTURE)

        resume.write_capsule("sess-1")

        self.assertNotIn(stale, self.capsule.read_text())
        self.assertEqual(
            self.capsule_task_ids(), EXPECTED_LIVE | EXPECTED_DEFERRED
        )

    def test_capsule_ignores_the_frozen_bounded_registry(self):
        """Break 2: the bounded active.json is not the board's source of truth."""
        registry.TASKS_DIR.mkdir()
        (registry.TASKS_DIR / "active.json").write_text(
            json.dumps([{"id": "TASK-BOUNDED-STALE", "state": "in-flight"}])
        )
        self.write_registry(LIVE_FIXTURE)

        resume.write_capsule("sess-1")

        self.assertNotIn("TASK-BOUNDED-STALE", self.capsule.read_text())
        self.assertEqual(
            self.capsule_task_ids(), EXPECTED_LIVE | EXPECTED_DEFERRED
        )

    def test_list_shaped_registry_is_understood(self):
        """The file has used both shapes; the list form must not read as empty."""
        self.write_registry(
            [{"id": tid, **rec} for tid, rec in LIVE_FIXTURE.items()]
        )
        resume.write_capsule("sess-1")
        self.assertEqual(
            self.capsule_task_ids(), EXPECTED_LIVE | EXPECTED_DEFERRED
        )

    def test_terminal_records_never_reach_the_capsule(self):
        self.write_registry(LIVE_FIXTURE)
        resume.write_capsule("sess-1")
        text = self.capsule.read_text()
        for excluded in set(LIVE_FIXTURE) - EXPECTED_LIVE - EXPECTED_DEFERRED:
            self.assertNotIn(excluded, text)

    def test_bound_is_enforced_and_every_omission_is_declared(self):
        """Under the token bound the capsule may drop tasks, but never silently."""
        many = {
            f"TASK-2026-08-05-{i:04d}-bulk": {"status": "in-flight"}
            for i in range(40)
        }
        self.write_registry(many)
        resume.write_capsule("sess-1", max_tokens=60)

        text = self.capsule.read_text()
        shown = self.capsule_task_ids()
        declared = OMITTED.search(text)

        self.assertLessEqual(len(text) // 4, 60)
        self.assertTrue(shown < set(many), "expected the bound to drop something")
        self.assertIsNotNone(declared, "tasks were dropped without declaring it")
        self.assertEqual(len(shown) + int(declared.group(1)), len(many))

    # Removed: test_operator_instruction_survives_a_registry_refresh. The
    # `## Latest operator instruction` section it guarded was deleted (audit
    # Rec 3), so there is no operator line in the capsule to preserve across a
    # refresh, and write_capsule no longer takes one.

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        self.write_registry(LIVE_FIXTURE)
        resume.write_capsule("sess-1")
        siblings = [p.name for p in self.capsule.parent.iterdir()]
        self.assertEqual(siblings, ["resume.md"])

    def test_an_unknown_status_is_reported_rather_than_dropped(self):
        """A new board status must be loud, not silently absent from the capsule."""
        self.write_registry({"TASK-X": {"status": "quarantined"}})
        self.assertEqual(registry.unclassified_statuses(), {"quarantined"})

    def test_reconciler_writeable_statuses_are_all_classified(self):
        """Every status registry_reconciler.py can write must be in the partition.

        The four below were reproduced as silently vanishing by the 0210 review:
        needs_human (SETTLEABLE_STATUSES), needs_rework (reopen target), timed_out
        (terminal delivery outcome), work-done-no-envelope (SETTLED_WITHOUT_ENVELOPE).
        """
        for status in (
            "needs_human",
            "needs_rework",
            "timed_out",
            "work-done-no-envelope",
        ):
            with self.subTest(status=status):
                self.write_registry({"TASK-R": {"status": status}})
                self.assertEqual(registry.unclassified_statuses(), set())

    def test_deferred_owed_work_is_itemised_with_ids_and_next_actions(self):
        """The 0230 review's second defect: a count line is not a task line.

        A resuming Chrono must be able to act from the capsule alone, so every
        deferred task appears in its own section with its ID and next action —
        through the production writer. Expectations are written out by hand,
        not derived from the code under test.
        """
        needs_human = "TASK-2026-08-05-0301-hotel"
        expected = {
            "TASK-2026-08-05-0302-india": ("needs_rework", "rework and redispatch"),
            "TASK-2026-08-05-0303-juliet": ("needs_rework", "rework and redispatch"),
            "TASK-2026-08-05-0304-kilo": ("timed_out", "investigate timeout"),
            "TASK-2026-08-05-0305-lima": (
                "work-done-no-envelope",
                "verify work, reconcile envelope",
            ),
            "TASK-2026-08-05-0306-mike": ("blocked", "unblock or rework"),
            "TASK-2026-08-05-0307-nova": ("needs_review", "settle review"),
        }
        self.write_registry(
            {
                **{tid: {"status": status} for tid, (status, _) in expected.items()},
                needs_human: {"status": "needs_human"},
            }
        )
        resume.write_capsule("sess-1")
        text = self.capsule.read_text()
        self.assertIn(resume.DEFERRED_HEADING, text)
        section = text.split(resume.DEFERRED_HEADING, 1)[1].split("\n## ", 1)[0]
        for tid, (status, action) in expected.items():
            with self.subTest(task=tid):
                line = next(
                    (row for row in section.splitlines() if f"[{tid}]" in row), None
                )
                self.assertIsNotNone(line, f"{tid} is not itemised")
                self.assertIn(status, line)
                self.assertIn(action, line)
        attention = text.split(resume.THREAD_HEADING, 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"[{needs_human}]", attention)
        self.assertIn("NEEDS HUMAN", attention)
        self.assertNotIn(f"[{needs_human}]", section)

    def test_needs_human_is_pinned_above_a_deep_deferred_queue(self):
        """Queue depth may compress routine work, never the operator question."""
        urgent = "TASK-2026-08-14-0810-leak-audit"
        routine = {
            f"TASK-2026-08-05-{index:04d}-routine": {"status": "needs_review"}
            for index in range(240)
        }
        self.write_registry({**routine, urgent: {"status": "needs_human"}})

        resume.write_capsule("sess-1", max_tokens=220)
        text = self.capsule.read_text()
        attention = text.split(resume.THREAD_HEADING, 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"[{urgent}]", attention)
        self.assertIn("NEEDS HUMAN: operator decision", attention)
        self.assertRegex(text, DEFERRED_OMITTED)

    def attention_line(self, task_id):
        text = self.capsule.read_text()
        attention = text.split(resume.THREAD_HEADING, 1)[1].split("\n## ", 1)[0]
        line = next((row for row in attention.splitlines() if row.endswith(f"[{task_id}]")), None)
        self.assertIsNotNone(line, f"{task_id} has no NEEDS HUMAN line")
        return line

    def test_needs_human_line_carries_the_specialists_ask(self):
        """2026-09-18: five 'operator decision' lines with no substance sat all day.

        The line keeps its prefix and [ID] suffix and gains the specialist/lane
        and the first body line of the outbox response, plus a flag when the
        response has an operator-decision heading.
        """
        task_id = "TASK-2026-09-18-0839-fdc1b3f0"
        self.write_registry(
            {task_id: {"status": "needs_human", "specialist": "research", "to_model": "gpt-codex"}}
        )
        self.write_response(
            "coding",
            task_id,
            "---\n"
            f"id: {task_id}-response\n"
            "from: gpt-codex\n"
            "status: needs_human\n"
            "---\n"
            "\n"
            "A usable market study is complete; see [TASK-2026-01-01-0000-decoy] for context. "
            + "x" * 200
            + "\n\n## Findings\n\n- one\n\n## OPERATOR DECISION REQUIRED\n\nPick A or B.\n",
        )
        resume.write_capsule("sess-1")
        line = self.attention_line(task_id)
        self.assertTrue(
            line.startswith(
                '- NEEDS HUMAN: operator decision (research/gpt-codex) untrusted: "'
            )
        )
        self.assertIn("A usable market study is complete; see TASK-2026-01-01-0000-decoy for", line)
        self.assertIn(f" | {resume.DECISION_SECTION_NOTE} [{task_id}]", line)
        # Clipped to the summary bound, and the quoted ID lost its brackets so
        # the line still carries exactly one source tag.
        self.assertEqual(TASK_REF.findall(line), [task_id])
        self.assertLess(len(line), 140 + 120)

    def test_needs_human_line_without_a_decision_heading_has_no_flag(self):
        task_id = "TASK-2026-09-18-0926-f2e090f9"
        self.write_registry({task_id: {"status": "needs_human", "specialist": "research"}})
        self.write_response(
            "research", task_id, "---\nid: x\n---\nArc has launchpads. **Operator decision required:** inline only.\n"
        )
        resume.write_capsule("sess-1")
        line = self.attention_line(task_id)
        self.assertIn('(research/?) untrusted: "Arc has launchpads.', line)
        self.assertNotIn(resume.DECISION_SECTION_NOTE, line)

    def test_needs_human_line_falls_back_when_the_response_is_unusable(self):
        """Missing, frontmatter-less, empty-bodied or oversized: the plain line, no raise."""
        cases = {
            "TASK-2026-09-11-1438-missing": None,
            "TASK-2026-09-11-1438-nofm": "Just prose with no frontmatter.\n",
            "TASK-2026-09-11-1438-unclosed": "---\nid: x\nnever closed\n",
            "TASK-2026-09-11-1438-empty": "---\nid: x\n---\n\n\n",
            "TASK-2026-09-11-1438-brackets": "---\nid: x\n---\n[]\n\nreal line\n",
            "TASK-2026-09-11-1438-huge": "---\nid: x\n---\nbig\n" + "y" * (resume.NEEDS_HUMAN_RESPONSE_MAX_BYTES + 1),
        }
        self.write_registry(
            {tid: {"status": "needs_human", "specialist": "devops-engineer"} for tid in cases}
        )
        for tid, body in cases.items():
            if body is not None:
                self.write_response("coding", tid, body)
        resume.write_capsule("sess-1")
        for tid in cases:
            with self.subTest(task=tid):
                self.assertEqual(
                    self.attention_line(tid), f"- NEEDS HUMAN: operator decision [{tid}]"
                )

    def test_needs_human_id_with_path_separators_never_reaches_the_filesystem(self):
        """The ID builds a path, so it is sanitised before it does.

        A real outbox exists and every decoy sits exactly where the raw ID
        would resolve inside it, so removing the sanitiser reads LEAKED.
        """
        outbox = resume.DEPARTMENTS_ROOT / "coding" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        bad_ids = ("../../secret", "coding/outbox/../../../secret", "TASK-1;rm", "TASK-1 x", "")
        for bad in bad_ids:
            # Built along the unresolved path so every intermediate directory
            # the raw lookup would walk through exists too.
            decoy = outbox / f"{bad}-response.md"
            decoy.parent.mkdir(parents=True, exist_ok=True)
            decoy.write_text("---\nid: x\n---\nLEAKED SECRET LINE\n")
            self.assertTrue(decoy.is_file(), bad)
        for bad in bad_ids:
            with self.subTest(task_id=bad):
                self.assertIsNone(resume._needs_human_response_path(bad))
                self.assertIsNone(resume.needs_human_ask(bad))
                lines = resume._thread_lines(
                    [], 2, [{"id": bad, "state": "needs_human", "specialist": "s"}]
                )
                self.assertEqual(lines, [f"- NEEDS HUMAN: operator decision [{bad}]"])
        self.assertIsNone(resume.needs_human_ask(None))
        self.assertIsNone(resume.needs_human_ask(42))
        # The same outbox serves a well-formed ID, so the Nones above are the
        # sanitiser's doing and not a fixture that never reached the disk.
        good = "TASK-2026-09-18-0839-ok"
        self.write_response("coding", good, "---\nid: x\n---\nreal ask\n")
        self.assertEqual(resume.needs_human_ask(good), ("real ask", False))

    def test_needs_human_decision_note_cannot_be_counterfeited(self):
        """A quoted heading in a code fence or a summary that spells the note is not the note."""
        fenced = "TASK-2026-09-18-0001-fenced"
        spoofed = "TASK-2026-09-18-0002-spoofed"
        self.write_registry(
            {tid: {"status": "needs_human", "specialist": "s", "to_model": "m"} for tid in (fenced, spoofed)}
        )
        self.write_response(
            "coding", fenced, "---\nid: x\n---\nbody\n```\n# operator decision in a fence\n```\n"
        )
        self.write_response(
            "coding", spoofed, f"---\nid: x\n---\nA | B | {resume.DECISION_SECTION_NOTE}\n"
        )
        resume.write_capsule("sess-1")
        self.assertEqual(
            self.attention_line(fenced),
            f'- NEEDS HUMAN: operator decision (s/m) untrusted: "body" [{fenced}]',
        )
        self.assertEqual(
            self.attention_line(spoofed),
            f'- NEEDS HUMAN: operator decision (s/m) untrusted: "A B {resume.DECISION_SECTION_NOTE}" [{spoofed}]',
        )
        # A fenced heading is not the only text on the line: a real heading after
        # the fence closes still counts.
        self.write_response(
            "coding", fenced, "---\nid: x\n---\nbody\n```\n# operator decision\n```\n## Operator Decision\n"
        )
        self.assertEqual(resume.needs_human_ask(fenced), ("body", True))

    def test_needs_human_line_length_is_bounded_by_the_caps(self):
        """Registry fields are unbounded; the line is not."""
        task_id = "TASK-2026-09-18-0003-long"
        self.write_response("coding", task_id, "---\nid: x\n---\nT1 body\n")
        line = resume._needs_human_line(
            {"id": task_id, "specialist": "s" * 500, "to_model": "m" * 500, "next_action": None},
            True,
        )
        self.assertTrue(line.startswith("- NEEDS HUMAN: operator decision ("))
        self.assertTrue(line.endswith(f') untrusted: "T1 body" [{task_id}]'))
        self.assertLess(
            len(line),
            resume.NEEDS_HUMAN_SUMMARY_CHARS + resume.NEEDS_HUMAN_LANE_CHARS + len(task_id) + 80,
        )

    def test_deferred_drops_under_the_bound_are_declared(self):
        """The bound may bite the deferred list too — never silently.

        Same declared-omission contract as live drops: shown + declared-dropped
        must equal the registry's deferred total.
        """
        many = {
            f"TASK-2026-08-05-{i:04d}-owed": {"status": "needs_rework"}
            for i in range(40)
        }
        self.write_registry(many)
        resume.write_capsule("sess-1", max_tokens=100)

        text = self.capsule.read_text()
        shown = self.capsule_task_ids()
        declared = DEFERRED_OMITTED.search(text)

        self.assertLessEqual(len(text) // 4, 100)
        self.assertTrue(shown < set(many), "expected the bound to drop something")
        self.assertIsNotNone(
            declared, "deferred tasks were dropped without declaring it"
        )
        self.assertEqual(len(shown) + int(declared.group(1)), len(many))

    def _write_archived_charter(self, name, body):
        d = self.base / "_state" / "chrono" / "thread-charters" / "complete"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(body)

    def test_archived_debt_is_shown_and_never_silently_dropped(self):
        """A charter archived while still owed must survive the token bound.

        The block exists because archiving is a bare `mv` and every other
        reader stops at active/. If the bound could delete it without a word,
        the capsule would be back to the state that lost six queued items —
        an absent heading reading as "nothing is owed".
        """
        self.write_registry({})
        for i in range(6):
            self._write_archived_charter(
                f"owed-{i}.md",
                "## THE ASK\nship it\n\n## OPEN LOOPS\n"
                f"- 2026-08-01T00:00:00Z | QUEUE Q-{i:03d} | thing — why: x; resume: y\n"
                "\n## DONE-WHEN\n- [ ] shipped\n",
            )

        # Roomy budget: the block renders in full.
        resume.write_capsule("sess-1", max_tokens=3000)
        full = self.capsule.read_text()
        self.assertIn(resume.ARCHIVED_DEBT_HEADING, full)
        self.assertIn("owed-0.md", full)

        # Squeezed: it collapses to a declared count, and still never vanishes.
        # NOT asserted: that the capsule then fits an arbitrary bound. Measured
        # 2026-08-23 — with any debt present the floor is ~87 tokens (heading +
        # declared count + the sections that never drop), so a 60-token budget
        # is unreachable by construction. The contract is "collapses and says
        # so", not "fits any number a caller invents".
        resume.write_capsule("sess-1", max_tokens=60)
        tight = self.capsule.read_text()
        self.assertLess(
            len(tight), len(full), "the squeezed capsule must actually be smaller"
        )
        self.assertIn(
            resume.ARCHIVED_DEBT_HEADING,
            tight,
            "archived-debt heading vanished silently under the bound",
        )
        declared = re.search(
            r"\((\d+) archived charter\(s\) still owed, omitted", tight
        )
        self.assertIsNotNone(
            declared, "archived debt was dropped without declaring it"
        )
        self.assertEqual(int(declared.group(1)), 6)

    def test_archived_debt_collapsed_count_is_the_true_owed_count(self):
        """The collapsed line must report len(owed), not the capped row count.

        Rows cap at 8 (+1 "… and more"), so `len(debt_rows)` can undercount
        owed charters. The one-line form carries the true number so a
        resuming Chrono sees the real debt.
        """
        self.write_registry({})
        for i in range(22):
            self._write_archived_charter(
                f"owed-{i:02d}.md",
                "## THE ASK\nship it\n\n## OPEN LOOPS\n"
                f"- 2026-08-01T00:00:00Z | QUEUE Q-{i:03d} | thing — why: x; resume: y\n"
                "\n## DONE-WHEN\n- [ ] shipped\n",
            )
        # Tight budget forces the block to collapse to its declared count.
        resume.write_capsule("sess-1", max_tokens=60)
        tight = self.capsule.read_text()
        declared = re.search(
            r"\((\d+) archived charter\(s\) still owed, omitted", tight
        )
        self.assertIsNotNone(declared, "archived debt collapsed without a count")
        self.assertEqual(int(declared.group(1)), 22)

    def test_every_populated_section_fits_the_default_token_bound(self):
        """Rec 3.5: with every droppable section populated, nothing overflows.

        Exercises active decisions (recent), live, deferred, pending
        completions, and archived debt together at the real 3000-token bound,
        and asserts each heading renders — the budget win must not come from a
        section silently vanishing.
        """
        self.write_registry(
            {
                "TASK-2026-10-20-0001-live": {
                    "status": "in-flight",
                    "specialist": "planner",
                },
                "TASK-2026-10-20-0002-owed": {"status": "needs_rework"},
            }
        )
        self.write_queue(
            [
                "2026-10-20T00:00:00Z | needs_review | coding/TASK-3 | review this",
                "2026-10-20T00:00:00Z | BLOCKED | security/TASK-4 | stuck",
            ]
        )
        decisions.record(
            "keep the dynamic gate", "confirmed", ["turn-1"], home="harness"
        )
        self._write_archived_charter(
            "owed.md",
            "## THE ASK\nship it\n\n## OPEN LOOPS\n"
            "- 2026-08-01T00:00:00Z | QUEUE Q-001 | thing — why: x; resume: y\n"
            "\n## DONE-WHEN\n- [ ] shipped\n",
        )
        resume.write_capsule("sess-1", max_tokens=3000)
        text = self.capsule.read_text()
        for heading in (
            "## Active decisions",
            resume.TASKS_HEADING,
            resume.DEFERRED_HEADING,
            resume.QUEUE_HEADING,
            resume.ARCHIVED_DEBT_HEADING,
        ):
            self.assertIn(heading, text, f"section missing: {heading}")
        self.assertIn("keep the dynamic gate", text)
        self.assertLessEqual(len(text) // 4, 3000)

    def test_handled_pending_statuses_are_not_counted_as_owed(self):
        """Settled queue receipts must not render as "awaiting a decision".

        The append-only queue keeps every AUTO-CLOSED / TASK-CLOSED / cancelled
        / complete receipt; counting them would present handled work as
        outstanding debt.
        """
        self.write_registry({})
        self.write_queue(
            [
                "2026-10-20T00:00:00Z | needs_review | coding/TASK-1 | real",
                "2026-10-20T00:00:00Z | AUTO-CLOSED | coding/TASK-2 | done",
                "2026-10-20T00:00:00Z | TASK-CLOSED | coding/TASK-3 | done",
                "2026-10-20T00:00:00Z | cancelled | coding/TASK-4 | gone",
                "2026-10-20T00:00:00Z | complete | coding/TASK-5 | done",
                "2026-10-20T00:00:00Z | completed | coding/TASK-6 | done",
            ]
        )
        resume.write_capsule("sess-1", max_tokens=3000)
        text = self.capsule.read_text()
        self.assertIn("1 x coding | needs_review", text)
        for handled in ("AUTO-CLOSED", "TASK-CLOSED", "cancelled", "| complete", "completed"):
            self.assertNotIn(handled, text)

    def test_active_decisions_older_than_30_days_collapse_to_a_count(self):
        """Rec 3.1: the capsule renders only harness decisions within 30 days.

        Older confirmed decisions still live in the journal; the capsule names
        how many there are and points at it instead of re-stating months-old
        authority every session, preserving the capsule budget for current
        work without losing access to older decisions.
        """
        decisions.DECISIONS_FILE.write_text(
            json.dumps(
                {
                    "decision_id": "DEC-recent",
                    "status": "confirmed",
                    "home": "harness",
                    "statement": "recent policy",
                    "scope": [],
                    "supersedes": [],
                    "source_turn_ids": ["t"],
                    "timestamp": "2026-10-20T00:00:00Z",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "decision_id": "DEC-stale",
                    "status": "confirmed",
                    "home": "harness",
                    "statement": "stale policy",
                    "scope": [],
                    "supersedes": [],
                    "source_turn_ids": ["t"],
                    "timestamp": "2026-07-01T00:00:00Z",
                }
            )
            + "\n"
        )
        self.write_registry({})
        resume.write_capsule(
            "sess-1", "regenerate", now=datetime(2026, 10, 27, tzinfo=timezone.utc)
        )
        text = self.capsule.read_text()
        self.assertIn("- recent policy [DEC-recent]", text)
        self.assertNotIn("stale policy", text)
        self.assertNotIn("[DEC-stale]", text)
        self.assertIn("1 harness decision(s) older than 30 days", text)

    def test_archived_debt_block_is_silent_when_nothing_is_owed(self):
        """Probation clause: no debt, no block. A check that always fires is noise."""
        self.write_registry({})
        self._write_archived_charter(
            "settled.md",
            "## THE ASK\nship it\n\n## OPEN LOOPS\n"
            "- 2026-08-01T00:00:00Z | FOLD | thing — why: x; resume: y\n"
            "\n## DONE-WHEN\n- [x] shipped\n",
        )
        resume.write_capsule("sess-1", max_tokens=3000)
        self.assertNotIn(resume.ARCHIVED_DEBT_HEADING, self.capsule.read_text())

    def test_pending_drops_under_the_bound_are_declared(self):
        """The bound may bite the pending-completions section too — never silently.

        Fix round 2 controller ruling: unlike the pre-fix behaviour (a silent
        drop indistinguishable from an empty queue), a collapsed pending
        section must always declare what it dropped — the same contract live
        and deferred already keep.
        """
        self.write_registry({})
        self.write_queue(
            [
                f"2026-08-16T00:{i:02d}:00Z | needs_review | ns{i}/TASK-{i} | "
                + ("x" * 40)
                for i in range(40)
            ]
        )
        resume.write_capsule("sess-1", max_tokens=150)

        text = self.capsule.read_text()
        self.assertLessEqual(len(text) // 4, 150)
        self.assertIn(
            resume.QUEUE_HEADING, text, "pending heading must not vanish silently"
        )
        declared = PENDING_OMITTED.search(text)
        self.assertIsNotNone(
            declared, "pending groups were dropped without declaring it"
        )
        self.assertEqual(int(declared.group(1)), 40)

    def test_pending_renders_in_full_when_it_fits(self):
        """The positive control for the round-1 fix: pending is not dead code."""
        self.write_registry({})
        self.write_queue(
            [
                "2026-08-16T00:00:00Z | needs_review | coding/TASK-1 | a",
                "2026-08-16T00:01:00Z | needs_review | coding/TASK-2 | b",
            ]
        )
        resume.write_capsule("sess-1")  # real 3000-token budget

        text = self.capsule.read_text()
        self.assertIn(resume.QUEUE_HEADING, text)
        self.assertIn("- 2 x coding | needs_review", text)
        self.assertIsNone(
            PENDING_OMITTED.search(text), "nothing should be declared omitted"
        )

    def test_an_unknown_status_is_loud_through_the_production_writer(self):
        """The 0210 review's positive control: write_capsule itself must be loud.

        The detector passing in a test while production never calls it is the
        original ten-day failure shape — a clean-looking file, silent stderr,
        owed state gone.
        """
        self.write_registry({"TASK-X": {"status": "quarantined"}})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            resume.write_capsule("sess-1")
        text = self.capsule.read_text()
        self.assertIn("UNCLASSIFIED", text)
        self.assertIn("quarantined", text)
        self.assertIn("quarantined", stderr.getvalue())

    def test_replace_failure_leaves_no_temp_and_preserves_the_old_capsule(self):
        """The 0210 review's fault injection: a failed rename must clean its temp."""
        self.write_registry(LIVE_FIXTURE)
        resume.write_capsule("sess-1")
        before = self.capsule.read_text()
        with mock.patch.object(
            resume.os, "replace", side_effect=OSError("injected replace failure")
        ):
            with self.assertRaises(OSError):
                resume.write_capsule("sess-1")
        self.assertEqual(self.capsule.read_text(), before)
        self.assertEqual(
            [p.name for p in self.capsule.parent.iterdir()], ["resume.md"]
        )

    def test_cleanup_failure_does_not_mask_the_original_write_error(self):
        """The 0230 review's third defect: an unguarded `tmp.close()` in the
        cleanup path replaced the primary exception and skipped the unlink.

        Both the write and the close fail here; the caller must still see the
        ORIGINAL write error, the temp must still be unlinked, and the old
        capsule must survive.
        """
        self.write_registry(LIVE_FIXTURE)
        resume.write_capsule("sess-1")
        before = self.capsule.read_text()

        class FaultyTemp:
            def __init__(self, real):
                self._real = real
                self.name = real.name

            def write(self, data):
                raise OSError("injected write failure")

            def close(self):
                raise OSError("injected close failure")

            def __getattr__(self, attr):
                return getattr(self._real, attr)

        real_factory = tempfile.NamedTemporaryFile
        with mock.patch.object(
            resume.tempfile,
            "NamedTemporaryFile",
            lambda *args, **kwargs: FaultyTemp(real_factory(*args, **kwargs)),
        ):
            with self.assertRaises(OSError) as caught:
                resume.write_capsule("sess-1")
        self.assertEqual(str(caught.exception), "injected write failure")
        self.assertEqual(self.capsule.read_text(), before)
        self.assertEqual(
            [p.name for p in self.capsule.parent.iterdir()], ["resume.md"]
        )

    def test_missing_registry_yields_an_empty_capsule_not_a_crash(self):
        resume.write_capsule("sess-1")
        self.assertEqual(self.capsule_task_ids(), set())
        self.assertIn("(none)", self.capsule.read_text())


class LiveRegistryClassificationCanary(unittest.TestCase):
    """Runs against the real registry when one is present on this host."""

    @skip_in_host_independent_ci("reads the host's live _state/active-tasks.json")
    def test_every_status_in_the_real_registry_is_classified(self):
        live = Path(registry.LIVE_REGISTRY)
        if not live.exists():
            self.skipTest(f"no live registry at {live}")
        unknown = registry.unclassified_statuses(live)
        self.assertEqual(
            unknown, set(), f"unclassified board statuses would vanish silently: {unknown}"
        )


if __name__ == "__main__":
    unittest.main()
