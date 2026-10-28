#!/usr/bin/env python3
"""scripts/send-task.sh must be able to request a per-dispatch effort tier.

The context builder has always honoured a packet ``route_tier:`` through
``packet_route_tier()``, but the generating wrapper never emitted the field, so
the capability was unreachable from an ordinary Chrono dispatch. These tests pin
the three states that matter: absent (unchanged legacy selection), present (the
tier reaches frontmatter verbatim), and invalid (refused before any packet is
staged, so a bad tier leaves no dispatch residue).

The fixture stubs bin/send-task.sh to capture the staged packet, so nothing here
launches a model CLI or touches the real board.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "scripts" / "send-task.sh"
PYTHON_SCRIPTS = ROOT / "scripts" / "python"
if str(PYTHON_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PYTHON_SCRIPTS))

from dispatch_context_builder import (  # noqa: E402
    DispatchContextError,
    ROUTE_TIERS,
    packet_route_tier,
)


class RouteTierWrapperTests(unittest.TestCase):
    """The wrapper emits route_tier only on request, and only a valid one."""

    def _run(self, route_tier: str | None) -> tuple[subprocess.CompletedProcess, Path]:
        stack = tempfile.TemporaryDirectory()
        self.addCleanup(stack.cleanup)
        root = Path(stack.name)
        tools = root / "tools"
        tools.mkdir()
        (root / "shared").mkdir()
        (root / "bin").mkdir()
        body = root / "body.md"
        body.write_text("Exercise the generating wrapper.\n", encoding="utf-8")
        packet_capture = root / "packet-capture.md"
        (root / "shared/lead-windows.sh").write_text(
            "COMPATIBILITY_NAMESPACES=(coding security content sysmgmt research)\n"
            "MODEL_LANES=(gpt-codex claude gemini grok)\n"
            'is_compatibility_namespace() { [[ "$1" == coding ]]; }\n',
            encoding="utf-8",
        )
        runtime_fields = ["exodia", "shared", "judgment", "medium"]
        runtime_fields.extend(
            ["x", "x", "claude", "x", "x", "x", "x", "x", "x", "gpt-codex"]
        )
        (root / "shared/specialist-runtime-map.tsv").write_text(
            "\t".join(runtime_fields) + "\n", encoding="utf-8"
        )
        hardened = root / "bin/send-task.sh"
        hardened.write_text('#!/bin/bash\ncp "$1" "$PACKET_CAPTURE"\n', encoding="utf-8")
        hardened.chmod(0o755)
        uuidgen = tools / "uuidgen"
        uuidgen.write_text(
            "#!/bin/sh\nprintf '12345678-1234-1234-1234-123456789abc\\n'\n",
            encoding="utf-8",
        )
        uuidgen.chmod(0o755)
        mktemp = tools / "mktemp"
        mktemp.write_text(
            '#!/bin/sh\nprintf "staging\\n" >> "$STAGING_ATTEMPTED"\n'
            'exec /usr/bin/mktemp "$@"\n',
            encoding="utf-8",
        )
        mktemp.chmod(0o755)
        environment = {
            **os.environ,
            "PACKET_CAPTURE": str(packet_capture),
            "STAGING_ATTEMPTED": str(root / "staging-attempted"),
            "PATH": f"{tools}:/usr/bin:/bin",
            "REVIEWS": "none",
            "VAULT_ROOT": str(root),
        }
        environment.pop("ROUTE_TIER", None)
        if route_tier is not None:
            environment["ROUTE_TIER"] = route_tier
        completed = subprocess.run(
            [
                "bash",
                str(WRAPPER),
                "coding",
                str(body),
                "exodia",
                "claude",
                "--mode",
                "modeless",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return completed, packet_capture

    def test_absent_variable_emits_no_field_so_selection_is_unchanged(self) -> None:
        completed, packet = self._run(None)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("route_tier:", packet.read_text(encoding="utf-8"))
        self.assertTrue((packet.parent / "staging-attempted").exists())

    def test_each_declared_tier_reaches_packet_frontmatter(self) -> None:
        for tier in ROUTE_TIERS:
            with self.subTest(tier=tier):
                completed, packet = self._run(tier)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                text = packet.read_text(encoding="utf-8")
                self.assertIn(f"\nroute_tier: {tier}\n", text)
                # The field lands in frontmatter, not the body the operator wrote.
                frontmatter = text.split("\n---\n", 1)[0]
                self.assertIn(f"route_tier: {tier}", frontmatter)
                self.assertTrue((packet.parent / "staging-attempted").exists())

    def test_invalid_tier_is_refused_before_any_packet_is_staged(self) -> None:
        completed, packet = self._run("ultra")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        self.assertIn("invalid ROUTE_TIER", completed.stderr)
        self.assertFalse(packet.exists())
        self.assertFalse((packet.parent / "staging-attempted").exists())

    def test_empty_variable_is_refused_with_builder_enum_error_before_staging(self) -> None:
        completed, packet = self._run("")
        self.assertEqual(completed.returncode, 1, completed.stdout)
        with self.assertRaises(DispatchContextError) as builder_error:
            packet_route_tier({"route_tier": ""})
        self.assertEqual(
            completed.stderr,
            f"ERROR: invalid ROUTE_TIER ''; {builder_error.exception}\n",
        )
        self.assertFalse(packet.exists())
        self.assertFalse((packet.parent / "staging-attempted").exists())


if __name__ == "__main__":
    unittest.main()
