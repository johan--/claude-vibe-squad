"""The dashboard's model truth: the effort beside the model, and a warning when a
spawn silently ran on a model other than the one it was dispatched on.

Measured 2026-09-23: a canary dispatched on claude.opus55.max ran one message on
claude-opus-5-5 and 25 on claude-opus-4-8. A safety classifier had stopped a reply,
Claude Code fell back without a word, and the card said claude-opus-5-5 throughout.

What can silently break, and the test that catches it:

  * **The detector reads nothing.** A dashboard that never warns looks exactly like
    a board where nothing fell back. ControlTest removes each signal and shows the
    positive tests stop passing.
  * **The detector warns about nothing.** Other lanes say "fallback" in plain text,
    subagents run on other models on purpose, harness messages carry the model
    "<synthetic>", and a spawn working on THIS feature quotes the classifier notice.
    None of those may raise the warning.
  * **The dashboard re-reads whole logs every second.** IncrementalTest pins the
    byte offset, the per-frame read bound, and the no-write-when-idle rule.
  * **The box moves.** RenderTest pins every row of every variant to one width.

Nothing here touches tmux, the live board, or the squad's /tmp state files.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD = REPO_ROOT / "bin" / "vs-board-dashboard.py"
REGISTRY = REPO_ROOT / "shared" / "registries" / "profiles.tsv"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))
import dispatch_context_builder as dcb  # noqa: E402

_ENV_KEYS = ("VAULT_ROOT", "NO_COLOR", "VS_DASH_COLOR", "VS_DASH_TRUECOLOR", "TERM",
             "COLORTERM", "VS_DASH_MODELSTATE")
_INSTANCE = itertools.count()
_SCRATCH = tempfile.mkdtemp(prefix="vs-dash-model-truth-")


def _load(**env):
    """Import the dashboard under an exact environment (colour and paths are read
    at import time). The default state path points into scratch, never /tmp."""
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["VAULT_ROOT"] = str(REPO_ROOT)
        os.environ["VS_DASH_MODELSTATE"] = os.path.join(_SCRATCH, "import-default.json")
        os.environ.update({k: v for k, v in env.items() if v is not None})
        spec = importlib.util.spec_from_file_location(
            f"vs_board_dashboard_mt_{next(_INSTANCE)}", DASHBOARD)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def tearDownModule() -> None:
    shutil.rmtree(_SCRATCH, ignore_errors=True)


DASH = _load(NO_COLOR="1")
WARN = DASH.WARN
SELECTED = "claude-opus-5-5"


# --- fixture logs, in Claude Code's stream-json shape ---------------------------
def _assistant(model, *blocks, parent=None):
    return {"type": "assistant", "message": {
        "model": model, "role": "assistant", "type": "message",
        "content": list(blocks) or [{"type": "text", "text": "working"}]},
        "parent_tool_use_id": parent}


NOTICE_TEXT = ("Your response above was stopped by a safety classifier — this is not a "
               "tool or API error. The rest of it was withheld.")
INIT = {"type": "system", "subtype": "init", "model": SELECTED}
NOTICE = {"type": "user", "parent_tool_use_id": None, "message": {
    "role": "user", "content": [{"type": "text", "text": NOTICE_TEXT}]}}
FALLBACK_BLOCK = {"type": "fallback", "from": {"model": "claude-opus-5-5"},
                  "to": {"model": "claude-opus-4-8"}}
REFUSAL_EVENT = {"type": "system", "subtype": "model_refusal_fallback", "trigger": "refusal",
                 "scope": "session", "original_model": "claude-opus-5-5",
                 "fallback_model": "claude-opus-4-8"}
# Everything in CLEAN looks like a signal to a substring match, and none is one.
CLEAN = [
    INIT,
    _assistant(SELECTED),
    {"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": NOTICE_TEXT}]}},
    {"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
        {"type": "text", "text": f"The packet says: ({NOTICE_TEXT})"}]}},
    _assistant(SELECTED, {"type": "text", "text": 'a "fallback" block, a model_refusal_fallback'}),
    _assistant("claude-haiku-4-5-20251001", parent="toolu_subagent"),
    _assistant("<synthetic>"),
]
FELL_BACK = [
    INIT,
    _assistant(SELECTED),
    NOTICE,
    _assistant("claude-opus-4-8", FALLBACK_BLOCK),
    REFUSAL_EVENT,
    _assistant("claude-opus-4-8", {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}),
    _assistant("claude-opus-4-8"),
]


def _lines(events):
    return "".join(json.dumps(e, separators=(",", ":"), ensure_ascii=False) + "\n"
                   for e in events).encode("utf-8")


class _TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vs-dash-mt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._patch(DASH, "MODEL_STATE", self.tmp / "model-state.json")

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def _log(self, events, name="lane.log"):
        path = self.tmp / name
        path.write_bytes(_lines(events))
        return path

    def _scan(self, events, name="lane.log"):
        return DASH._scan_log(str(self._log(events, name)), None)

    def _assert_warns(self, line, model):
        self.assertTrue(line.startswith(WARN), line)
        self.assertIn(f"→ {model}", line)


class DetectorTest(_TempDirCase):
    def test_a_clean_log_shows_no_warning(self) -> None:
        entry = self._scan(CLEAN)
        self.assertEqual((entry["fell_back"], entry["stops"], entry["model"]),
                         (False, 0, SELECTED))
        self.assertEqual(DASH._truth_line(entry, SELECTED, 46), "")

    def test_a_fallback_log_shows_the_warning_and_the_actual_model(self) -> None:
        entry = self._scan(FELL_BACK)
        self.assertEqual((entry["fell_back"], entry["fb_to"], entry["stops"], entry["model"]),
                         (True, "claude-opus-4-8", 1, "claude-opus-4-8"))
        line = DASH._truth_line(entry, SELECTED, 46)
        self._assert_warns(line, "opus-4-8")
        self.assertEqual(line, f"{WARN} fell back → opus-4-8 · 1 safety stop")

    def test_each_signal_is_enough_on_its_own(self) -> None:
        cases = {
            # the content block alone, then back on the dispatched model
            "block": ([INIT, _assistant("claude-opus-4-8", FALLBACK_BLOCK),
                       _assistant(SELECTED)], "opus-4-8, now back"),
            # the system event alone
            "event": ([INIT, _assistant(SELECTED), REFUSAL_EVENT, _assistant(SELECTED)],
                      "opus-4-8, now back"),
            # no record at all: only the main thread's model gives it away
            "drift": ([INIT, _assistant(SELECTED), _assistant("claude-sonnet-5")], "sonnet-5"),
        }
        for label, (events, expected) in cases.items():
            with self.subTest(signal=label):
                line = DASH._truth_line(self._scan(events, f"{label}.log"), SELECTED, 46)
                self.assertEqual(line, f"{WARN} fell back → {expected}")

    def test_classifier_stops_without_a_fallback_are_still_counted(self) -> None:
        entry = self._scan([INIT, _assistant(SELECTED), NOTICE, _assistant(SELECTED), NOTICE,
                            _assistant(SELECTED)])
        self.assertEqual(DASH._truth_line(entry, SELECTED, 46), f"{WARN} 2 safety stops")

    def test_other_lanes_logs_never_warn(self) -> None:
        """codex/agy/grok logs contain the word "fallback" in plain text."""
        codex = [{"type": "item.completed", "item": {"type": "agent_message",
                  "text": "added a fallback block; model_refusal_fallback; safety classifier"}},
                 {"role": "assistant", "content": "fallback", "model": "gpt-5.6-sol"}]
        entry = self._scan(codex)
        self.assertEqual((entry["fell_back"], entry["stops"], entry["model"]), (False, 0, ""))
        self.assertEqual(DASH._truth_line(entry, "gpt-5.6-sol", 46), "")

    def test_names_that_differ_only_by_date_or_context_suffix_are_one_model(self) -> None:
        for actual in ("claude-opus-5-5", "claude-opus-5-5[1m]", "claude-opus-5-5-20260901",
                       "CLAUDE-OPUS-5-5"):
            with self.subTest(actual=actual):
                self.assertEqual(DASH._model_key(actual), DASH._model_key(SELECTED))
        self.assertNotEqual(DASH._model_key("claude-opus-5"), DASH._model_key(SELECTED))

    def test_a_spawn_without_a_known_model_skips_only_the_drift_check(self) -> None:
        """The snapshot reports the bare lane name when there was no --model."""
        drift = self._scan([_assistant("claude-opus-4-8")], "drift.log")
        spawn = {"lane": "claude", "model": "claude", "profile": None}
        self.assertEqual(DASH._selected_model(spawn), "")
        self.assertEqual(DASH._truth_line(drift, DASH._selected_model(spawn), 46), "")
        fell = self._scan(FELL_BACK, "fell.log")
        self.assertIn("fell back", DASH._truth_line(fell, "", 46))

    def test_current_cheap_profiles_resolve_without_false_fallback(self) -> None:
        for lane, profile_id in (("claude", "claude.sonnet55.high"),
                                 ("claude", "claude.haiku.default"),
                                 ("codex", "codex.terra.default"),
                                 ("gemini", "gemini.flash.distill")):
            with self.subTest(profile=profile_id):
                profile = dcb._profile_row(REPO_ROOT, lane=lane, profile_id=profile_id)
                spawn = {"lane": lane, "model": SELECTED, "profile": profile}
                selected = DASH._selected_model(spawn)
                self.assertEqual(selected, profile["model_id"])
                self.assertNotEqual(selected, SELECTED)
                entry = self._scan([_assistant(selected)], profile_id + ".log")
                self.assertEqual(DASH._truth_line(entry, selected, 46), "")
                if lane == "claude":
                    drift = self._scan([_assistant(SELECTED)], profile_id + "-drift.log")
                    self._assert_warns(DASH._truth_line(drift, selected, 46), "opus-5-5")

    def test_the_profile_model_wins_over_the_cli_flag(self) -> None:
        spawn = {"lane": "claude", "model": "opus", "profile": {"model_id": SELECTED}}
        self.assertEqual(DASH._selected_model(spawn), SELECTED)
        self.assertEqual(DASH._selected_model({"lane": "claude", "model": SELECTED,
                                               "profile": None}), SELECTED)


class ControlTest(_TempDirCase):
    """Take the detector away and the positive tests above must stop passing."""

    def test_control_removing_the_event_reader_hides_the_fallback(self) -> None:
        self._patch(DASH, "_scan_event", lambda event, entry: None)
        line = DASH._truth_line(self._scan(FELL_BACK), SELECTED, 46)
        with self.assertRaises(AssertionError):
            self._assert_warns(line, "opus-4-8")

    def test_control_removing_the_drift_check_hides_an_unrecorded_fallback(self) -> None:
        entry = self._scan([INIT, _assistant(SELECTED), _assistant("claude-sonnet-5")])
        self._assert_warns(DASH._truth_line(entry, SELECTED, 46), "sonnet-5")
        self._patch(DASH, "_model_key", lambda model: "")
        with self.assertRaises(AssertionError):
            self._assert_warns(DASH._truth_line(entry, SELECTED, 46), "sonnet-5")

    def test_control_removing_the_attach_step_hides_it_from_the_card(self) -> None:
        log = self._log(FELL_BACK)
        make = lambda: [{"task_id": "TASK-X", "lane": "claude", "specialist": "ui-engineer",
                         "started": str(DASH.NOW - 60), "pid": "0", "log": str(log),
                         "summary": "s", "model": SELECTED,
                         "profile": {"model_id": SELECTED, "effort": "max"}, "effort": "max"}]
        spawns = make()
        DASH._attach_model_truth(spawns)
        self.assertIn(f"{WARN} fell back → opus-4-8", "\n".join(DASH._render_card(spawns[0], 46)))
        self._patch(DASH, "_attach_model_truth", lambda active: None)
        spawns = make()
        DASH._attach_model_truth(spawns)
        with self.assertRaises(AssertionError):
            self.assertIn("fell back", "\n".join(DASH._render_card(spawns[0], 46)))


class IncrementalTest(_TempDirCase):
    def _counting(self):
        calls = []
        original = DASH._scan_event
        self._patch(DASH, "_scan_event", lambda e, t: (calls.append(e), original(e, t)))
        return calls

    def test_a_later_frame_reads_only_what_was_appended(self) -> None:
        path = self._log(CLEAN)
        entry = DASH._scan_log(str(path), None)
        self.assertEqual(entry["offset"], path.stat().st_size)
        calls = self._counting()
        self.assertEqual(DASH._scan_log(str(path), dict(entry)), entry)
        self.assertEqual(calls, [])  # nothing new, nothing parsed
        with path.open("ab") as handle:
            handle.write(_lines(FELL_BACK[2:]))
        entry = DASH._scan_log(str(path), entry)
        self.assertEqual(len(calls), len(FELL_BACK) - 2)  # only the appended events
        self.assertEqual((entry["fell_back"], entry["stops"], entry["model"]),
                         (True, 1, "claude-opus-4-8"))

    def test_one_frame_reads_at_most_a_chunk_and_later_frames_catch_up(self) -> None:
        events = [INIT] + [_assistant(SELECTED)] * 40 + FELL_BACK[2:]
        path = self._log(events)
        whole = DASH._scan_log(str(path), None)
        self._patch(DASH, "LOG_CHUNK_BYTES", 512)
        entry, frames = None, 0
        while entry is None or entry["offset"] < path.stat().st_size:
            before = entry["offset"] if entry else 0
            entry = DASH._scan_log(str(path), entry)
            self.assertLessEqual(entry["offset"] - before, 512)
            frames += 1
            self.assertLess(frames, 200, "scan stopped advancing")
        self.assertGreater(frames, 5)
        for key in ("fell_back", "fb_to", "stops", "model", "offset"):
            self.assertEqual(entry[key], whole[key], key)

    def test_a_line_longer_than_a_chunk_is_skipped_not_stalled_on(self) -> None:
        giant = _assistant(SELECTED, {"type": "text", "text": "x" * 5000})
        path = self._log([INIT, giant] + FELL_BACK[2:])
        self._patch(DASH, "LOG_CHUNK_BYTES", 1024)
        entry = None
        for _ in range(50):
            entry = DASH._scan_log(str(path), entry)
            if entry["offset"] >= path.stat().st_size:
                break
        self.assertEqual(entry["offset"], path.stat().st_size)
        self.assertTrue(entry["fell_back"])
        self.assertEqual(entry["stops"], 1)

    def test_a_half_written_line_waits_for_its_newline(self) -> None:
        path = self._log([INIT])
        size = path.stat().st_size
        tail = _lines([NOTICE])
        with path.open("ab") as handle:
            handle.write(tail[:-10])
        entry = DASH._scan_log(str(path), None)
        self.assertEqual((entry["offset"], entry["stops"]), (size, 0))
        with path.open("ab") as handle:
            handle.write(tail[-10:])
        entry = DASH._scan_log(str(path), entry)
        self.assertEqual((entry["offset"], entry["stops"]), (path.stat().st_size, 1))

    def test_a_replaced_or_truncated_log_is_scanned_from_the_start(self) -> None:
        path = self._log(FELL_BACK)
        entry = DASH._scan_log(str(path), None)
        path.write_bytes(_lines(CLEAN[:2]))  # truncated in place: shorter than the offset
        self.assertFalse(DASH._scan_log(str(path), dict(entry))["fell_back"])
        replacement = self.tmp / "other.log"
        replacement.write_bytes(_lines(CLEAN) + b"\n" * 4096)  # longer, but a new inode
        os.replace(replacement, path)
        self.assertFalse(DASH._scan_log(str(path), dict(entry))["fell_back"])

    def test_a_malformed_saved_entry_is_discarded_not_trusted(self) -> None:
        path = self._log(FELL_BACK)
        ino = path.stat().st_ino
        for bad in ({"ino": ino, "offset": "12"}, {"ino": ino, "offset": 0, "stops": "x"}, []):
            with self.subTest(bad=bad):
                self.assertTrue(DASH._scan_log(str(path), bad)["fell_back"])

    def _spawns(self, *logs):
        return [{"task_id": f"TASK-{i}", "lane": "claude", "model": SELECTED, "log": str(log)}
                for i, log in enumerate(logs)]

    def test_state_is_saved_atomically_and_only_when_a_scan_moved(self) -> None:
        a, b = self._log(FELL_BACK, "a.log"), self._log(CLEAN, "b.log")
        saves = []
        original = DASH._save_model_state
        self._patch(DASH, "_save_model_state", lambda s: (saves.append(s), original(s)))
        spawns = self._spawns(a, b)
        DASH._attach_model_truth(spawns)
        self.assertTrue(spawns[0]["truth"]["fell_back"])
        self.assertFalse(spawns[1]["truth"]["fell_back"])
        self.assertEqual(sorted(json.loads(DASH.MODEL_STATE.read_text())), sorted([str(a), str(b)]))
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(".tmp")], [])
        DASH._attach_model_truth(self._spawns(a, b))
        self.assertEqual(len(saves), 1, "an unchanged frame rewrote the state file")
        again = self._spawns(a)
        DASH._attach_model_truth(again)  # a new process: the offset comes from the file
        self.assertTrue(again[0]["truth"]["fell_back"])

    def test_no_readable_log_means_no_state_file_at_all(self) -> None:
        """The other dashboard suites render spawns with /nonexistent logs and do
        not isolate this path; they must never write the live squad's file."""
        DASH._attach_model_truth(self._spawns("/nonexistent/a.log", ""))
        self.assertFalse(DASH.MODEL_STATE.exists())

    def test_a_corrupt_state_file_is_ignored_and_stale_entries_are_pruned(self) -> None:
        DASH.MODEL_STATE.write_text("{not json")
        spawns = self._spawns(self._log(FELL_BACK))
        DASH._attach_model_truth(spawns)
        self.assertTrue(spawns[0]["truth"]["fell_back"])
        state = json.loads(DASH.MODEL_STATE.read_text())
        state["/gone.log"] = {"seen": DASH.NOW - DASH.MODEL_STATE_TTL_S - 1}
        DASH.MODEL_STATE.write_text(json.dumps(state))
        with Path(spawns[0]["log"]).open("ab") as handle:
            handle.write(_lines([NOTICE]))
        DASH._attach_model_truth(self._spawns(Path(spawns[0]["log"])))
        self.assertNotIn("/gone.log", json.loads(DASH.MODEL_STATE.read_text()))


def _card(module, *, effort="max", truth=None, cw=46, pid="0", specialist="ui-engineer"):
    spawn = {"task_id": "TASK-X", "lane": "claude", "specialist": specialist,
             "started": str(module.NOW - 60), "pid": pid, "log": "/nonexistent",
             "summary": "a summary long enough that it wraps onto the second summary row",
             "model": SELECTED, "profile": {"model_id": SELECTED, "effort": effort},
             "effort": effort, "truth": truth}
    return module._render_card(spawn, cw, 0)


FELL = {"fell_back": True, "fb_to": "claude-opus-4-8", "stops": 1, "model": "claude-opus-4-8"}
TRUTHS = {
    "none": None,
    "clean": {"fell_back": False, "fb_to": "", "stops": 0, "model": SELECTED},
    "fell": FELL,
    "fell-many-stops": {**FELL, "stops": 12},
    "recovered": {**FELL, "model": SELECTED},
    "stops-only": {"fell_back": False, "fb_to": "", "stops": 3, "model": SELECTED},
    "long-model": {**FELL, "model": "claude-some-extremely-long-model-name-2027-preview"},
}


class RenderTest(unittest.TestCase):
    def test_the_effort_sits_beside_the_model_and_the_model_gives_way_first(self) -> None:
        self.assertEqual(DASH._model_label(SELECTED, "max", 32), "claude-opus-5-5 · max")
        self.assertEqual(DASH._model_label(SELECTED, "max", 14), "opus-5-5 · max")
        narrow = DASH._model_label(SELECTED, "max", 10)
        self.assertLessEqual(DASH._dwidth(narrow), 10)
        self.assertTrue(narrow.endswith(" · max"), narrow)
        self.assertEqual(DASH._model_label(SELECTED, "", 32), SELECTED)  # unresolved: as before

    def test_every_variant_keeps_every_row_at_the_box_width(self) -> None:
        for module in (DASH, _load(VS_DASH_COLOR="1")):
            for cw in range(28, 47):
                for label, truth in TRUTHS.items():
                    for effort in ("max", "default", ""):
                        rows = _card(module, effort=effort, truth=truth, cw=cw)
                        with self.subTest(colour=not module._NO_COLOR, cw=cw, truth=label,
                                          effort=effort):
                            self.assertEqual(len(rows), module.CARD_H)
                            self.assertEqual({module._dwidth(r) for r in rows}, {cw + 2})

    def test_the_warning_takes_the_tagline_row_and_only_when_there_is_one(self) -> None:
        spec = "backend-engineer"
        tagline = DASH._card_meta(spec)[2]
        self.assertTrue(tagline, "fixture needs a card with a tagline")
        clean = "\n".join(_card(DASH, truth=TRUTHS["clean"], specialist=spec))
        self.assertIn(tagline[:12], clean)
        self.assertNotIn(WARN, clean)
        rows = _card(DASH, truth=FELL, specialist=spec)
        self.assertTrue(rows[8].startswith(f"│{WARN} fell back → opus-4-8"), rows[8])
        self.assertNotIn(tagline[:12], "\n".join(rows))

    def test_a_narrow_card_keeps_the_stop_count(self) -> None:
        self.assertEqual(DASH._truth_line(FELL, SELECTED, 28), f"{WARN} fell → opus-4-8 · 1 stop")

    def test_no_card_carries_a_bare_warning_sign(self) -> None:
        """tmux 3.6a draws a bare U+26A0 in one cell while _dwidth counts two
        (measured 2026-09-23), so a bare one shifts the box edge. The old
        "⚠ stopped" activity row did exactly that on every card with a dead pid."""
        def bare(text):
            return any(ch == "⚠" and text[i + 1:i + 2] != "️"
                       for i, ch in enumerate(text))
        self.assertTrue(bare("⚠ stopped"))  # control: the check can see one
        self.assertFalse(bare(f"{WARN} stopped"))
        dead = subprocess.Popen(["/bin/sleep", "0"])
        dead.wait()
        for label, truth in TRUTHS.items():
            with self.subTest(truth=label):
                self.assertFalse(bare("".join(_card(DASH, truth=truth, pid=str(dead.pid)))))

    def test_every_crew_avatar_is_exactly_the_avatar_width(self) -> None:
        """53 of 73 cards had an emoji in a 13-character art row, so padding by
        characters left rows 14-15 cells wide."""
        cards = sorted((REPO_ROOT / "shared" / "cards").glob("*.card"))
        self.assertGreater(len(cards), 10)
        by_chars = 0
        for path in cards:
            name, _motif, _tag, frame = DASH._card_meta(path.stem)
            for row in frame:
                self.assertEqual(DASH._dwidth(row), DASH.AVATAR_W, f"{path.stem}: {row!r}")
            raw = [line for line in path.read_text(encoding="utf-8").splitlines()]
            by_chars += any(DASH._dwidth(r[:DASH.AVATAR_W].ljust(DASH.AVATAR_W)) != DASH.AVATAR_W
                            for r in raw)
        self.assertGreater(by_chars, 0, "control: no card exercises the cell-width fit")


class EndToEndTest(unittest.TestCase):
    """A whole frame, as a real process: one fell-back spawn, one clean one."""

    FELL_ID, CLEAN_ID = "TASK-2026-09-23-9001-fellback", "TASK-2026-09-23-9002-clean"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vs-dash-mt-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        board = self.vault / "_state" / "board-dispatch"
        board.mkdir(parents=True)
        (self.vault / "shared" / "registries").mkdir(parents=True)
        shutil.copy(REGISTRY, self.vault / "shared" / "registries" / "profiles.tsv")
        shutil.copytree(REPO_ROOT / "shared" / "cards", self.vault / "shared" / "cards")
        profile = dcb._profile_row(self.vault, lane="claude", profile_id="claude.opus55.max")  # noqa: SLF001
        digest = dcb._sha256_bytes(  # noqa: SLF001
            dcb._canonical_json({"profile_id": "claude.opus55.max", "profile": profile}))  # noqa: SLF001
        self.logs = {}
        for task_id, events in ((self.FELL_ID, FELL_BACK), (self.CLEAN_ID, CLEAN)):
            (board / f"{task_id}.d-1.context.json").write_text(
                json.dumps({"authority": {"selected_model_sha256": digest}}), encoding="utf-8")
            self.logs[task_id] = board / f"{task_id}.d-1.log"
            self.logs[task_id].write_bytes(_lines(events))

    def _render(self, **env):
        started = int(time.time()) - 60
        stream = "".join(
            f"@SPAWN\t{task_id}\tclaude\t{spec}\t{started}\t0\t{self.logs[task_id]}\t{SELECTED}\n"
            f"@SUMMARY\t{task_id} summary\n"
            for task_id, spec in ((self.FELL_ID, "ui-engineer"), (self.CLEAN_ID, "backend-engineer")))
        result = subprocess.run(
            [sys.executable, str(DASHBOARD), "--stdin"], input=stream, capture_output=True,
            text=True, timeout=60,
            env={**{k: v for k, v in os.environ.items() if k not in _ENV_KEYS},
                 "VAULT_ROOT": str(self.vault), "VS_DASH_WIDTH": "110", "VS_DASH_HEIGHT": "30",
                 "VS_DASH_HITMAP": str(self.tmp / "hitmap.tsv"),
                 "VS_SWARM_STATUS": str(self.tmp / "swarm.status"),
                 "VS_DASH_HISTSTATE": str(self.tmp / "hist.state"),
                 "VS_DASH_MODELSTATE": str(self.tmp / "model-state.json"), **env})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_frame_shows_the_effort_and_warns_only_on_the_fell_back_card(self) -> None:
        out = self._render()
        self.assertEqual(out.count("claude-opus-5-5 · max"), 2)
        self.assertEqual(out.count("fell back"), 1)
        self.assertIn(f"{WARN} fell back → opus-4-8 · 1 safety stop", out)
        self.assertTrue((self.tmp / "model-state.json").exists())

    def test_the_frame_rows_all_keep_one_width(self) -> None:
        dash = _load(NO_COLOR="1")
        rows = [r for r in self._render().splitlines() if r[:1] in ("╭", "│", "╰")]
        self.assertEqual(len(rows), dash.CARD_H)
        self.assertEqual(len({dash._dwidth(r) for r in rows}), 1)

    def test_plain_destinations_emit_no_escape_codes_and_keep_the_warning(self) -> None:
        for label, env in (("pipe", {}), ("NO_COLOR", {"NO_COLOR": "1"}),
                           ("TERM=dumb", {"TERM": "dumb"}), ("VS_DASH_COLOR=0", {"VS_DASH_COLOR": "0"})):
            with self.subTest(destination=label):
                out = self._render(**env)
                self.assertNotIn("\033", out)
                self.assertIn(f"{WARN} fell back → opus-4-8", out)

    def test_control_the_escape_check_can_see_escape_codes(self) -> None:
        out = self._render(VS_DASH_COLOR="1")
        self.assertIn("\033[", out)
        self.assertIn(f"{WARN} fell back → opus-4-8", DASH._ANSI_RE.sub("", out))


if __name__ == "__main__":
    unittest.main()
