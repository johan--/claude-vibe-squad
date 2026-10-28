"""Scratch-only regression controls for TASK-2026-09-07-0335-a0c2e78e.

CHRONO_MEMORY_TEST_ROOT selects a disposable source copy for mutation controls.
No watcher daemon, provider, live registry, or live memory store is invoked.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = Path(os.environ.get("CHRONO_MEMORY_TEST_ROOT", REPO_ROOT))
PLUGIN_ROOT = SOURCE_ROOT / "plugins" / "chrono-vault"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "python"))
sys.path.insert(0, str(PLUGIN_ROOT))

import autocapture
import audit
import lifecycle
import notes
import privacy
import recall


# Synthetic format fixtures only. Build token strings so source scanners do not
# mistake literal test credentials for credentials to a real service.
IDENTIFIERS = {
    "email": "capture.person@example.invalid",
    "openai": "sk-proj-" + "A" * 48,
    "anthropic": "sk-ant-api03-" + "B" * 48,
    "xai": "xai-" + "C" * 48,
    "perplexity": "pplx-" + "D" * 48,
    "google": "AIza" + "E" * 35,
    "github_classic": "ghp_" + "F" * 36,
    "github_fine": "github_pat_" + "G" * 48,
    "aws": "AKIA" + "H" * 16,
    "slack": "xoxb-" + "1234567890-" * 2 + "I" * 24,
    "jwt": "eyJ" + "J" * 20 + "." + "K" * 20 + "." + "L" * 24,
    "huggingface": "hf_" + "M" * 36,
    "stripe": "sk_test_" + "N" * 32,
    "apify": "apify_api_" + "O" * 32,
    "url": "https://example.invalid/route?access_token=synthetic-private-value",
}


class ScratchMemoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="memory-fix-control-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.vault = self.root / "vault"
        self.repo = self.root / "repo"
        self.vault.mkdir()
        self.repo.mkdir()
        (self.vault / ".chrono-vault").write_text(
            json.dumps({"vault_id": "synthetic-memory-control", "schema_version": 1})
        )
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("CHRONO_", "VAULT_"))
        }
        env.update({
            "CHRONO_VAULT_ROOT": str(self.vault),
            "CHRONO_VAULT_AUDIT_DIR": str(self.root / "audit"),
            "CHRONO_AUTOCAPTURE_DISTILL": "off",
            "VAULT_ROOT": str(self.repo),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(autocapture, "REPO_ROOT", self.repo)
        patch.start()
        self.addCleanup(patch.stop)

    def response(self, body, number=1, **fields):
        folder = self.repo / "departments" / "coding" / "outbox"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"TASK-synthetic-{number}-response.md"
        metadata = {"specialist": "backend-engineer", "status": "complete", "mode": "build", **fields}
        path.write_text("---\n" + "\n".join(
            f"{key}: {json.dumps(value)}" for key, value in metadata.items()
        ) + "\n---\n\n" + body + "\n")
        return path

    def spool(self):
        paths = list((self.repo / "_state" / "episodic").glob("*.jsonl"))
        self.assertTrue(paths, "a missing spool is not a privacy pass")
        return [json.loads(line) for path in paths for line in path.read_text().splitlines()]

    def write_note(self, **fields):
        return notes.record("learning", {
            "title": "MemoryControl evidence", "body": "MemoryControl reusable evidence.",
            "component": "capture", **fields,
        })

    def test_baseline_identifiers_screened_through_real_capture(self):
        for number, (label, secret) in enumerate(IDENTIFIERS.items()):
            with self.subTest(label=label):
                path = self.response(
                    f"CaptureControl retains useful retry behavior and verified context. Contact {secret}.",
                    number, verdict=f"CaptureControl {secret}",
                )
                result = autocapture.capture_response(str(path))
                self.assertEqual(result["reason"], "captured")
                row = self.spool()[-1]
                self.assertIn("CaptureControl", row["raw_body"])
                self.assertNotIn(secret, json.dumps(row), label)
                self.assertIn("[REDACTED]", row["raw_body"])
                stored = lifecycle.get_note(result["note_id"])
                self.assertNotIn(secret, json.dumps(stored), label)
        print(f"redaction: {len(IDENTIFIERS)} synthetic formats screened in spool and canonical notes")

    def test_capture_email_canary(self):
        email = IDENTIFIERS["email"]
        path = self.response(f"CaptureControl preserves retry semantics and useful context for {email}.")
        result = autocapture.capture_response(str(path))
        row = self.spool()[0]
        print("capture=" + json.dumps(result, sort_keys=True))
        print("spool title=" + json.dumps(row["raw_title"]))
        print("spool body=" + json.dumps(row["raw_body"]))
        self.assertEqual(result["reason"], "captured")
        self.assertNotIn(email, json.dumps(row))
        self.assertIn("[REDACTED]", row["raw_body"])

    def test_normalized_content_is_screened_at_both_sinks(self):
        email = IDENTIFIERS["email"]
        cases = {"plain": email}
        for codepoint in range(32):
            character = chr(codepoint)
            if not character.isspace():
                cases[f"control-{codepoint:02x}"] = email.replace("@", character + "@")
        for number, (label, value) in enumerate(cases.items()):
            with self.subTest(label=label):
                body = f"CaptureControl preserves retry semantics and useful context for {value}."
                path = self.response(body, number, verdict=body)
                recipient = mock.Mock(return_value={
                    "title": "CaptureControl distilled", "body": "CaptureControl reusable context.",
                    "aliases": [], "keywords": [], "attack_class": "none",
                })
                with mock.patch.dict(os.environ, {"CHRONO_AUTOCAPTURE_DISTILL": "on"}):
                    result = autocapture.capture_response(str(path), distiller=recipient)
                if label == "control-00":
                    # The response parser rejects NUL before capture begins.
                    self.assertEqual(result["reason"], "malformed_frontmatter")
                    recipient.assert_not_called()
                    self.assertEqual(len(self.spool()), 1)
                    continue
                self.assertEqual(result["reason"], "captured")
                recipient.assert_called_once()
                row = self.spool()[-1]
                payload, context = recipient.call_args.args
                exposed = email in json.dumps([row, payload, context])
                if label in {"plain", "control-01"}:
                    print(f"{label}: spool body={json.dumps(row['raw_body'])}")
                    print(f"{label}: distiller payload={json.dumps(payload, sort_keys=True)}")
                self.assertFalse(exposed, "normalized identifier reached a sink")
                self.assertIn("[REDACTED]", row["raw_body"])
                self.assertIn("[REDACTED]", payload["body"])
        print(f"normalization: {len(cases) - 1} sink cases; one NUL parser refusal checked")

    def test_normalization_preserves_separators_and_unicode(self):
        email = IDENTIFIERS["email"]
        transformations = {
            "tab": "\t", "vertical-tab": "\v", "carriage-return": "\r",
            "nbsp": "\u00a0", "line-separator": "\u2028",
            "del": "\x7f", "c1": "\x80", "soft-hyphen": "\u00ad",
        }
        for number, (label, separator) in enumerate(transformations.items()):
            with self.subTest(label=label):
                value = email.replace("@", separator + "@")
                path = self.response(
                    f"CaptureControl preserves retry semantics and useful context for {value}.", number,
                )
                recipient = mock.Mock(return_value={
                    "title": "CaptureControl distilled", "body": "CaptureControl reusable context.",
                    "aliases": [], "keywords": [], "attack_class": "none",
                })
                with mock.patch.dict(os.environ, {"CHRONO_AUTOCAPTURE_DISTILL": "on"}):
                    result = autocapture.capture_response(str(path), distiller=recipient)
                self.assertEqual(result["reason"], "captured")
                recipient.assert_called_once()
                row = self.spool()[-1]
                self.assertNotIn(email, json.dumps([row, recipient.call_args.args]))
                self.assertIn("CaptureControl", row["raw_body"])
                self.assertEqual(privacy.redact_text("left" + separator + "right"),
                                 "left" + separator + "right")
        print(f"transformations: {len(transformations)} separator/preserved-character cases checked at both sinks")

    def test_normalization_screens_all_baseline_formats(self):
        checked = 0
        for label, value in IDENTIFIERS.items():
            for codepoint in range(32):
                if chr(codepoint).isspace():
                    continue
                with self.subTest(label=label, codepoint=codepoint):
                    normalized = privacy.redact_text(chr(codepoint).join(value))
                    self.assertEqual(normalized, privacy.redact_text(value))
                    self.assertIn("[REDACTED]", normalized)
                    checked += 1
        print(f"normalization: {checked} baseline-format/control combinations screened")

    def test_screening_precedes_bounds_and_refusal(self):
        email = IDENTIFIERS["email"]
        path = self.response("x " * 742 + email)
        autocapture.capture_response(str(path))
        row = self.spool()[-1]
        self.assertNotIn("capture.person", row["raw_body"])
        self.assertIn("[REDACTED]", row["raw_body"])
        short = self.response(email, 2)
        self.assertTrue(autocapture.capture_response(str(short))["reason"].startswith("refused:"))
        self.assertEqual(self.spool()[-1]["raw_body"], "[REDACTED]")

    def test_distiller_and_direct_record_receive_screened_content(self):
        email = IDENTIFIERS["email"]
        path = self.response(f"CaptureControl preserves useful failure context and retry semantics for {email}.")
        def distiller(capture_fields, context):
            self.assertNotIn(email, json.dumps(capture_fields))
            self.assertIn("[REDACTED]", capture_fields["body"])
            return {"title": "CaptureControl distilled", "body": "Distiller returned " + email,
                    "aliases": [email], "keywords": [email], "attack_class": "none"}
        with mock.patch.dict(os.environ, {"CHRONO_AUTOCAPTURE_DISTILL": "on"}):
            result = autocapture.capture_response(str(path), distiller=distiller)
        self.assertEqual(result["reason"], "captured")
        self.assertNotIn(email, json.dumps(lifecycle.get_note(result["note_id"])))
        direct = self.write_note(title=email, body=email, aliases=[email], evidence_refs=[email])
        self.assertNotIn(email, Path(direct["path"]).read_text())

    def test_metadata_and_replay_identity_are_screened_without_losing_dedupe(self):
        email = IDENTIFIERS["email"]
        path = self.response(
            "CaptureControl preserves CVE-2026-1234 and mem-123456abcdef as useful references.",
            specialist=email, target=email, verdict=email, artifacts=["reports/" + email + ".md"],
        )
        first = autocapture.capture_response(str(path), record_replay_event=False)
        second = autocapture.capture_response(str(path), record_replay_event=False)
        self.assertEqual(first["reason"], "captured")
        self.assertEqual(second["reason"], "duplicate")
        rows = self.spool()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("capture.person", json.dumps(rows))
        self.assertIn("CVE-2026-1234", rows[0]["raw_body"])
        self.assertIn("mem-123456abcdef", rows[0]["raw_body"])
        unsafe = path.with_name("TASK-" + IDENTIFIERS["openai"] + "-response.md")
        unsafe.write_bytes(path.read_bytes())
        self.assertEqual(autocapture.capture_response(str(unsafe))["reason"], "unsafe_task_identifier")
        self.assertEqual(len(self.spool()), 1)

        private_namespace = self.repo / "departments" / email / "outbox"
        private_namespace.mkdir(parents=True)
        copied = private_namespace / "TASK-synthetic-namespace-response.md"
        copied.write_bytes(path.read_bytes())
        self.assertEqual(autocapture.capture_response(str(copied))["reason"], "captured")
        self.assertNotIn(email, json.dumps(self.spool()[-1]))
        self.assertEqual(self.spool()[-1]["sensitivity"], "restricted")

    def test_legacy_spool_is_screened_before_graduation_without_rewrite(self):
        email = IDENTIFIERS["email"]
        body = f"CaptureControl preserves retry behavior and reusable context for {email}."
        path = self.response(body)
        with mock.patch.dict(os.environ, {"CHRONO_VAULT_ROOT": str(self.root / "missing")}):
            self.assertEqual(autocapture.capture_response(str(path))["reason"], "vault_unavailable")
        row = self.spool()[0]
        row["raw_body"] = body
        row["raw_title"] = email
        spool_path = next((self.repo / "_state" / "episodic").glob("*.jsonl"))
        spool_path.write_text(json.dumps(row) + "\n")  # synthetic historical row
        before = spool_path.read_bytes()
        def distiller(fields, context):
            self.assertNotIn(email, json.dumps(fields))
            return {"title": "Legacy CaptureControl", "body": fields["body"],
                    "aliases": [], "keywords": [], "attack_class": "none"}
        with mock.patch.dict(os.environ, {"CHRONO_AUTOCAPTURE_DISTILL": "on"}):
            result = autocapture.graduate_spooled_once(seed="synthetic", distiller=distiller)
        self.assertTrue(result["graduated"], result)
        self.assertNotIn(email, json.dumps(lifecycle.get_note(result["note_id"])))
        self.assertEqual(spool_path.read_bytes(), before)

    def test_recall_returns_late_body_and_metadata_evidence(self):
        token = "LateEvidenceMarker"
        note = self.write_note(body="Background detail. " * 100 + "Do not enable " + token + " without review.")
        row = recall.recall(token)["results"][0]
        print("recall snippet=" + json.dumps(row["snippet"]))
        self.assertEqual(row["id"], note["id"])
        self.assertIn("Do not enable " + token, row["snippet"])
        self.assertIn("without review", row["snippet"])
        self.assertLessEqual(len(row["snippet"]), 700)
        self.assertNotIn("\x01", row["snippet"])
        for field in ("title", "aliases", "keywords", "component", "target", "attack_class"):
            with self.subTest(field=field):
                marker = "MetadataMarker" + field.replace("_", "")
                value = [marker] if field in {"aliases", "keywords"} else marker
                expected = self.write_note(**{field: value})
                result = recall.recall(marker)["results"][0]
                self.assertEqual(result["id"], expected["id"])
                self.assertIn(marker, result["snippet"])

    def test_retirement_preserves_history_relationships_and_feedback(self):
        original = self.write_note()
        audit.emit("contradiction", result=audit.CONTRA_FLAGGED,
                   request_hash=audit.request_digest("synthetic", {}),
                   returned_note_ids=[original["id"]],
                   extra={"unreconciled_note_ids": [original["id"]]})
        history = {p: p.read_bytes() for p in (self.root / "audit" / "contradiction").glob("evt-*.json")
                   if json.loads(p.read_text())["result"] == "flagged"}
        self.assertEqual(len(history), 1, "historical flag positive control")
        complementary = self.write_note(body="MemoryControl independent cache setting.")
        same_topic = recall.recall("MemoryControl")
        self.assertEqual(len(same_topic["results"]), 2)
        for row in same_topic["results"]:
            self.assertNotIn("disputed", row)
        for relationship in ("coexists-with", "supersedes"):
            directive = {"relationship": relationship, "note_id": original["id"]}
            if relationship == "coexists-with":
                directive["condition"] = "Different deployment settings."
            declared = self.write_note(contradiction=directive)
            stored = lifecycle.get_note(declared["id"])
            if relationship == "supersedes":
                self.assertIn(original["id"], stored["supersedes"])
            else:
                self.assertIn("coexists-with:" + original["id"], stored["evidence_refs"])
        for outcome in ("incorrect", "not_useful"):
            result = recall.recall("MemoryControl")
            lifecycle.record_usage(result["recall_id"], complementary["id"], outcome, repo_root=self.repo)
        row = next(r for r in recall.recall("MemoryControl")["results"] if r["id"] == complementary["id"])
        self.assertEqual(row["score_components"]["usage"]["incorrect"], 1)
        self.assertEqual(row["score_components"]["usage"]["not_useful"], 1)
        self.assertLess(row["score_components"]["usage"]["signal"], 0)
        queue = [json.loads(line) for line in (self.repo / "_state" / "curation-queue.jsonl").read_text().splitlines()]
        self.assertEqual({r["reason"] for r in queue}, {"incorrect", "not_useful"})
        for path, content in history.items():
            self.assertEqual(path.read_bytes(), content)
        events = [json.loads(p.read_text()) for p in (self.root / "audit" / "contradiction").glob("evt-*.json")]
        self.assertEqual(sum(e["result"] == "flagged" for e in events), 1)
        self.assertEqual(sum(e["result"] == "declared" for e in events), 2)
        print("retirement: historical flag intact; two declarations and both negative feedback outcomes preserved")

    def test_resume_does_not_collect_or_render_retired_signal(self):
        spec = importlib.util.spec_from_file_location("resume_control", SOURCE_ROOT / "scripts/python/chrono_state/resume.py")
        resume = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(resume)
        view = {"live": [], "deferred": [], "unclassified": {}}
        with mock.patch.object(resume, "registry_view", return_value=view), \
             mock.patch.object(resume, "active_decisions", return_value=[]), \
             mock.patch.object(resume, "pending_completions", return_value=[(("coding", "REVIEW-REQUIRED"), 1)]), \
             mock.patch.object(resume, "active_thread_charters", return_value=[]), \
             mock.patch.object(resume, "_archived_debt_rows", return_value=([], False)), \
             mock.patch.object(resume, "open_work_items", return_value=[]), \
             mock.patch.object(audit, "resolve_audit_dir", side_effect=AssertionError("retired audit scan")) as scan:
            text = resume.render_capsule("synthetic", "Preserve this instruction")
            dest = resume.write_capsule("synthetic", "Preserve this instruction", path=self.repo / "resume.md")
            self.assertEqual(scan.call_count, 0)
            self.assertNotIn("contradiction", text.lower())
            self.assertNotIn("contradiction", dest.read_text().lower())
            self.assertIn("REVIEW-REQUIRED", text)
            # There is no live producer for operator turns in the capsule.
            # A stored instruction must not be presented as current authority.
            self.assertNotIn("Preserve this instruction", text)
            self.assertNotIn("Preserve this instruction", dest.read_text())
        print("resume: zero retired audit scans; pending review remains visible without stale operator instructions")

    def test_watcher_reports_missing_root_and_settlement_continues(self):
        source = (SOURCE_ROOT / "bin/outbox-watcher.sh").read_text()
        def function(name):
            match = re.search(r"(?m)^" + name + r"\(\) \{\n[\s\S]*?^\}", source)
            self.assertIsNotNone(match, name)
            return match.group()
        plugin = self.repo / "plugins/chrono-vault"
        plugin.mkdir(parents=True)
        for path in PLUGIN_ROOT.glob("*.py"):
            shutil.copyfile(path, plugin / path.name)
        bin_dir = self.repo / "bin"
        bin_dir.mkdir()
        reconciler = bin_dir / "registry-reconciler.sh"
        reconciler.write_text('#!/bin/bash\nprintf "settled\\n" > "$VAULT_ROOT/settlement"\nprintf "reconciled %s -> complete\\n" "$2"\n')
        reconciler.chmod(0o700)
        path = self.response("CaptureControl records reusable retry behavior and failure context in a screened spool.")
        script = "\n".join(function(name) for name in (
            "autocapture_response_best_effort", "autocapture_dispatch", "handle_response_path"
        )) + "\n" + r'''
set -e
date() { printf 'CONTROL-TIME\n'; }
python3() { "$CONTROL_PYTHON" "$@"; }
frontmatter_field() { printf 'complete\n'; }
response_context() { printf 'synthetic\n'; }
response_status() { printf 'complete\n'; }
status_nudge_prefix() { printf 'complete\n'; }
record_skill_telemetry_best_effort() { :; }
ALL_NAMESPACES=0
NAMESPACE=coding
PROCESSED_PATHS='|'
PENDING_PATHS='|'
AUTOCAPTURE_MAX_INFLIGHT=1
AUTOCAPTURE_PIDS=''
TMUX_BIN=/usr/bin/false
SESSION=synthetic
handle_response_path "$CONTROL_RESPONSE"
test "$(cat "$VAULT_ROOT/settlement")" = settled
printf 'SETTLEMENT_COMPLETED\n'
'''
        for number, label in enumerate(("ready", "missing", "unset"), 1):
            with self.subTest(root=label):
                path = self.response("CaptureControl records reusable retry behavior and failure context in a screened spool.", number)
                env = dict(os.environ, CONTROL_RESPONSE=str(path), CONTROL_PYTHON=sys.executable)
                if label == "missing":
                    env["CHRONO_VAULT_ROOT"] = str(self.root / "missing-vault")
                elif label == "unset":
                    env.pop("CHRONO_VAULT_ROOT", None)
                result = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True, timeout=20)
                print(f"watcher {label}: exit={result.returncode}\nstdout={result.stdout}stderr={result.stderr}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("SETTLEMENT_COMPLETED", result.stdout)
                if label == "ready":
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertIn("vault root unavailable", result.stderr)
                    self.assertIn("screened response retained", result.stderr)
                self.assertEqual(len(self.spool()), number)
                self.assertIn("CaptureControl", self.spool()[-1]["raw_body"])


if __name__ == "__main__":
    unittest.main()
