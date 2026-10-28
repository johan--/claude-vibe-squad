"""Pin the public-clone registry degradation on validate_capabilities.py.

Blocker 6 of the V4 acceptance gate was a raw ``FileNotFoundError`` traceback:
a contributor in a public clone who installs the documented ``.githooks``
pre-commit hook stages a capability file, the hook runs
``bin/validate-capabilities.sh``, and the validator dies reading the shared
skill-tool registry -- which the export policy (``tools/export/policy/
path-policy.json``) deliberately withholds from the public tree.

The fix for that blocker was first applied to the SIBLING validator
(``validate_capability_homes.py``, commit ``fe5c76b1``) and only later to this
one (commit ``1a224975``).  Nothing asserted the difference, so a fix on one
file was indistinguishable from a fix on both.  That is what this module
exists to prevent: it pins the degradation on THIS file, by name, and pins the
two validators to a single shared classification of the same tree.

Three properties are load-bearing, and the second is the one most easily lost:

1. A withheld registry degrades to a typed, non-fatal result (exit 0).
2. A registry this tree TRACKS but is missing still fails closed (exit 1).
   Broadening the handler past the withheld case would turn a real breakage
   into a green run with no baseline at all -- a validator reporting pass
   while validating nothing.
3. The maintainer run is untouched: it still discovers and validates cards.
   A degradation that also degrades the real check is worse than the bug.
"""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "scripts/python/validate_capabilities.py"
SPEC = importlib.util.spec_from_file_location("validate_capabilities", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
validate_capabilities = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validate_capabilities
SPEC.loader.exec_module(validate_capabilities)

HOMES_PATH = REPO_ROOT / "scripts/python/validate_capability_homes.py"
# Spelled out rather than imported from the module under test: a validator that
# has lost the guard entirely must still be able to LOAD this file, so the loss
# is reported as a named failing assertion instead of a collection-time
# AttributeError that hides every other check in here.
REGISTRY_RELATIVE = Path("shared/registries/skill-tool-registry.tsv")
MAINTAINER_REGISTRY = REPO_ROOT / REGISTRY_RELATIVE


def _git(root: Path, *args: str) -> None:
    """Run one git command against a hermetic throwaway repository."""
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "registry guard test",
        "GIT_AUTHOR_EMAIL": "registry-guard@example.invalid",
        "GIT_COMMITTER_NAME": "registry guard test",
        "GIT_COMMITTER_EMAIL": "registry-guard@example.invalid",
    }
    subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, env=environment
    )


def _write_registry(root: Path) -> Path:
    path = root / REGISTRY_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "record_kind\tname\tlanes\ttype\tverified_state\tcost_tier\n", encoding="utf-8"
    )
    return path


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("public tree\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "public tree")


def _run_validator(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the validator exactly as bin/validate-capabilities.sh does."""
    return subprocess.run(
        [sys.executable, str(MODULE_PATH), "--root", str(root), *args],
        capture_output=True,
        text=True,
    )


def _summary(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


class RegistryPublicationStateTests(unittest.TestCase):
    """The classifier must separate 'withheld' from 'broken' before degrading."""

    def test_this_validator_carries_the_guard(self) -> None:
        """The blocker-6 assertion by name: the guard lives on THIS file."""
        self.assertTrue(
            hasattr(validate_capabilities, "registry_publication_state"),
            "validate_capabilities.py has no registry_publication_state guard",
        )
        self.assertTrue(
            hasattr(validate_capabilities, "emit_registry_configuration"),
            "validate_capabilities.py has no emit_registry_configuration guard",
        )
        self.assertEqual(validate_capabilities.REGISTRY_RELATIVE, REGISTRY_RELATIVE)

    def test_published_when_the_registry_file_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_registry(root)
            self.assertEqual(
                validate_capabilities.registry_publication_state(root), "published"
            )

    def test_not_published_when_untracked_in_a_git_clone(self) -> None:
        """The public repo does not track the registry at all."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            self.assertEqual(
                validate_capabilities.registry_publication_state(root), "not-published"
            )

    def test_not_published_when_the_tree_has_no_git_metadata(self) -> None:
        """An extracted public archive carries no .git, and must not be 'unknown'."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared").mkdir()
            self.assertFalse((root / ".git").exists())
            self.assertEqual(
                validate_capabilities.registry_publication_state(root), "not-published"
            )

    def test_missing_when_tracked_but_absent_from_the_worktree(self) -> None:
        """A tree that TRACKS the registry has a real failure when it is gone.

        Built with sparse-checkout rather than a delete, so the file is absent
        from the worktree for the same reason a partial checkout makes it
        absent -- while HEAD still tracks it.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_registry(root)
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "maintainer tree tracks the registry")
            _git(root, "sparse-checkout", "init", "--no-cone")
            _git(root, "sparse-checkout", "set", "/*", "!/shared/registries/")

            self.assertFalse((root / REGISTRY_RELATIVE).is_file())
            self.assertEqual(
                validate_capabilities.registry_publication_state(root), "missing"
            )


class EmitRegistryConfigurationTests(unittest.TestCase):
    """Each classification maps to one exit code and one typed payload."""

    def _emit(self, state: str) -> tuple[int, dict, dict]:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = validate_capabilities.emit_registry_configuration(state)
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, buffer.getvalue())
        return code, json.loads(lines[0]), json.loads(lines[1])

    def test_not_published_is_typed_non_fatal_and_not_applicable(self) -> None:
        code, degradation, summary = self._emit("not-published")
        self.assertEqual(code, 0)
        self.assertEqual(degradation["type"], "registry-degradation")
        self.assertEqual(degradation["code"], "registry-not-published")
        self.assertEqual(degradation["status"], "not-applicable")
        self.assertEqual(degradation["file"], REGISTRY_RELATIVE.as_posix())
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["not_applicable"], 1)
        self.assertEqual(summary["failed"], 0)
        # Zero files validated must stay visible, so a skipped run can never be
        # mistaken for a run that checked something.
        self.assertEqual(summary["files"], 0)
        self.assertEqual(summary["passed"], 0)

    def test_missing_registry_still_fails_closed(self) -> None:
        """The anti-swallow assertion: a tracked-but-absent registry is an error."""
        code, degradation, summary = self._emit("missing")
        self.assertEqual(code, 1)
        self.assertEqual(degradation["code"], "missing-registry")
        self.assertEqual(degradation["status"], "fail")
        self.assertEqual(summary["status"], "fail")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["not_applicable"], 0)

    def test_unknown_publication_is_could_not_run_not_pass(self) -> None:
        code, degradation, summary = self._emit("unknown")
        self.assertEqual(code, 2)
        self.assertEqual(degradation["code"], "registry-publication-undetermined")
        self.assertEqual(degradation["status"], "could-not-run")
        self.assertEqual(summary["status"], "could-not-run")
        self.assertEqual(summary["could_not_run"], 1)


class PublicCloneEntryPointTests(unittest.TestCase):
    """The blocker itself: the documented command, end to end, no traceback."""

    def _assert_typed_degradation(self, result: subprocess.CompletedProcess[str]) -> None:
        combined = result.stdout + result.stderr
        self.assertNotIn("Traceback", combined, combined)
        self.assertNotIn("FileNotFoundError", combined, combined)
        self.assertEqual(result.returncode, 0, combined)
        first = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(first["code"], "registry-not-published")
        self.assertEqual(_summary(result.stdout)["status"], "pass")

    def test_public_clone_run_degrades_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            self._assert_typed_degradation(_run_validator(root))

    def test_public_clone_self_test_degrades_without_a_traceback(self) -> None:
        """The pre-commit hook runs the validator twice; --self-test is the second."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            self._assert_typed_degradation(_run_validator(root, "--self-test"))

    def test_extracted_archive_run_degrades_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "shared").mkdir()
            self._assert_typed_degradation(_run_validator(root))

    def test_tracked_but_missing_registry_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_registry(root)
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "maintainer tree tracks the registry")
            _git(root, "sparse-checkout", "init", "--no-cone")
            _git(root, "sparse-checkout", "set", "/*", "!/shared/registries/")

            result = _run_validator(root)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            self.assertEqual(
                json.loads(result.stdout.splitlines()[0])["code"], "missing-registry"
            )


@unittest.skipUnless(
    MAINTAINER_REGISTRY.is_file(),
    "maintainer-only: this tree does not carry the shared skill-tool registry",
)
class MaintainerRunUnaffectedTests(unittest.TestCase):
    """The degradation must not degrade the real check on a maintainer tree."""

    def test_grok_card_tuples_stay_unsupported_until_probed(self) -> None:
        validator = validate_capabilities.Validator(REPO_ROOT)
        card = validate_capabilities.policy_fixture(
            "grok-tuple", "live", "`arxiv_search` (grok · yes · subscription)")
        codes = {e["code"] for e in validator.validate_text(card, "<grok>", None)["errors"]}
        self.assertIn("tool-lane-invalid", codes)

    def test_retired_lane_tuples_are_rejected(self) -> None:
        validator = validate_capabilities.Validator(REPO_ROOT)
        for registered in ({"all"}, {"codex", "kimi"}):
            with self.subTest(registered=registered):
                validator.tools["retirement-control"] = [{
                    "lanes": "|".join(sorted(registered)),
                    "verified_state": "yes", "cost_tier": "subscription",
                }]
                control = validate_capabilities.policy_fixture(
                    "lane-control", "live",
                    "`retirement-control` (codex · yes · subscription)")
                self.assertEqual(validator.validate_text(control, "<control>", None)["errors"], [])
                retired = control.replace("(codex ·", "(kimi ·")
                result = validator.validate_text(retired, "<retired>", None)
                self.assertEqual(result["status"], "fail")
                self.assertIn("tool-lane-invalid", {e["code"] for e in result["errors"]})
        self.assertFalse(validate_capabilities.lane_supported("kimi", {"all"}))

    def test_live_cards_do_not_cite_retired_lane(self) -> None:
        for path in sorted((REPO_ROOT / "shared/capabilities").rglob("*.md")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for match in validate_capabilities.TOOL_TUPLE_RE.finditer(line):
                    self.assertNotEqual(match.group("lane").strip(), "kimi",
                                        f"{path}:{number} cites a retired lane")

    def test_maintainer_run_still_validates_every_card(self) -> None:
        result = _run_validator(REPO_ROOT)
        combined = result.stdout + result.stderr
        self.assertNotIn("Traceback", combined, combined)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        cards = [row for row in rows if row["type"] == "capability"]
        self.assertEqual(
            {row["file"] for row in cards},
            {path.relative_to(REPO_ROOT).as_posix() for path in validate_capabilities.discover(REPO_ROOT)},
        )
        self.assertGreater(len(cards), 0)
        for row in [*cards, next(row for row in rows if row["type"] == "catalog-registry")]:
            self.assertEqual(row["status"], "pass", row)
            self.assertEqual(row["errors"], [], row)

        # The published mirror may contain stale demands even when every private
        # card passes. Require every such demand to appear in the default run;
        # a green result that ignored the mirror must fail this test.
        retired = validate_capabilities.Validator(REPO_ROOT).retired_only_skills()
        expected = {
            (ref.name, path.relative_to(REPO_ROOT).as_posix(), ref.line)
            for path in (REPO_ROOT / "shared/capabilities/public").rglob("*.md")
            for ref in validate_capabilities.retired_skill_references(path.read_text(encoding="utf-8"), retired)
            if ref.kind == "demand"
        }
        demand = next(row for row in rows if row["type"] == "skill-demand")
        for error in demand["errors"]:
            self.assertEqual(error["code"], "skill-retired", error)
        actual = {
            (error["name"], ref["file"], ref["line"])
            for error in demand["errors"] for ref in error["references"]
        }
        self.assertEqual(actual, expected)
        self.assertEqual(demand["demand_count"], len(expected))
        self.assertEqual(demand["status"], "fail" if expected else "pass")
        self.assertEqual(result.returncode, 1 if expected else 0, combined)
        summary = _summary(result.stdout)
        self.assertEqual(summary["status"], "fail" if expected else "pass")
        # The failure this guards against is a "pass" over an empty file set.
        self.assertEqual(summary["files"], len(cards) + 2)
        self.assertEqual(summary["failed"], int(bool(expected)))
        self.assertEqual(summary["passed"], summary["files"] - summary["failed"])
        self.assertNotIn("registry-not-published", result.stdout)

    def test_maintainer_self_test_still_passes(self) -> None:
        result = _run_validator(REPO_ROOT, "--self-test")
        combined = result.stdout + result.stderr
        self.assertNotIn("Traceback", combined, combined)
        self.assertEqual(result.returncode, 0, combined)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["status"], "pass")

    def test_maintainer_tree_classifies_as_published(self) -> None:
        self.assertEqual(
            validate_capabilities.registry_publication_state(REPO_ROOT), "published"
        )

    def test_self_test_passes_after_retired_arsenal_tools_and_lane_are_removed(self) -> None:
        retired = {"xai_search", "perplexity_search", "firecrawl_scrape",
                   "firecrawl_crawl", "firecrawl_parse"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("shared/capabilities", "shared/skills"):
                shutil.copytree(REPO_ROOT / relative, root / relative)
            shutil.copy2(REPO_ROOT / "shared/specialist-runtime-map.tsv", root / "shared")
            with MAINTAINER_REGISTRY.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle, delimiter="\t")
                fields = reader.fieldnames
                rows = [row for row in reader if row["name"] not in retired]
            for row in rows:
                relative = Path(row["path_or_source"])
                if (row["record_kind"] == "skill" and row["type"] == "invokable"
                        and not relative.is_absolute() and (REPO_ROOT / relative).is_file()):
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(REPO_ROOT / relative, target)
            registry = root / REGISTRY_RELATIVE
            registry.parent.mkdir(parents=True)
            with registry.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
                writer.writeheader()
                writer.writerows(rows)
            for path in (root / "shared/capabilities").rglob("*.md"):
                lines = []
                for line in path.read_text(encoding="utf-8").splitlines():
                    cells = line.split("|")
                    if len(cells) == 7 and validate_capabilities.STEP_RE.match(cells[1].strip()):
                        matches = list(validate_capabilities.TOOL_TUPLE_RE.finditer(cells[3]))
                        if matches:
                            kept = [match.group(0) for match in matches
                                    if match.group("name").strip() not in retired]
                            cells[3] = " " + (", ".join(kept) or "—") + " "
                            line = "|".join(cells)
                    lines.append(line)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            validator = validate_capabilities.Validator(root)
            self.assertTrue(retired.isdisjoint(validator.tools))
            self.assertIn("arxiv_search", validator.tools)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = validate_capabilities.self_test(validator)
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0, result)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["fixture_preconditions"], "pass")
            # Stale demand must still fail; tolerance never makes a missing tool usable.
            for name in retired:
                card = validate_capabilities.policy_fixture(
                    "stale-demand", "live", f"`{name}` (claude · yes · metered)",
                    cost_note="metered with budget ceiling",
                )
                finding = validator.validate_text(card, "<retired-demand>", None)
                self.assertIn("tool-catalog-claim", {error["code"] for error in finding["errors"]})


class SiblingValidatorParityTests(unittest.TestCase):
    """Pin the two validators to one shared answer, so they cannot drift apart.

    Blocker 6 was a fix that landed on one of these files and not the other.
    The guards are deliberately not identical in shape -- this validator is
    registry-backed end to end, so it degrades wholesale and must distinguish
    'withheld' from 'tracked but broken'; the capability-home validator only
    skips its registry-backed subset, so a boolean is enough there.  What must
    match is the vocabulary and the verdict on the same tree.
    """

    def setUp(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "validate_capability_homes_parity", HOMES_PATH
        )
        assert spec is not None and spec.loader is not None
        self.homes = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.homes
        spec.loader.exec_module(self.homes)

    def test_both_validators_resolve_the_same_registry_path(self) -> None:
        self.assertEqual(self.homes.REGISTRY_RELATIVE, REGISTRY_RELATIVE)

    def test_both_validators_agree_a_public_clone_lacks_the_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            self.assertEqual(
                validate_capabilities.registry_publication_state(root), "not-published"
            )
            self.assertFalse(self.homes.registry_published(root))

    def test_both_validators_agree_a_registry_bearing_tree_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_registry(root)
            self.assertEqual(
                validate_capabilities.registry_publication_state(root), "published"
            )
            self.assertTrue(self.homes.registry_published(root))

    def test_both_validators_use_the_same_typed_code(self) -> None:
        """`registry-not-published` is the shared name for this condition."""
        self.assertIn(
            "registry-not-published", HOMES_PATH.read_text(encoding="utf-8")
        )
        self.assertIn(
            "registry-not-published", MODULE_PATH.read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
