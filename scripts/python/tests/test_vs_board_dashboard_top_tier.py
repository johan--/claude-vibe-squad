"""The dashboard's top-tier cue: a rainbow border that moves, and degrades.

Three things can silently break this feature, so each gets a test with a control:

  * **The profile never resolves.** A dispatch context records the SHA-256 of the
    selected profile BUNDLE, not a `profile_id` -- so the dashboard re-hashes the
    profile registry the way dispatch_context_builder does and matches digests. If
    either side's hashing drifts, nothing matches and every card renders normal:
    a silent, invisible failure. ProfileResolutionTest computes the digest with
    the DISPATCHER's own helpers, so drift on either side fails here.
  * **The border stops moving.** A border that is merely coloured looks fine in a
    single frame. AnimationTest compares two frames, and carries a control that
    shows the comparison fails when the cue is taken away.
  * **The box shifts.** The ◆ MAX label replaces rule cells rather than adding
    any. WidthTest pins every row of a top-tier card to the same display width as
    a normal one.

Nothing here touches tmux, the live board, or the squad's /tmp state files.
"""

from __future__ import annotations

import csv
import importlib.util
import itertools
import json
import os
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

_ENV_KEYS = (
    "VAULT_ROOT", "NO_COLOR", "VS_DASH_COLOR", "VS_DASH_TRUECOLOR",
    "TERM", "COLORTERM",
)
_INSTANCE = itertools.count()


def _load(**env):
    """Import the dashboard under an exact environment.

    Colour support, truecolor support and VAULT_ROOT are all read at import time,
    so each rendering mode needs its own module instance."""
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    try:
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.setdefault("VAULT_ROOT", str(REPO_ROOT))
        os.environ.update({k: v for k, v in env.items() if v is not None})
        name = f"vs_board_dashboard_tt_{next(_INSTANCE)}"
        spec = importlib.util.spec_from_file_location(name, DASHBOARD)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# VS_DASH_COLOR=1 forces colour on: the test runner's stdout is a pipe, and the
# renderer correctly refuses to emit escape codes into one.
TRUE = _load(VS_DASH_COLOR="1", VS_DASH_TRUECOLOR="1")
C256 = _load(VS_DASH_COLOR="1", VS_DASH_TRUECOLOR="0")
PLAIN = _load(NO_COLOR="1")

MAX_PROFILE = {"profile_id": "claude.opus55.max", "lane": "claude",
               "model_id": "claude-opus-5-5", "effort": "max", "flags": "none",
               "usage": "primary"}
ULTRA_PROFILE = {"profile_id": "codex.sol.ultra", "lane": "codex",
                 "model_id": "gpt-5.6-sol", "effort": "max", "flags": "ultra",
                 "usage": "escalation"}
NORMAL_PROFILE = {"profile_id": "claude.opus55.high", "lane": "claude",
                  "model_id": "claude-opus-5-5", "effort": "high", "flags": "none",
                  "usage": "primary"}


def _card(module, profile, phase=0, cw=46):
    """One rendered card. Every input is pinned, so two renders of the same
    (profile, phase) are byte-identical and only the cue can move."""
    spawn = {
        "task_id": "TASK-TEST-0001", "lane": "claude", "specialist": "ui-engineer",
        "started": str(module.NOW - 90), "pid": "0",
        "log": "/nonexistent/vs-dash-top-tier.log",
        "summary": "a task", "model": "claude-opus-5-5", "profile": profile,
    }
    return module._render_card(spawn, cw, phase)


def _plainify(module, rows):
    return [module._ANSI_RE.sub("", row) for row in rows]


class TopTierSelectionTest(unittest.TestCase):
    def test_operator_approved_model_upgrades_and_advisor_routes(self) -> None:
        with REGISTRY.open(newline="") as stream:
            profiles = {row["profile_id"]: row for row in csv.DictReader(stream, delimiter="\t")}
        self.assertEqual(profiles["codex.terra.default"]["model_id"], "gpt-6.1-terra")
        self.assertEqual(profiles["claude.haiku.default"]["model_id"], "claude-haiku-5-5")
        fable = [row for name, row in profiles.items() if name.startswith("claude.fable.")]
        self.assertEqual(len(fable), 3)
        self.assertEqual({row["model_id"] for row in fable}, {"claude-fable-5-1"})
        self.assertEqual({row["lane"] for row in profiles.values()},
                         {"codex", "claude", "gemini", "grok"})
        with (REGISTRY.parents[1] / "specialist-runtime-map.tsv").open(newline="") as stream:
            routes = {row["specialist"]: row for row in csv.DictReader(stream, delimiter="\t")}
        for role, field, expected in (
            ("exodia", "backup_profile", "claude.opus55.high"),
            ("exodia", "review_profile", "claude.opus55.max"),
            ("kestrel", "primary_profile", "claude.opus55.high"),
            ("kestrel", "escalate_profile", "claude.opus55.max"),
            ("vega", "backup_profile", "claude.opus55.high"),
        ):
            self.assertEqual(routes[role][field], expected)

    """Effort and flags decide, never the profile name."""

    def test_the_live_registry_has_both_kinds_and_they_are_classified(self) -> None:
        with REGISTRY.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        top = [r for r in rows if TRUE._is_top_tier(r)]
        rest = [r for r in rows if not TRUE._is_top_tier(r)]
        # Positive and negative control on real data: a rule that said "yes" to
        # everything, or "no" to everything, fails one of these.
        self.assertTrue(top, "no top-tier profile found in the live registry")
        self.assertTrue(rest, "every live profile classified as top tier")
        for row in top:
            self.assertTrue(
                row["effort"] == "max" or "ultra" in row["flags"], row["profile_id"]
            )
        self.assertIn("claude.opus55.max", [r["profile_id"] for r in top])
        self.assertIn("codex.sol.ultra", [r["profile_id"] for r in top])
        self.assertIn("claude.opus55.xhigh", [r["profile_id"] for r in rest])
        self.assertIn("gemini.argon.high", [r["profile_id"] for r in rest])

    def test_profiles_that_do_not_exist_yet_are_classified_by_their_columns(self) -> None:
        """The stated requirement: a later *.opus55.max / *.sol6.ultra must light
        up with no edit to the dashboard."""
        future_max = {"profile_id": "claude.opus55.max", "effort": "max", "flags": "none"}
        future_ultra = {"profile_id": "codex.sol6.ultra", "effort": "max", "flags": "ultra"}
        self.assertTrue(TRUE._is_top_tier(future_max))
        self.assertTrue(TRUE._is_top_tier(future_ultra))

    def test_a_name_that_merely_contains_max_is_not_top_tier(self) -> None:
        """Control against the tempting shortcut of matching the profile id."""
        decoy = {"profile_id": "claude.maxwell.high", "effort": "high", "flags": "ultrasonic"}
        self.assertFalse(TRUE._is_top_tier(decoy))

    def test_an_unresolved_profile_is_never_top_tier(self) -> None:
        for profile in (None, {}, {"effort": "", "flags": ""}):
            with self.subTest(profile=profile):
                self.assertFalse(TRUE._is_top_tier(profile))


class ProfileResolutionTest(unittest.TestCase):
    """The digest in a dispatch context must lead back to the exact profile row.

    The expected digest is computed with dispatch_context_builder's OWN helpers,
    so this fails if either side of the hashing changes.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "shared" / "registries").mkdir(parents=True)
        (self.vault / "_state" / "board-dispatch").mkdir(parents=True)
        (self.vault / "shared" / "registries" / "profiles.tsv").write_bytes(
            REGISTRY.read_bytes()
        )
        self.module = _load(VAULT_ROOT=str(self.vault), VS_DASH_COLOR="1")

    def _digest_for(self, profile_id, lane):
        profile = dcb._profile_row(self.vault, lane=lane, profile_id=profile_id)  # noqa: SLF001
        return dcb._sha256_bytes(  # noqa: SLF001
            dcb._canonical_json({"profile_id": profile_id, "profile": profile})  # noqa: SLF001
        )

    def _write_context(self, task_id, attempt, digest, mtime=None):
        path = (self.vault / "_state" / "board-dispatch"
                / f"{task_id}.{attempt}.context.json")
        path.write_text(json.dumps({
            "authority": {"lane": "claude", "selected_model_sha256": digest,
                          "task_id": task_id},
            "schema": "go-live-authority/v1",
            "task_prompt": "# a task\n" + "filler " * 4000,
        }, indent=2), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def _resolve(self, task_id):
        table = self.module._profile_by_bundle()
        digests = self.module._selected_model_digests([task_id])
        row = table.get(digests.get(task_id, ""))
        return row["profile_id"] if row else None

    def test_a_dispatched_profile_resolves_to_its_exact_registry_row(self) -> None:
        for profile_id, lane in (("claude.opus55.max", "claude"),
                                 ("claude.opus55.high", "claude"),
                                 ("codex.sol.ultra", "codex")):
            with self.subTest(profile_id=profile_id):
                task_id = f"TASK-2026-09-23-{profile_id.replace('.', '')}"
                self._write_context(task_id, "d-1", self._digest_for(profile_id, lane))
                self.assertEqual(self._resolve(task_id), profile_id)

    def test_same_lane_and_model_still_separates_the_effort_tiers(self) -> None:
        """claude.opus55.max and claude.opus55.high pin the same model. The model
        name can never tell them apart -- only the profile digest can."""
        self._write_context("TASK-A", "d-1", self._digest_for("claude.opus55.max", "claude"))
        self._write_context("TASK-B", "d-1", self._digest_for("claude.opus55.high", "claude"))
        table = self.module._profile_by_bundle()
        digests = self.module._selected_model_digests(["TASK-A", "TASK-B"])
        self.assertTrue(self.module._is_top_tier(table[digests["TASK-A"]]))
        self.assertFalse(self.module._is_top_tier(table[digests["TASK-B"]]))

    def test_an_unknown_or_missing_digest_resolves_to_nothing(self) -> None:
        """Control: the lookup is not vacuously matching whatever it is given."""
        self._write_context("TASK-UNKNOWN", "d-1", "f" * 64)
        self.assertIsNone(self._resolve("TASK-UNKNOWN"))
        self.assertIsNone(self._resolve("TASK-NEVER-DISPATCHED"))

    def test_the_newest_attempt_wins(self) -> None:
        self._write_context("TASK-RETRY", "d-old",
                            self._digest_for("claude.opus55.high", "claude"), mtime=1_000_000)
        self._write_context("TASK-RETRY", "d-new",
                            self._digest_for("claude.opus55.max", "claude"), mtime=2_000_000)
        self.assertEqual(self._resolve("TASK-RETRY"), "claude.opus55.max")

    def test_a_missing_registry_disables_the_cue_instead_of_crashing(self) -> None:
        (self.vault / "shared" / "registries" / "profiles.tsv").unlink()
        self.assertEqual(self.module._profile_by_bundle(), {})


class AnimationTest(unittest.TestCase):
    """A top-tier border must MOVE between frames; nothing else may."""

    def _border(self, rows):
        return [rows[0], rows[-1]] + [r[:12] for r in rows[1:-1]]

    def test_the_top_tier_border_colours_move_between_consecutive_frames(self) -> None:
        for module, label in ((TRUE, "truecolor"), (C256, "256-colour")):
            with self.subTest(mode=label):
                first = _card(module, MAX_PROFILE, phase=0)
                second = _card(module, MAX_PROFILE, phase=module.RAINBOW_SPEED)
                self.assertIn("\033[", first[0])
                self.assertNotEqual(self._border(first), self._border(second))
                # The same frame twice is identical: only `phase` moves anything.
                self.assertEqual(first, _card(module, MAX_PROFILE, phase=0))

    def test_the_text_and_the_layout_never_move_between_frames(self) -> None:
        """Only colour may change. If any glyph moved, the box would jitter."""
        first = _card(TRUE, MAX_PROFILE, phase=0)
        second = _card(TRUE, MAX_PROFILE, phase=7)
        self.assertEqual(_plainify(TRUE, first), _plainify(TRUE, second))
        self.assertNotEqual(first, second)  # ... and something DID change

    def test_the_ultra_flag_animates_the_same_as_a_max_effort(self) -> None:
        first = _card(TRUE, ULTRA_PROFILE, phase=0)
        second = _card(TRUE, ULTRA_PROFILE, phase=5)
        self.assertNotEqual(first[0], second[0])

    def test_a_normal_card_emits_none_of_the_new_codes_and_never_moves(self) -> None:
        for module, label in ((TRUE, "truecolor"), (C256, "256-colour")):
            with self.subTest(mode=label):
                rows = _card(module, NORMAL_PROFILE, phase=0)
                self.assertEqual(rows, _card(module, NORMAL_PROFILE, phase=9))
                self.assertNotIn("38;2;", "".join(rows))  # no truecolor anywhere
                self.assertNotIn("MAX", "".join(_plainify(module, rows)))
                # An unresolved profile renders exactly like a normal one.
                self.assertEqual(rows, _card(module, None, phase=0))

    def test_a_normal_card_is_byte_identical_to_the_pre_feature_rendering(self) -> None:
        """`_top_rule` with no label, and the un-animated border, must reproduce
        the old `╭───╮` / `│…│` / `╰───╯` frame exactly."""
        rows = _card(TRUE, NORMAL_PROFILE, phase=3)
        color = TRUE.LANE_COLOR["claude"]
        self.assertEqual(rows[0], TRUE._fg("╭" + "─" * 46 + "╮", color))
        self.assertEqual(rows[-1], TRUE._fg("╰" + "─" * 46 + "╯", color))
        self.assertTrue(rows[1].startswith(TRUE._fg("│", color)))

    def test_control_the_movement_check_fails_when_the_rainbow_is_removed(self) -> None:
        """Remove the cue and the test above must stop passing -- otherwise it was
        never measuring the rainbow."""
        original = TRUE._is_top_tier
        TRUE._is_top_tier = lambda profile: False  # the feature, deleted
        try:
            first = _card(TRUE, MAX_PROFILE, phase=0)
            second = _card(TRUE, MAX_PROFILE, phase=TRUE.RAINBOW_SPEED)
            with self.assertRaises(AssertionError):
                self.assertNotEqual(self._border(first), self._border(second))
            with self.assertRaises(AssertionError):
                self.assertIn("MAX", "".join(_plainify(TRUE, first)))
        finally:
            TRUE._is_top_tier = original

    def test_the_two_colour_depths_are_actually_different_renderings(self) -> None:
        """Control on the truecolor fallback: it is a real branch, not dead code."""
        self.assertIn("38;2;", "".join(_card(TRUE, MAX_PROFILE, phase=0)))
        self.assertNotIn("38;2;", "".join(_card(C256, MAX_PROFILE, phase=0)))
        self.assertIn("38;5;", "".join(_card(C256, MAX_PROFILE, phase=0)))


class WidthTest(unittest.TestCase):
    """The cue may never move the box."""

    def test_a_top_tier_card_is_exactly_as_wide_as_a_normal_one(self) -> None:
        for module in (TRUE, C256, PLAIN):
            for cw in (28, 33, 46):
                with self.subTest(colour=not module._NO_COLOR, cw=cw):
                    top = _card(module, MAX_PROFILE, phase=4, cw=cw)
                    normal = _card(module, NORMAL_PROFILE, phase=4, cw=cw)
                    self.assertEqual(len(top), module.CARD_H)
                    self.assertEqual(len(top), len(normal))
                    for index, (a, b) in enumerate(zip(top, normal)):
                        self.assertEqual(module._dwidth(a), cw + 2, f"row {index}")
                        self.assertEqual(module._dwidth(a), module._dwidth(b))

    def test_the_marker_uses_no_glyph_the_width_model_only_guesses_at(self) -> None:
        """The defect this caught: `✦` (U+2726) draws in ONE cell in a real
        monospace terminal, but _dwidth counts the whole 0x2600-0x27BF block as
        two -- so the top rule came out a column short of the bottom one while
        every width assertion still passed. Keep the marker out of that band."""
        for ch in TRUE.TOP_TIER_LABEL:
            self.assertEqual(TRUE._dwidth(ch), 1, f"{ch!r} is U+{ord(ch):04X}")
        self.assertEqual(TRUE._dwidth("\u2726"), 2)  # control: the band is real

    def test_the_label_is_dropped_rather_than_overflowing_a_tiny_card(self) -> None:
        self.assertEqual(TRUE._dwidth(TRUE._top_rule(4, TRUE.TOP_TIER_LABEL)), 4)
        self.assertEqual(TRUE._top_rule(4, TRUE.TOP_TIER_LABEL), "─" * 4)


class PlainOutputTest(unittest.TestCase):
    """NO_COLOR / TERM=dumb / a pipe: the marker carries the cue, alone."""

    def test_no_color_renders_the_marker_and_not_one_escape_code(self) -> None:
        rows = _card(PLAIN, MAX_PROFILE, phase=3)
        self.assertNotIn("\033", "".join(rows))
        self.assertIn("◆ MAX", rows[0])
        self.assertEqual(rows, _card(PLAIN, MAX_PROFILE, phase=99))  # nothing animates

    def test_the_marker_is_also_there_in_colour_mode(self) -> None:
        for module in (TRUE, C256):
            with self.subTest(module=module.__name__):
                self.assertIn("◆ MAX", "".join(_plainify(module, _card(module, MAX_PROFILE))))


class EndToEndPlainTest(unittest.TestCase):
    """The whole frame, as a real process, under each degraded destination."""

    SPAWN = "TASK-2026-09-23-0001-toptier"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "shared" / "registries").mkdir(parents=True)
        (self.vault / "shared" / "cards").mkdir(parents=True)
        (self.vault / "_state" / "board-dispatch").mkdir(parents=True)
        (self.vault / "shared" / "registries" / "profiles.tsv").write_bytes(
            REGISTRY.read_bytes()
        )
        profile = dcb._profile_row(self.vault, lane="claude", profile_id="claude.opus55.max")  # noqa: SLF001
        digest = dcb._sha256_bytes(  # noqa: SLF001
            dcb._canonical_json({"profile_id": "claude.opus55.max", "profile": profile})  # noqa: SLF001
        )
        (self.vault / "_state" / "board-dispatch" / f"{self.SPAWN}.d-1.context.json").write_text(
            json.dumps({"authority": {"selected_model_sha256": digest}}), encoding="utf-8"
        )

    def _render(self, **env):
        started = int(time.time()) - 60
        stream = (
            f"@SPAWN\t{self.SPAWN}\tclaude\tui-engineer\t{started}\t0\t/nonexistent\tclaude-opus-5\n"
            "@SUMMARY\ta top-tier task\n"
            "@SPAWN\tTASK-2026-09-23-0002-normal\tclaude\tbackend-engineer\t"
            f"{started}\t0\t/nonexistent\tclaude-opus-5\n"
            "@SUMMARY\tan ordinary task\n"
        )
        result = subprocess.run(
            [sys.executable, str(DASHBOARD), "--stdin"],
            input=stream, capture_output=True, text=True, timeout=60,
            env={**os.environ, "VAULT_ROOT": str(self.vault), "VS_DASH_WIDTH": "110",
                 "VS_DASH_HEIGHT": "30",
                 # never touch the running squad's /tmp state files
                 "VS_DASH_HITMAP": str(self.vault / "hitmap.tsv"),
                 "VS_SWARM_STATUS": str(self.vault / "swarm.status"),
                 "VS_DASH_HISTSTATE": str(self.vault / "hist.state"),
                 **env},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_pipe_no_color_and_term_dumb_all_emit_no_escape_codes(self) -> None:
        for label, env in (("pipe", {}),
                           ("NO_COLOR", {"NO_COLOR": "1"}),
                           ("TERM=dumb", {"TERM": "dumb"}),
                           ("VS_DASH_COLOR=0", {"VS_DASH_COLOR": "0"})):
            with self.subTest(destination=label):
                out = self._render(**env)
                self.assertNotIn("\033", out)
                self.assertIn("◆ MAX", out)          # the cue survives as text
                self.assertIn("ui-engineer", out)

    def test_control_the_escape_check_can_see_escape_codes(self) -> None:
        """Without this, "no escape codes" would pass on an empty frame."""
        out = self._render(VS_DASH_COLOR="1", VS_DASH_TRUECOLOR="1")
        self.assertIn("\033[", out)
        self.assertIn("38;2;", out)

    def test_only_the_top_tier_card_carries_the_marker(self) -> None:
        out = self._render()
        self.assertEqual(out.count("◆ MAX"), 1)


if __name__ == "__main__":
    unittest.main()
