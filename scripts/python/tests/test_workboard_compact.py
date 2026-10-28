#!/usr/bin/env python3
"""Compaction keeps the live projection and loses no historical item.

Parsing settled history on every prompt, tool call and Stop adds work that
need not grow with the live queue. ``workboard.compact`` re-emits the open
items (original ``event_id``/``at``) plus the focus, archives every other
line, and refuses unless ``compare_item_censuses`` over archive ∪ live equals
the original and the projection is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from chrono_state import focus_gate, workboard  # noqa: E402


LEGACY_PROSE = (
    "# Open Work\n\n"
    "The single list. Everything raised and not yet done lives here and nowhere else.\n\n"
    "---\n\n"
    "## Owed from the August audits\n\n"
    "Four audits ran and produced roughly 25 findings; the rest are here.\n\n"
    "<!-- workboard-migration/v1 plan_sha256="
    + "a" * 64
    + " source_sha256="
    + "b" * 64
    + " body_sha256="
    + "c" * 64
    + " body_size=1 -->\n"
    "- [x] OLD-1 | a finished legacy row\n"
    "- [x] an anonymous completed historical row\n\n"
)


class CompactionFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="workboard-compact-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / workboard.WORKBOARD_REL
        self.ledger.parent.mkdir(parents=True)
        self.archive = self.ledger.parent / "OPEN-WORK-archive-test.md"
        self.tick = 0

    def append(self, kind: str, **facts: str) -> workboard.WorkEvent:
        self.tick += 1
        return workboard.append_event(
            kind,
            path=self.ledger,
            event_id=f"EV-{self.tick:04d}",
            at=f"2026-10-{1 + self.tick // 24:02d}T{self.tick % 24:02d}:00:00Z",
            **facts,
        )

    def history_board(self) -> dict[str, workboard.WorkEvent]:
        """Active + queued + blocked items, restates, folds, drops, successors."""
        self.ledger.write_text(LEGACY_PROSE, encoding="utf-8")
        events: dict[str, workboard.WorkEvent] = {}
        events["A"] = self.append(
            "start", alias="A", summary="first", why="w-a", next_action="do a"
        )
        events["B"] = self.append(
            "queue", alias="B", summary="second", why="w-b", resume_action="do b"
        )
        events["C"] = self.append(
            "queue", alias="C", summary="third", why="w-c", resume_action="do c"
        )
        self.append("restate", item_id="B", summary="second (revised)", why="w-b2", next_action="do b2")
        self.append("advance", item_id="A", next_action="do a2")
        self.append("fold", request_id="R-fold", target_id="A", summary="folded", why="same ask", next_action="do a3")
        self.append("complete", item_id="C")
        self.append("block", item_id="B", resume_action="await x")
        self.append("drop", request_id="R-noise", summary="a request", why="noise")
        events["D"] = self.append(
            "queue", alias="D", summary="fourth", why="w-d", resume_action="do d"
        )
        events["E"] = self.append(
            "queue", alias="E", summary="fifth", why="w-e", resume_action="do e"
        )
        self.append("drop", request_id="E", summary="withdrawn", why="obsolete")
        self.append("complete", item_id="A", successor_id="D", next_action="go d")
        self.append("restart")
        return events

    @staticmethod
    def shape(projection: workboard.WorkboardProjection) -> dict[str, object]:
        return workboard._projection_shape(projection)


class CompactionTests(CompactionFixture):
    def test_dry_run_reports_and_writes_nothing(self) -> None:
        self.history_board()
        before_bytes = self.ledger.read_bytes()
        report = workboard.compact(self.ledger, self.archive)
        self.assertEqual(report["outcome"], "dry-run")
        self.assertTrue(report["ok"])
        self.assertTrue(report["projection_preserved"])
        self.assertTrue(report["partition_ok"])
        self.assertEqual(report["open_items"], 2)
        self.assertEqual(report["openings_moved"], 2)
        self.assertEqual(report["focus"], "active:D")
        self.assertLess(report["bytes_out"], report["bytes_in"])
        self.assertEqual(self.ledger.read_bytes(), before_bytes)
        self.assertFalse(self.archive.exists())

    def test_apply_partitions_the_ledger_losslessly(self) -> None:
        events = self.history_board()
        original_text = self.ledger.read_text(encoding="utf-8")
        original_doc = workboard.parse_workboard(self.ledger)
        before = workboard.load_workboard(self.ledger, strict=True)
        mode = self.ledger.stat().st_mode & 0o777

        report = workboard.compact(self.ledger, self.archive, dry_run=False)
        self.assertEqual(report["outcome"], "applied")

        after = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(after.issues, ())
        self.assertEqual(self.shape(after), self.shape(before))
        self.assertEqual(after.active_item_id, "D")
        self.assertEqual(after.next_action, "go d")
        self.assertEqual(self.ledger.stat().st_mode & 0o777, mode)

        live_events = [r for r in after.document.records if isinstance(r, workboard.WorkEvent)]
        openings = {r.fields.get("alias"): r for r in live_events if r.kind == "queue"}
        for alias in ("B", "D"):
            with self.subTest(opening=alias):
                self.assertEqual(openings[alias].event_id, events[alias].event_id)
                self.assertEqual(openings[alias].at, events[alias].at)
                self.assertEqual(openings[alias].fields["summary"], events[alias].fields["summary"])
        restates = [r for r in live_events if r.kind == "restate"]
        self.assertEqual(
            {r.fields["summary"] for r in restates}, {"second (revised)", "fourth"}
        )
        # Synthesized events carry the item's last-change time, never the clock
        # at compaction: B last changed at its block (tick 8), D at A's
        # successor completion (tick 13).
        by_work_id = {r.fields.get("work_id"): r for r in restates}
        b_item = next(i for i in before.items if i.alias == "B")
        d_item = next(i for i in before.items if i.alias == "D")
        self.assertEqual(by_work_id[b_item.work_id].at, "2026-10-01T08:00:00Z")
        self.assertEqual(by_work_id[d_item.work_id].at, "2026-10-01T13:00:00Z")
        self.assertTrue(all(r.event_id.startswith("CMP-") for r in restates))
        self.assertEqual(live_events[-1].kind, workboard.COMPACTION_KIND)
        self.assertTrue(self.ledger.read_text(encoding="utf-8").startswith(workboard.OPEN_WORK_HEADER))
        self.assertIn(workboard.COMPACTION_MARKER_PREFIX, self.ledger.read_text(encoding="utf-8"))
        self.assertNotIn("Owed from the August audits", self.ledger.read_text(encoding="utf-8"))

        archive_text = self.archive.read_text(encoding="utf-8")
        original_lines = original_text.splitlines(keepends=True)
        moved = {
            line for line in original_lines
            if f"event_id={events['B'].event_id} " in line or f"event_id={events['D'].event_id} " in line
        }
        self.assertEqual(len(moved), 2)
        expected_archive = "".join(line for line in original_lines if line not in moved)
        self.assertTrue(archive_text.startswith(expected_archive))
        trailer = archive_text[len(expected_archive):]
        self.assertTrue(trailer.startswith("\n" + workboard.COMPACTION_MARKER_PREFIX), trailer)
        self.assertIn("Owed from the August audits", archive_text)
        self.assertIn("workboard-migration/v1", archive_text)

        # Every original line lives in exactly one place: moved openings are
        # re-emitted live (same event_id/at, asserted above; key order may
        # differ), every other line is in the archive verbatim.
        live_text = self.ledger.read_text(encoding="utf-8")
        for line in original_lines:
            with self.subTest(line=line[:60]):
                if line in moved:
                    self.assertNotIn(line, archive_text)
                    self.assertNotIn(line, live_text)
                else:
                    self.assertIn(line, archive_text)
                    if line.startswith("- workboard-event/v1"):
                        self.assertNotIn(line, live_text)

        combined = workboard._parse_workboard_text(self.ledger, archive_text + live_text)
        census = workboard.compare_item_censuses(original_doc, combined)
        self.assertTrue(census["ok"], census)
        self.assertTrue(census["state_preserved"], census)
        self.assertEqual(census["count_in"], census["count_out"])
        self.assertEqual(census["anonymous_count_in"], 1)
        self.assertEqual(census["anonymous_count_out"], 1)
        self.assertEqual(report["events_in"], report["events_out"] - report["synthesized_events"] + report["events_archived"])

        # The ledger keeps working as the one writer after compaction.
        workboard.append_event("queue", path=self.ledger, alias="F", summary="sixth", why="w-f", resume_action="do f")
        self.assertEqual(len(workboard.load_workboard(self.ledger, strict=True).items), 3)

    def test_negative_control_an_unrelated_census_is_rejected(self) -> None:
        """The census gate must be able to say no, or its yes means nothing."""
        self.history_board()
        original = workboard.parse_workboard(self.ledger)
        damaged = workboard._parse_workboard_text(
            self.ledger,
            "".join(
                line for line in self.ledger.read_text(encoding="utf-8").splitlines(keepends=True)
                if "alias=B " not in line
            ),
        )
        census = workboard.compare_item_censuses(original, damaged)
        self.assertFalse(census["ok"])
        self.assertEqual(len(census["dropped"]), 1)

    def test_focus_gate_accepts_the_compacted_ledger(self) -> None:
        self.history_board()
        before = focus_gate.decide(focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger)
        workboard.compact(self.ledger, self.archive, dry_run=False)
        write = focus_gate.decide(
            focus_gate.PRE_TOOL_USE, ledger_path=self.ledger, tool_name="Write"
        )
        self.assertTrue(write.allow)
        self.assertEqual(write.message, "")
        self.assertFalse(write.fail_open)
        prompt = focus_gate.decide(focus_gate.USER_PROMPT_SUBMIT, ledger_path=self.ledger)
        self.assertFalse(prompt.fail_open)
        self.assertIn("NEXT ACTION: go d", prompt.message)
        self.assertEqual(
            [l for l in prompt.message.splitlines() if l.startswith(("ACTIVE ITEM", "NEXT ACTION"))],
            [l for l in before.message.splitlines() if l.startswith(("ACTIVE ITEM", "NEXT ACTION"))],
        )

    def test_blocked_item_and_idle_focus_are_reproduced(self) -> None:
        self.ledger.write_text(LEGACY_PROSE, encoding="utf-8")
        self.append("start", alias="A", summary="first", why="w-a", next_action="do a")
        self.append("queue", alias="B", summary="second", why="w-b", resume_action="do b")
        self.append("block", item_id="B", resume_action="await the operator")
        self.append("complete", item_id="A", focus_state="idle")
        before = workboard.load_workboard(self.ledger, strict=True)
        self.assertTrue(before.idle)
        report = workboard.compact(self.ledger, self.archive, dry_run=False)
        self.assertEqual(report["focus"], "idle")
        after = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(self.shape(after), self.shape(before))
        self.assertTrue(after.idle)
        self.assertEqual(after.items[0].state, "blocked")

    def test_waiting_on_a_blocked_item_is_reproduced(self) -> None:
        self.ledger.write_text(LEGACY_PROSE, encoding="utf-8")
        self.append("start", alias="A", summary="first", why="w-a", next_action="do a")
        self.append("block", item_id="A", resume_action="await evidence")
        before = workboard.load_workboard(self.ledger, strict=True)
        self.assertIsNotNone(before.waiting_work_id)
        report = workboard.compact(self.ledger, self.archive, dry_run=False)
        self.assertEqual(report["focus"], "waiting:A")
        after = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(self.shape(after), self.shape(before))

    def test_waiting_on_an_unblocked_item_is_refused(self) -> None:
        self.ledger.write_text(LEGACY_PROSE, encoding="utf-8")
        self.append("start", alias="A", summary="first", why="w-a", next_action="do a")
        self.append("queue", alias="B", summary="second", why="w-b", resume_action="do b")
        self.append("complete", item_id="A", waiting_id="B", resume_action="wait for b")
        before_bytes = self.ledger.read_bytes()
        with self.assertRaisesRegex(workboard.WorkboardCompactionError, "waiting"):
            workboard.compact(self.ledger, self.archive, dry_run=False)
        self.assertEqual(self.ledger.read_bytes(), before_bytes)
        self.assertFalse(self.archive.exists())

    def test_invalid_or_legacy_ledgers_are_refused(self) -> None:
        self.append("start", alias="A", summary="first", why="w-a", next_action="do a")
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(workboard.format_event("complete", event_id="EV-RAW", at="2026-10-02T00:00:00Z", item_id="A"))
        self.assertTrue(workboard.load_workboard(self.ledger).issues)
        before_bytes = self.ledger.read_bytes()
        with self.assertRaisesRegex(workboard.WorkboardCompactionError, "repair") as caught:
            workboard.compact(self.ledger, self.archive, dry_run=False)
        self.assertIn("validation_issues", caught.exception.report)
        self.assertEqual(self.ledger.read_bytes(), before_bytes)

        legacy = self.ledger.parent / "legacy.md"
        legacy.write_text("- [ ] L-1 | legacy only; next: migrate\n", encoding="utf-8")
        with self.assertRaisesRegex(workboard.WorkboardCompactionError, "migration"):
            workboard.compact(legacy, legacy.parent / "legacy-archive.md")

    def test_archive_guards(self) -> None:
        self.history_board()
        self.archive.write_text("occupied\n", encoding="utf-8")
        with self.assertRaisesRegex(workboard.WorkboardCompactionError, "already exists"):
            workboard.compact(self.ledger, self.archive)
        elsewhere = self.root / "elsewhere.md"
        with self.assertRaisesRegex(workboard.WorkboardCompactionError, "beside the ledger"):
            workboard.compact(self.ledger, elsewhere)
        with self.assertRaisesRegex(workboard.WorkboardCompactionError, "regular file"):
            workboard.compact(self.ledger.parent / "missing.md", self.ledger.parent / "x.md")
        self.assertEqual(
            workboard.default_archive_path(self.ledger, "2026-10-27T16:00:00Z"),
            self.ledger.parent / "OPEN-WORK-archive-2026-10-27.md",
        )

    def test_expect_source_sha256_pins_the_dry_run_pre_image(self) -> None:
        self.history_board()
        report = workboard.compact(self.ledger, self.archive)
        self.append("queue", alias="LATE", summary="arrived", why="w", resume_action="later")
        before_bytes = self.ledger.read_bytes()
        with self.assertRaisesRegex(workboard.WorkboardCompactionError, "fresh dry-run"):
            workboard.compact(
                self.ledger, self.archive, dry_run=False,
                expect_source_sha256=report["source_sha256"],
            )
        self.assertEqual(self.ledger.read_bytes(), before_bytes)
        self.assertFalse(self.archive.exists())
        with self.assertRaisesRegex(workboard.WorkboardCompactionError, "64-hex"):
            workboard.compact(self.ledger, self.archive, expect_source_sha256="nope")
        fresh = workboard.compact(self.ledger, self.archive)
        applied = workboard.compact(
            self.ledger, self.archive, dry_run=False,
            expect_source_sha256=fresh["source_sha256"],
        )
        self.assertEqual(applied["outcome"], "applied")

    def test_a_second_compaction_keeps_the_projection(self) -> None:
        self.history_board()
        before = workboard.load_workboard(self.ledger, strict=True)
        workboard.compact(self.ledger, self.archive, dry_run=False)
        second = self.ledger.parent / "OPEN-WORK-archive-second.md"
        workboard.compact(self.ledger, second, dry_run=False)
        after = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(self.shape(after), self.shape(before))
        self.assertIn("kind=compact", second.read_text(encoding="utf-8"))


class CompactionCliTests(CompactionFixture):
    def run_cli(self, *argv: str) -> tuple[int, dict[str, object], str]:
        import contextlib
        import io

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = workboard.main(list(argv))
        payload = json.loads(out.getvalue().splitlines()[0]) if out.getvalue().strip() else {}
        return code, payload, err.getvalue()

    def test_cli_defaults_to_dry_run_then_applies(self) -> None:
        self.history_board()
        code, report, err = self.run_cli(
            "compact", "--path", str(self.ledger), "--archive", str(self.archive)
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(report["outcome"], "dry-run")
        self.assertFalse(self.archive.exists())

        code, report, err = self.run_cli(
            "compact", "--path", str(self.ledger), "--archive", str(self.archive),
            "--apply", "--expect-source-sha256", report["source_sha256"],
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(report["outcome"], "applied")
        self.assertTrue(self.archive.exists())
        workboard.load_workboard(self.ledger, strict=True)

        code, report, err = self.run_cli(
            "compact", "--path", str(self.ledger), "--archive", str(self.archive), "--apply"
        )
        self.assertEqual(code, 3)
        self.assertIn("already exists", err)

    def test_cli_default_archive_name_sits_beside_the_ledger(self) -> None:
        self.history_board()
        code, report, err = self.run_cli("compact", "--path", str(self.ledger))
        self.assertEqual(code, 0, err)
        archive = Path(report["archive_path"])
        self.assertEqual(archive.parent, self.ledger.parent)
        self.assertRegex(archive.name, r"^OPEN-WORK-archive-\d{4}-\d{2}-\d{2}\.md$")
        self.assertFalse(archive.exists())


if __name__ == "__main__":
    unittest.main()
