from __future__ import annotations

import ast
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
PYTHON_DIR = ROOT / "scripts" / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

import dispatch_context_builder as dcb  # noqa: E402
import seatbelt_profile as seatbelt  # noqa: E402


def board_supervisor_launch_program() -> str:
    """Return the Python program bin/board-supervisor.sh execs for a lane launch.

    It is embedded as one quoted heredoc, so a test can parse the real program
    instead of pattern-matching the shell text wrapped around it.
    """
    lines = (ROOT / "bin" / "board-supervisor.sh").read_text(
        encoding="utf-8"
    ).splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if "exec" in line and line.rstrip().endswith("<<'PYEOF'")
    )
    end = next(
        index for index in range(start + 1, len(lines)) if lines[index] == "PYEOF"
    )
    return "\n".join(lines[start + 1 : end])


def called_and_defined_names(program: str) -> tuple[set[str], set[str]]:
    """Names actually invoked, and names defined, in a parsed program.

    A name is "called" only when it heads an ast.Call. A `def` line, a comment,
    a docstring and a string literal all fail to qualify, which is the whole
    point: those are what made counting source-text occurrences vacuous.
    """
    tree = ast.parse(program)
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else getattr(function, "attr", None)
        )
        if name:
            called.add(name)
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return called, defined


class AgyLaneRepointTests(unittest.TestCase):
    def test_gemini_lane_points_to_exact_agy_native_entrypoint(self) -> None:
        # Load the declared defaults in an isolated namespace.  The hermetic CI
        # probe deliberately replaces the already-imported module's mapping to
        # prove tests do not depend on installed CLIs; that runtime fixture must
        # not hide a regression in the source-of-truth default itself.
        declared = runpy.run_path(str(PYTHON_DIR / "seatbelt_profile.py"))
        self.assertEqual(
            declared["LANE_CLI_PATHS"]["gemini"],
            declared["LOCAL_BIN_ROOT"] / "agy",
        )

    def test_regular_native_entrypoint_needs_no_alias_or_shebang_grant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "agy"
            executable.write_bytes(b"native-fixture\x00")
            executable.chmod(0o700)
            with mock.patch.dict(
                seatbelt.LANE_CLI_PATHS,
                {"gemini": executable},
                clear=True,
            ):
                paths = seatbelt.installed_lane_cli_executable_paths(
                    lanes=("gemini",),
                    include_offline=False,
                )
                normalized = tuple(
                    seatbelt.normalize_realpath(path) for path in paths
                )
                aliases = seatbelt.installed_lane_cli_executable_aliases(
                    normalized,
                    lanes=("gemini",),
                )

            self.assertEqual(paths, (executable.resolve(),))
            self.assertEqual(aliases, ())
            self.assertIsNone(seatbelt._shebang_interpreter(executable))

    def test_board_launcher_uses_only_supported_agy_flags(self) -> None:
        source = (ROOT / "bin" / "board-supervisor.sh").read_text(
            encoding="utf-8"
        )
        launcher = source.split("    def gemini_ordered_launcher(", 1)[1].split(
            "\n    def grok_role_launcher(", 1
        )[0]
        for retired in (
            "--allowed-mcp-server-names",
            "--allowed-tools",
            "--approval-mode",
            "--include-directories",
            "--skip-trust",
        ):
            self.assertNotIn(retired, launcher)
        self.assertIn('"--add-dir"', launcher)
        self.assertIn('"--output-format",\n            "text"', launcher)
        self.assertIn('"--print",\n            concise_prompt', launcher)
        self.assertIn("agent_system_context.rstrip()", launcher)
        self.assertIn(dcb.AGY_EXTERNAL_MCP_MAX_CALLS_FIELD, launcher)

    def test_board_bounds_agy_print_mode_by_the_board_wall(self) -> None:
        """agy's print mode must not impose its own deadline on a lane.

        Measured 2026-09-14: four gemini dispatches over three days returned a
        truncated stub with returncode 0 and the line "[agy] print timeout after
        5m0s with turn in progress; returning partial output". `agy --help`
        documents `--print-timeout` with a 5m0s default, and a probe with an
        explicit `--print-timeout 20s` reproduced the same message carrying the
        supplied value and still exited 0 -- so the flag is the source of the
        deadline, and its absence silently capped every gemini lane at five
        minutes while every other lane ran to the board's wall.

        Exit 0 is why it went unnoticed for three days: the board cannot tell a
        truncated turn from a finished one, so it promoted the stub.
        """
        source = (ROOT / "bin" / "board-supervisor.sh").read_text(
            encoding="utf-8"
        )
        launcher = source.split("    def gemini_ordered_launcher(", 1)[1].split(
            "\n    def grok_role_launcher(", 1
        )[0]
        self.assertIn('"--print-timeout"', launcher)
        # Derived from the board's own wall, never a second hardcoded number.
        # Two independently written deadlines is how this drifts back.
        self.assertIn("launch_timeout", launcher)

    def test_board_fails_loudly_on_an_agy_truncated_turn(self) -> None:
        """A truncated agy turn exits 0; the board must not promote it.

        The deadline fix stops agy's timer firing first, but nothing about
        returncode 0 tells a half-finished turn from a finished one. Measured:
        `agy --print-timeout 20s` on a long prompt exits 0 with partial output
        and this marker; the positive control -- a prompt that completes --
        emits the marker zero times, so it discriminates.
        """
        source = (ROOT / "bin" / "board-supervisor.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('transcript_line.startswith("[agy] print timeout after")', source)
        # Scoped to the lane and anchored at line start: a worker that QUOTES
        # the marker in an artifact must not be read as having truncated.
        detector = source.split(
            "    # A truncated agy turn exits ZERO.", 1
        )[1].split("observed_memory_ids = set()", 1)[0]
        self.assertIn('lane == "gemini"', detector)
        self.assertIn('failure_class="timeout"', detector)
        # Per-line and anchored, not a substring search over the whole blob:
        # that is what keeps a quoted marker from reading as a truncation.
        self.assertIn("splitlines()", detector)
        self.assertIn(".startswith(", detector)

    def test_board_does_not_override_agy_login_with_gemini_api_key(self) -> None:
        source = (ROOT / "bin" / "board-supervisor.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("load_gemini_api_key", source)

    def test_agy_launch_path_never_calls_the_gemini_acknowledgment_helper(self) -> None:
        """The launch path must not mutate Gemini CLI acknowledgment state.

        Asserted against the parsed program, not the file's text. The previous
        form counted occurrences of "acknowledge_gemini_agents(" and required
        exactly 1 -- which the `def` line alone satisfies. Measured against two
        mutants of bin/board-supervisor.sh on 2026-09-01:

          - helper deleted (a later phase intends exactly this): occurrences
            drop to 0, so the COUNT assertion FAILS -- on the change that makes
            its own stated property more true than ever. This form passes: no
            definition and no call means nothing mutates that state.
          - helper deleted AND a real call added, leaving occurrences at 1: the
            COUNT assertion PASSES, blessing a launch path that does mutate
            acknowledgment state and would raise NameError. This form fails.

        Mutation caught: re-introducing a call into the launch path. Deleting
        the helper is deliberately NOT a failure, so this test does not have to
        be revisited when that happens.

        If the heredoc goes away, the extraction below raises StopIteration and
        this test moves with it. The assertion to preserve in that case is the
        property, not the mechanism: the agy launch path must not write
        $HOME/.gemini/acknowledgments/agents.json.
        """
        called, defined = called_and_defined_names(board_supervisor_launch_program())
        # Guard the extraction itself. If the heredoc layout changes and the
        # program comes back empty, the assertion below would pass trivially --
        # which is the exact failure mode this rewrite exists to end. Keyed on
        # the count rather than one function name so that deleting any single
        # helper does not trip it.
        self.assertGreater(
            len(defined), 20, "launch program extraction returned no real code"
        )
        # Matched on the prefix, not the exact name, so a renamed variant of the
        # same helper is caught too -- and so a failure prints two names rather
        # than the ~250-name call set.
        self.assertEqual(
            sorted(name for name in called if name.startswith("acknowledge")),
            [],
            "the agy launch path mutates Gemini CLI acknowledgment state again",
        )
        self.assertNotIn(
            "acknowledge_gemini_agents",
            defined,
            "the agy cutover left its uncallable Gemini-CLI compatibility helper behind",
        )

    def test_squad_launcher_has_no_retired_gemini_acknowledgment_helper(self) -> None:
        source = (ROOT / "bin" / "launch-squad.sh").read_text(encoding="utf-8")
        self.assertNotIn("acknowledge_gemini_agents", source)

    def test_gemini_lead_describes_agy_global_mcp_authority(self) -> None:
        source = (ROOT / "model-lanes" / "gemini" / "GEMINI.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "board uses to enumerate your authorized MCP servers",
            source,
        )
        normalized = " ".join(source.split())
        self.assertIn("persistent host MCP configuration globally", normalized)
        self.assertIn("sealed task capability plan", normalized)

    def test_agy_mode_docs_do_not_invent_a_board_flag(self) -> None:
        board = (ROOT / "bin" / "board-supervisor.sh").read_text(encoding="utf-8")
        launcher = board.split("    def gemini_ordered_launcher(", 1)[1].split(
            "\n    def grok_role_launcher(", 1
        )[0]
        self.assertNotIn('"--mode"', launcher)
        for relative in (
            "shared/api-catalog.md",
            "shared/registries/skill-tool-registry.tsv",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertIn(
                    "board dispatch does not pass this flag",
                    source.lower(),
                )

    def test_bootstrap_registers_all_four_chrono_mcps_with_agy(self) -> None:
        source = (ROOT / "scripts" / "bootstrap-mcps.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("AGY_LIST", source)
        self.assertIn("register_agy()", source)
        self.assertIn('local agy_args=("mcp" "add")', source)
        self.assertIn('agy "${agy_args[@]}"', source)
        self.assertIn('register_agy "${name}" "${args_str}" "${env_vars}"', source)
        self.assertIn("AGY_REGISTRATION_FAILED=1", source)
        for name in (
            "chrono-vault",
            "chrono-obsidian",
            "chrono-research-arsenal",
            "chrono-media-studio",
        ):
            self.assertIn(f'"{name}|', source)


if __name__ == "__main__":
    unittest.main()
