"""Content corrections use temporary ledgers and preserve historical events."""

from pathlib import Path
import sys
import tempfile
import unittest

PYTHON_DIR = Path(__file__).resolve().parents[1]
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from chrono_state import workboard


class RestateTests(unittest.TestCase):
    CORRECTION = {
        "summary": "Consult says 'keep queued' — revised decision",
        "why": "Evidence changes the rationale; preserve the audit trail",
        "next_action": "read `consult.md` then decide; keep $literal",
    }

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / "OPEN-WORK.md"

    def append(self, kind, **facts):
        return workboard.append_event(kind, path=self.ledger, **facts)

    def seed(self, shape, name="case"):
        self.ledger = self.root / f"{name}-{shape}.md"
        focus = {
            "summary": "Unrelated work", "why": "Keep existing focus",
            "next_action": "continue focus",
        }
        if shape == "legacy":
            self.ledger.write_text(
                workboard.format_event("start", item_id="FOCUS", **focus),
                encoding="utf-8",
            )
        else:
            self.append("start", alias="FOCUS", **focus)
        facts = {
            "summary": "Original summary", "why": "Original rationale",
            "resume_action": "Original action",
        }
        if shape == "legacy":
            # append_event upgrades new openings, so seed the actual historical
            # item_id wire format rather than accidentally testing two new IDs.
            with self.ledger.open("a", encoding="utf-8") as handle:
                handle.write(workboard.format_event("queue", item_id="TARGET", **facts))
            identity = {"item_id": "TARGET"}
        else:
            opening = self.append("queue", alias="TARGET", **facts)
            identity = {"work_id": opening.fields["work_id"]}
        workboard.load_workboard(self.ledger, strict=True)
        return identity

    def test_queued_correction_preserves_bytes_identity_focus_and_census(self):
        for shape in ("legacy", "current", "current-alias"):
            with self.subTest(shape=shape):
                identity = self.seed(shape)
                if shape == "current-alias":
                    identity = {"item_id": "TARGET"}
                before_bytes = self.ledger.read_bytes()
                before = workboard.load_workboard(self.ledger, strict=True)
                original = next(item for item in before.items if item.alias == "TARGET")
                event = self.append("restate", **identity, **self.CORRECTION)
                after_bytes = self.ledger.read_bytes()
                after = workboard.load_workboard(self.ledger, strict=True)
                revised = next(item for item in after.items if item.alias == "TARGET")

                self.assertEqual(after_bytes[:len(before_bytes)], before_bytes)
                self.assertEqual(len(after_bytes[len(before_bytes):].splitlines()), 1)
                self.assertEqual(after.document.records[:-1], before.document.records)
                self.assertEqual(after.document.records[-1], event)
                self.assertEqual(revised.state, "queued")
                self.assertEqual(revised.work_id, original.work_id)
                self.assertEqual(revised.disposition, original.disposition)
                self.assertEqual(revised.summary, self.CORRECTION["summary"])
                self.assertEqual(revised.why, self.CORRECTION["why"])
                self.assertEqual(revised.resume_action, self.CORRECTION["next_action"])
                self.assertEqual(revised.last_event_id, event.event_id)
                self.assertEqual(after.active_work_id, before.active_work_id)
                self.assertEqual(after.next_action, "continue focus")
                self.assertEqual(after.terminal_work_ids, before.terminal_work_ids)
                self.assertEqual(after.known_work_ids, before.known_work_ids)
                self.assertEqual(
                    [item for item in after.items if item.alias != "TARGET"],
                    [item for item in before.items if item.alias != "TARGET"],
                )
                self.assertEqual(
                    set(event.fields) & {"work_id", "item_id"},
                    {"item_id"} if shape == "legacy" else {"work_id"},
                )
                census = workboard.compare_item_censuses(before.document, after.document)
                self.assertTrue(census["ok"])
                self.assertTrue(census["state_preserved"])
                # Migration census retains original text; current text comes
                # from the projection, not a rewrite of the migration preimage.
                rows = workboard.resume_rows(self.ledger)
                row = next(row for row in rows if row[0] == "TARGET")
                self.assertIn(self.CORRECTION["summary"], row[1])
                self.assertIn(self.CORRECTION["why"], row[1])
                self.assertEqual(row[2], self.CORRECTION["next_action"])

    def test_restate_preserves_active_blocked_waiting_and_idle_focus(self):
        for shape in ("legacy", "current"):
            for state in ("active", "blocked", "waiting", "idle"):
                with self.subTest(shape=shape, state=state):
                    identity = self.seed(shape, state)
                    if state == "active":
                        self.append("switch", **identity, next_action="start target")
                    elif state == "blocked":
                        self.append("switch", **identity, next_action="start target")
                        self.append("block", **identity, resume_action="await evidence")
                    elif state == "waiting":
                        self.append(
                            "complete", item_id="FOCUS", waiting_id="TARGET",
                            resume_action="await decision",
                        )
                    else:
                        self.append("idle")
                    before = workboard.load_workboard(self.ledger, strict=True)
                    self.append("restate", **identity, **self.CORRECTION)
                    after = workboard.load_workboard(self.ledger, strict=True)
                    target = next(item for item in after.items if item.alias == "TARGET")
                    self.assertEqual(target.state, "queued" if state == "idle" else state)
                    self.assertEqual(target.resume_action, self.CORRECTION["next_action"])
                    self.assertEqual(after.active_work_id, before.active_work_id)
                    self.assertEqual(after.waiting_work_id, before.waiting_work_id)
                    self.assertEqual(after.idle, before.idle)
                    self.assertEqual(
                        after.next_action,
                        self.CORRECTION["next_action"] if state == "active" else None,
                    )

    def test_terminal_and_unknown_targets_are_refused_without_writes(self):
        for shape in ("legacy", "current"):
            for terminal in ("complete", "archive", "drop", "unknown"):
                with self.subTest(shape=shape, terminal=terminal):
                    identity = self.seed(shape, terminal)
                    if terminal == "unknown":
                        identity = ({"item_id": "MISSING"} if shape == "legacy"
                                    else {"work_id": "W-" + "f" * 32})
                    elif terminal == "drop":
                        drop_identity = ({"request_id": "TARGET"} if shape == "legacy"
                                         else identity)
                        self.append("drop", **drop_identity, summary="Withdrawn", why="Obsolete")
                    else:
                        self.append(terminal, **identity)
                    before = self.ledger.read_bytes()
                    with self.assertRaisesRegex(workboard.WorkboardConsistencyError, "is not open"):
                        self.append("restate", **identity, **self.CORRECTION)
                    self.assertEqual(self.ledger.read_bytes(), before)
                    workboard.load_workboard(self.ledger, strict=True)

    def test_invalid_correction_fields_are_refused_without_writes(self):
        identity = self.seed("current")
        invalid = []
        for field in self.CORRECTION:
            invalid.append({key: value for key, value in self.CORRECTION.items() if key != field})
            invalid.append({**self.CORRECTION, field: " "})
            invalid.append({**self.CORRECTION, field: "two\nlines"})
        invalid.extend([
            {**self.CORRECTION, "focus_state": "idle"},
            {**self.CORRECTION, "item_id": "TARGET"},
            {**self.CORRECTION, "resume_action": "unexpected alternative"},
        ])
        for facts in invalid:
            with self.subTest(facts=facts):
                before = self.ledger.read_bytes()
                with self.assertRaises(workboard.WorkboardConsistencyError):
                    self.append("restate", **identity, **facts)
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_ambiguous_alias_requires_explicit_identity(self):
        identity = self.seed("current")
        with self.ledger.open("a", encoding="utf-8") as handle:
            handle.write(workboard.format_event(
                "queue", work_id="W-" + "a" * 32, alias="TARGET",
                summary="Another historical opening", why="Alias collision",
                resume_action="keep separate",
            ))
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(workboard.WorkboardConsistencyError, "ambiguous"):
            self.append("restate", item_id="TARGET", **self.CORRECTION)
        self.assertEqual(self.ledger.read_bytes(), before)
        self.append("restate", **identity, **self.CORRECTION)
        view = workboard.load_workboard(self.ledger, strict=True)
        untouched = next(item for item in view.items if item.work_id == "W-" + "a" * 32)
        self.assertEqual(untouched.summary, "Another historical opening")

    def test_repeated_restate_retains_each_correction(self):
        identity = self.seed("legacy")
        first = self.append("restate", **identity, **self.CORRECTION)
        before = self.ledger.read_bytes()
        second = self.append("restate", **identity, **{**self.CORRECTION, "summary": "Further correction"})
        after = workboard.load_workboard(self.ledger, strict=True)
        self.assertTrue(self.ledger.read_bytes().startswith(before))
        self.assertIn(first, after.document.records)
        self.assertEqual(after.document.records[-1], second)
        self.assertEqual(next(item for item in after.items if item.alias == "TARGET").summary,
                         "Further correction")

    def test_existing_advance_still_requires_active_target(self):
        identity = self.seed("current")
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(workboard.WorkboardConsistencyError, "is not active"):
            self.append("advance", **identity, next_action="cannot advance queued work")
        self.assertEqual(self.ledger.read_bytes(), before)
        self.append("restate", **identity, **self.CORRECTION)


class AppendCliTests(unittest.TestCase):
    """``workboard append`` is what bin/workboard-append.sh execs."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = Path(temporary.name) / "OPEN-WORK.md"

    def run_cli(self, *argv):
        import contextlib
        import io
        import json

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = workboard.main(["append", *argv, "--path", str(self.ledger)])
        payload = json.loads(out.getvalue()) if out.getvalue().strip() else {}
        return code, payload, err.getvalue()

    def test_every_kind_round_trips_through_the_cli(self):
        code, start, err = self.run_cli(
            "start", "--alias", "A", "--summary", "first", "--why", "w-a", "--next", "do a"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(start["kind"], "start")
        self.assertRegex(start["work_id"], r"^W-[0-9a-f]{32}$")
        self.assertRegex(start["event_id"], r"^EV-[0-9a-f]{12}$")
        self.assertTrue(start["line"].startswith("- workboard-event/v1 event_id="))
        code, queue, _ = self.run_cli(
            "queue", "--alias", "B", "--summary", "second", "--why", "w-b", "--resume", "do b"
        )
        self.assertEqual(code, 0)
        code, *_ = self.run_cli(
            "restate", "--alias", "B", "--summary", "second!", "--why", "w-b2", "--next", "do b2"
        )
        self.assertEqual(code, 0)
        code, *_ = self.run_cli("advance", "--work-id", start["work_id"], "--next", "do a2")
        self.assertEqual(code, 0)
        code, *_ = self.run_cli(
            "fold", "--request-id", "R-1", "--target", "A", "--summary", "folded",
            "--why", "same ask", "--next", "do a3",
        )
        self.assertEqual(code, 0)
        code, *_ = self.run_cli("block", "--alias", "B", "--resume", "await x")
        self.assertEqual(code, 0)
        code, *_ = self.run_cli("switch", "--alias", "B", "--next", "unblock b")
        self.assertEqual(code, 0)
        code, *_ = self.run_cli(
            "drop", "--alias", "R-noise", "--summary", "a request", "--why", "noise"
        )
        self.assertEqual(code, 0)
        code, *_ = self.run_cli(
            "complete", "--work-id", queue["work_id"], "--successor", start["work_id"],
            "--next", "back to a",
        )
        self.assertEqual(code, 0)
        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(view.active_item_id, "A")
        self.assertEqual(view.next_action, "back to a")
        self.assertEqual([item.alias for item in view.items], ["A"])
        code, *_ = self.run_cli("complete", "--alias", "A", "--idle")
        self.assertEqual(code, 0)
        self.assertTrue(workboard.load_workboard(self.ledger, strict=True).idle)
        code, *_ = self.run_cli("idle")
        self.assertEqual(code, 0)
        code, *_ = self.run_cli(
            "queue", "--alias", "C", "--summary", "third", "--why", "w-c", "--resume", "do c"
        )
        self.assertEqual(code, 0)
        code, *_ = self.run_cli("switch", "--alias", "C", "--next", "do c now")
        self.assertEqual(code, 0)
        code, *_ = self.run_cli(
            "queue", "--alias", "D", "--summary", "fourth", "--why", "w-d", "--resume", "do d"
        )
        self.assertEqual(code, 0)
        code, *_ = self.run_cli("archive", "--alias", "C", "--waiting", "D", "--resume", "wait d")
        self.assertEqual(code, 0)
        view = workboard.load_workboard(self.ledger, strict=True)
        self.assertEqual(view.items[0].alias, "D")
        self.assertEqual(view.waiting_work_id, view.items[0].work_id)

    def test_missing_flags_are_a_usage_error_before_any_write(self):
        cases = {
            "queue": (("queue", "--alias", "A", "--summary", "s"), "missing --why --resume"),
            "start": (("start", "--summary", "s", "--why", "w", "--next", "n"), "missing --alias"),
            "advance": (("advance", "--next", "n"), "missing --alias|--work-id"),
            "fold": (("fold", "--request-id", "R", "--summary", "s", "--why", "w", "--next", "n"), "missing --target"),
        }
        for label, (argv, expected) in cases.items():
            with self.subTest(kind=label):
                code, payload, err = self.run_cli(*argv)
                self.assertEqual(code, 3)
                self.assertEqual(payload, {})
                self.assertIn(expected, err)
        self.assertFalse(self.ledger.exists())
        code, _, err = self.run_cli(
            "switch", "--alias", "A", "--work-id", "W-" + "a" * 32, "--next", "n"
        )
        self.assertEqual(code, 3)
        self.assertIn("either --alias or --work-id", err)
        self.assertFalse(self.ledger.exists())

    def test_library_refusals_surface_as_exit_three(self):
        code, _, err = self.run_cli(
            "queue", "--alias", "A", "--summary", "s", "--why", "w", "--resume", "r"
        )
        self.assertEqual(code, 3)
        self.assertIn("append rejected", err)
        self.assertFalse(self.ledger.exists())

    def test_flag_mapping_picks_schema_keys_by_identity_shape(self):
        opaque = "W-" + "b" * 32
        facts = workboard.append_facts_from_flags(
            "complete", {"alias": "A", "successor": opaque, "next": "n"}
        )
        self.assertEqual(facts, {"item_id": "A", "next_action": "n", "successor_work_id": opaque})
        facts = workboard.append_facts_from_flags(
            "drop", {"alias": "R", "summary": "s", "why": "w", "waiting": "B", "resume": "r"}
        )
        self.assertEqual(
            facts,
            {"request_id": "R", "summary": "s", "why": "w", "waiting_id": "B", "resume_action": "r"},
        )
        facts = workboard.append_facts_from_flags(
            "fold", {"request_id": "R", "target": opaque, "summary": "s", "why": "w", "next": "n"}
        )
        self.assertEqual(facts["target_work_id"], opaque)
        facts = workboard.append_facts_from_flags("block", {"work_id": opaque, "resume": "r", "idle": True})
        self.assertEqual(facts, {"work_id": opaque, "resume_action": "r", "focus_state": "idle"})
        with self.assertRaisesRegex(workboard.WorkboardConsistencyError, "one of"):
            workboard.append_facts_from_flags("adopt", {})


if __name__ == "__main__":
    unittest.main()
