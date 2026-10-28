#!/usr/bin/env python3
"""Focused tests for the retired-trio validation contract.

RETIRED 2026-10-27 (TASK-2026-10-27-0124-a8f2b472). The validator now asserts the
guarded trio is gone, not present. These tests pin the retired-state helper and confirm
``main()`` returns pass (exit 0) against the real retired tree.
"""

from __future__ import annotations

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("validate_staged.py")
SPEC = importlib.util.spec_from_file_location("validate_staged", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RetiredTrioTests(unittest.TestCase):
    def test_absent_trio_is_clean(self) -> None:
        servers = {"playwright": {}, "chrome-devtools": {}}
        self.assertEqual(MODULE.retired_issues("live", servers), [])

    def test_empty_config_is_clean(self) -> None:
        self.assertEqual(MODULE.retired_issues("staged", {}), [])

    def test_reappearing_trio_member_is_flagged(self) -> None:
        servers = {"guarded-semgrep": {}, "playwright": {}}
        issues = MODULE.retired_issues("live", servers)
        self.assertEqual(issues, ["live:retired-server-present:guarded-semgrep"])

    def test_all_three_members_flagged(self) -> None:
        servers = {name: {} for name in MODULE.RETIRED}
        issues = MODULE.retired_issues("staged", servers)
        self.assertEqual(len(issues), 3)
        for name in MODULE.RETIRED:
            self.assertIn(f"staged:retired-server-present:{name}", issues)

    def test_all_private_inputs_absent_reports_skip(self) -> None:
        output = io.StringIO()
        with mock.patch.object(Path, "is_file", return_value=False), contextlib.redirect_stdout(output):
            self.assertEqual(MODULE.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "skip")

    def test_partial_private_inputs_are_a_failure(self) -> None:
        output = io.StringIO()
        # Exactly one missing input must not turn the whole validator off.
        with mock.patch.object(Path, "is_file", side_effect=[False] + [True] * 6), contextlib.redirect_stdout(output):
            self.assertEqual(MODULE.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "fail")

    @unittest.skipUnless(MODULE.CLAUDE_LIVE.is_file(), "private lane configs are not published")
    def test_main_passes_on_live_retired_tree(self) -> None:
        # Positive control: against the real retired tree the validator must pass (exit 0).
        self.assertEqual(MODULE.main(), 0)


if __name__ == "__main__":
    unittest.main()
