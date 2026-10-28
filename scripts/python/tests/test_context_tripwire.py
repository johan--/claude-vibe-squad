#!/usr/bin/env python3
"""Coverage for bin/context-tripwire.sh, the UserPromptSubmit context-size hook."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "bin" / "context-tripwire.sh"
SETTINGS = ROOT / "chrono" / ".claude" / "settings.json"
# Opt-in: point this at any real Claude Code transcript to run the size/timing test
# against production record shapes. Never hardcode a path here; a transcript path names
# the machine user, the checkout and a session id, none of which belong in the repo.
REAL_TRANSCRIPT = Path(os.environ.get("CHRONO_TRIPWIRE_REAL_TRANSCRIPT", "/nonexistent"))


def assistant_record(cache_read: int, cache_creation: int, inp: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": inp,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": 4,
                },
            },
        }
    )


def user_record(text: str = "hi") -> str:
    return json.dumps(
        {"type": "user", "uuid": "u1", "message": {"role": "user", "content": text}}
    )


class ContextTripwireTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="context-tripwire-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def write_transcript(self, total: int, name: str = "t.jsonl") -> Path:
        # Split the total across the three fields so the test proves summation.
        cache_read = total - 1100
        lines = [user_record(), assistant_record(cache_read, 1000, 100), user_record("next")]
        path = self.tmp / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def run_hook(self, stdin: str, env: dict | None = None) -> tuple[int, str, str]:
        merged = {k: v for k, v in os.environ.items() if not k.startswith("CHRONO_CONTEXT_")}
        if env:
            merged.update(env)
        proc = subprocess.run(
            ["bash", str(HOOK)],
            input=stdin,
            capture_output=True,
            text=True,
            env=merged,
            timeout=10,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def hook_payload(self, path: Path) -> str:
        return json.dumps(
            {
                "session_id": "s",
                "transcript_path": str(path),
                "cwd": str(ROOT / "chrono"),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "hello",
            }
        )

    def parse(self, stdout: str) -> str:
        data = json.loads(stdout)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        return data["hookSpecificOutput"]["additionalContext"]

    def test_below_warn_is_silent(self) -> None:
        rc, out, _ = self.run_hook(self.hook_payload(self.write_transcript(100_000)))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_warn_band_prints_warn_text(self) -> None:
        rc, out, _ = self.run_hook(self.hook_payload(self.write_transcript(650_000)))
        self.assertEqual(rc, 0)
        text = self.parse(out)
        self.assertIn("650000 tokens", text)
        self.assertIn("WARN", text)
        self.assertNotIn("HARD", text)
        self.assertIn("Finish only the current small step", text)
        self.assertIn("Dispatch nothing new", text)
        self.assertIn("OPEN-WORK.md", text)
        self.assertIn("/clear", text)
        self.assertNotIn("THIS turn", text)

    def test_hard_band_prints_hard_text(self) -> None:
        rc, out, _ = self.run_hook(self.hook_payload(self.write_transcript(800_000)))
        self.assertEqual(rc, 0)
        text = self.parse(out)
        self.assertIn("800000 tokens", text)
        self.assertIn("HARD", text)
        self.assertIn("THIS turn before anything else", text)
        self.assertIn("OPEN-WORK.md", text)
        self.assertIn("/clear", text)

    def test_synthetic_zero_usage_tail_record_is_skipped(self) -> None:
        # After an API error Claude Code appends an assistant record with model
        # "<synthetic>" and all-zero usage. The hook must keep walking back to the
        # last real measurement instead of going silent.
        synthetic = json.dumps(
            {
                "type": "assistant",
                "uuid": "a2",
                "message": {
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [{"type": "text", "text": "API Error: 529 Overloaded"}],
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "output_tokens": 0,
                    },
                },
            }
        )
        path = self.tmp / "synthetic.jsonl"
        lines = [user_record(), assistant_record(800_000, 0, 0), synthetic, user_record("retry")]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rc, out, _ = self.run_hook(self.hook_payload(path))
        self.assertEqual(rc, 0)
        text = self.parse(out)
        self.assertIn("HARD", text)
        self.assertIn("800000 tokens", text)
        # A tail record with zero usage but no synthetic marker is skipped the same way.
        lines = [assistant_record(800_000, 0, 0), assistant_record(0, 0, 0), user_record()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rc, out, _ = self.run_hook(self.hook_payload(path))
        self.assertEqual(rc, 0)
        self.assertIn("800000 tokens", self.parse(out))

    def test_env_thresholds_override_defaults(self) -> None:
        payload = self.hook_payload(self.write_transcript(100_000))
        rc, out, _ = self.run_hook(
            payload, {"CHRONO_CONTEXT_WARN": "50000", "CHRONO_CONTEXT_HARD": "90000"}
        )
        self.assertEqual(rc, 0)
        self.assertIn("HARD", self.parse(out))
        rc, out, _ = self.run_hook(
            payload, {"CHRONO_CONTEXT_WARN": "50000", "CHRONO_CONTEXT_HARD": "150000"}
        )
        self.assertEqual(rc, 0)
        self.assertIn("WARN", self.parse(out))
        rc, out, _ = self.run_hook(payload, {"CHRONO_CONTEXT_WARN": "garbage"})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "a malformed threshold falls back to the default")

    def test_malformed_stdin_is_silent(self) -> None:
        for stdin in ("not json", "", "[1,2,3]", '{"transcript_path": 7}'):
            rc, out, _ = self.run_hook(stdin)
            self.assertEqual(rc, 0, stdin)
            self.assertEqual(out, "", stdin)

    def test_missing_transcript_is_silent(self) -> None:
        rc, out, _ = self.run_hook(self.hook_payload(self.tmp / "absent.jsonl"))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_unreadable_or_garbage_transcript_is_silent(self) -> None:
        path = self.tmp / "garbage.jsonl"
        path.write_text("{not json\n\n{\"type\": \"user\"}\n", encoding="utf-8")
        rc, out, _ = self.run_hook(self.hook_payload(path))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_only_tail_is_read_and_partial_first_line_is_skipped(self) -> None:
        # A 5 MB transcript whose last assistant record sits inside the final 4 MB.
        # The 4 MB cut lands mid-line in the padding, which must be skipped cleanly.
        path = self.tmp / "big.jsonl"
        padding_line = json.dumps({"type": "user", "message": {"content": "x" * 100_000}})
        with path.open("w", encoding="utf-8") as fh:
            for _ in range(56):
                fh.write(padding_line + "\n")
            fh.write(assistant_record(10, 10, 10) + "\n")
            fh.write(assistant_record(650_000, 100, 20) + "\n")
            fh.write(user_record("after") + "\n")
        self.assertGreater(path.stat().st_size, 5 * 1024 * 1024)
        started = time.monotonic()
        rc, out, _ = self.run_hook(self.hook_payload(path))
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 0)
        self.assertIn("650120 tokens", self.parse(out))
        self.assertLess(elapsed, 1.0)

    def test_last_assistant_record_wins_over_earlier_ones(self) -> None:
        path = self.tmp / "order.jsonl"
        lines = [
            assistant_record(700_000, 0, 0),
            user_record(),
            assistant_record(100_000, 0, 0),
            user_record("trailing user record without usage"),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rc, out, _ = self.run_hook(self.hook_payload(path))
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_warn_above_hard_does_not_silence_hard(self) -> None:
        # Operator misconfiguration: WARN set above HARD. A context past HARD must still fire.
        env = {"CHRONO_CONTEXT_WARN": "700000", "CHRONO_CONTEXT_HARD": "600000"}
        rc, out, _ = self.run_hook(self.hook_payload(self.write_transcript(650_000)), env)
        self.assertEqual(rc, 0)
        text = self.parse(out)
        self.assertIn("HARD", text)
        self.assertIn("650000 tokens", text)
        rc, out, _ = self.run_hook(self.hook_payload(self.write_transcript(550_000)), env)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "", "below both thresholds stays silent")

    def test_settings_json_has_hook_retired_and_script_resolves_without_project_dir(self) -> None:
        # RETIRED 2026-09-21 by operator decision (see shared/lifecycle.md § 8): the
        # UserPromptSubmit hook is deliberately NOT registered, because the harness
        # auto-compacts and a forced /clear discarded live state. The script stays on
        # disk and is still covered below, so re-enabling is a one-line settings change.
        data = json.loads(SETTINGS.read_text(encoding="utf-8"))
        self.assertNotIn(
            "UserPromptSubmit",
            data.get("hooks", {}),
            "context tripwire is retired; re-registering it needs an explicit operator decision",
        )
        # The command the hook used to carry, kept literal here so the script's own path
        # resolution stays covered now that settings.json no longer supplies it.
        hook_command = 'bash "${CLAUDE_PROJECT_DIR:-.}/../bin/context-tripwire.sh"'
        # Above the 750k default hard limit, so this asserts the HARD band regardless of
        # where the warn threshold currently sits.
        payload = self.hook_payload(self.write_transcript(800_000))
        base = {k: v for k, v in os.environ.items() if not k.startswith("CHRONO_CONTEXT_")}
        base.pop("CLAUDE_PROJECT_DIR", None)
        # Path 1: CLAUDE_PROJECT_DIR set, cwd elsewhere (the documented hook environment).
        # Path 2: CLAUDE_PROJECT_DIR unset, cwd = the launch directory (the fallback).
        for env, cwd in (
            ({**base, "CLAUDE_PROJECT_DIR": str(ROOT / "chrono")}, self.tmp),
            (base, ROOT / "chrono"),
        ):
            proc = subprocess.run(
                ["bash", "-c", hook_command],
                input=payload,
                capture_output=True,
                text=True,
                env=env,
                cwd=cwd,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertIn("HARD", self.parse(proc.stdout))

    @unittest.skipUnless(REAL_TRANSCRIPT.is_file(), "real transcript not on this machine")
    def test_real_sized_transcript_parses_in_hard_under_one_second(self) -> None:
        # The live coordinator is still writing REAL_TRANSCRIPT, so its last usage is not a
        # fixed number. Copy it and append one sentinel record: same size and real record
        # shapes, but a value this test owns. The pristine file is only held to the contract.
        copy = self.tmp / "real-copy.jsonl"
        shutil.copyfile(REAL_TRANSCRIPT, copy)
        with copy.open("a", encoding="utf-8") as fh:
            fh.write(assistant_record(800_000 - 1100, 1000, 100) + "\n")
        started = time.monotonic()
        rc, out, _ = self.run_hook(self.hook_payload(copy))
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 0)
        text = self.parse(out)
        self.assertIn("HARD", text)
        self.assertIn("800000 tokens", text)
        self.assertLess(elapsed, 1.0)
        rc, out, _ = self.run_hook(self.hook_payload(REAL_TRANSCRIPT))
        self.assertEqual(rc, 0)
        if out:
            self.parse(out)


if __name__ == "__main__":
    unittest.main()
