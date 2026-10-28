from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest


PYTHON_DIR = Path(__file__).resolve().parents[1]
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

import dispatch_context_builder as dcb  # noqa: E402


class DeliveryContractNoteTests(unittest.TestCase):
    TASK_ID = "TASK-2026-09-23-0450-contract-test"
    OUTBOX = f"departments/coding/outbox/{TASK_ID}-response.md"
    ARTIFACT = "reports/result.md"
    NO_COMMIT = (
        "Never stage or commit `return_artifact` or the outbox envelope, "
        "even when asked to commit code."
    )

    def render(self, artifact: str, *, assembled: bool) -> str:
        scope = [artifact, "src/worker.py"]
        if assembled:
            return dcb.assemble_trusted_launch_prompt(
                "Commit the requested code and tests.",
                task_id=self.TASK_ID,
                attempt_id="d-" + "a" * 32,
                generation=1,
                memory_aperture="none",
                return_artifact=artifact,
                write_scope=scope,
                outbox_relative=self.OUTBOX,
            )
        return dcb.delivery_contract_note(
            artifact, scope, outbox_relative=self.OUTBOX
        )

    def assert_commit_contract(self, prompt: str, artifact: str) -> None:
        self.assertIn(self.NO_COMMIT, prompt)
        self.assertIn(
            "Leave those files in the worktree for controller promotion.", prompt
        )
        self.assertIn(f"`return_artifact`: `{artifact}`", prompt)
        self.assertIn(f"outbox envelope: `{self.OUTBOX}`", prompt)
        self.assertIn(
            "Packet-requested code commits remain allowed within the write scope.",
            prompt,
        )

    def test_delivery_outputs_are_excluded_from_commits_in_both_shapes(self) -> None:
        for artifact in (self.OUTBOX, self.ARTIFACT):
            for assembled in (False, True):
                with self.subTest(artifact=artifact, assembled=assembled):
                    self.assert_commit_contract(
                        self.render(artifact, assembled=assembled), artifact
                    )

    def test_missing_no_commit_sentence_is_detected(self) -> None:
        for artifact in (self.OUTBOX, self.ARTIFACT):
            with self.subTest(artifact=artifact):
                prompt = self.render(artifact, assembled=True)
                self.assert_commit_contract(prompt, artifact)
                mutated = prompt.replace(self.NO_COMMIT, "", 1)
                with self.assertRaises(AssertionError):
                    self.assert_commit_contract(mutated, artifact)

    def test_aliased_first_write_has_parseable_canonical_frontmatter(self) -> None:
        for assembled in (False, True):
            with self.subTest(assembled=assembled):
                prompt = self.render(self.OUTBOX, assembled=assembled)
                first_write = prompt.split("2. **Write only", 1)[0]
                self.assertIn("frontmatter on the FIRST write", first_write)
                self.assertIn("preserve it through every update", first_write)
                self.assertIn(
                    "Use `status: blocked` until there is a usable result", first_write
                )
                self.assertIn(
                    "Never fabricate a completion or a verdict.", first_write
                )
                blocks = re.findall(r"```yaml\n(.*?)```", first_write, re.DOTALL)
                self.assertEqual(len(blocks), 1)
                header = blocks[0].replace("<id>", self.TASK_ID).replace(
                    "<lane>", "gpt-codex"
                )
                for status, body in (
                    ("blocked", "Inspection is partial; verification remains."),
                    ("blocked", "Implementation is available; tests remain."),
                    ("complete", "Implementation and requested tests passed."),
                ):
                    updated_header = header.replace("status: blocked", f"status: {status}")
                    fields, summary = dcb._parse_response_envelope(
                        (updated_header + "\n" + body).encode("utf-8")
                    )
                    self.assertEqual(fields, {
                        "id": f"{self.TASK_ID}-response",
                        "in_response_to": self.TASK_ID,
                        "from": "gpt-codex",
                        "to": "chrono",
                        "type": "RESULT",
                        "status": status,
                        "return_artifact": self.OUTBOX,
                    })
                    self.assertEqual(summary, body)
                with self.assertRaises(dcb.DispatchContextError):
                    dcb._parse_response_envelope(b"Partial without frontmatter.")

    def test_split_outputs_keep_separate_envelope_and_scope_exemption(self) -> None:
        for assembled in (False, True):
            with self.subTest(assembled=assembled):
                prompt = self.render(self.ARTIFACT, assembled=assembled)
                self.assertIn("four ways good work gets discarded", prompt)
                self.assertIn(
                    f"Your envelope is a SECOND file here: also write `{self.OUTBOX}`",
                    prompt,
                )
                self.assertIn("**not** a scope violation", prompt)
                self.assertNotIn("artifact and envelope share this path", prompt)
                self.assertNotIn("```yaml", prompt)
                self.assertIn("4. **If you are running long", prompt)

    def test_unspecified_envelope_does_not_assume_aliased_outputs(self) -> None:
        note = dcb.delivery_contract_note(self.ARTIFACT, [self.ARTIFACT])
        self.assertIn(self.NO_COMMIT, note)
        self.assertIn("outbox envelope: the path declared in the packet", note)
        self.assertNotIn("artifact and envelope share this path", note)

    def test_no_artifact_keeps_note_empty(self) -> None:
        self.assertEqual(dcb.delivery_contract_note("", []), "")


if __name__ == "__main__":
    unittest.main()
