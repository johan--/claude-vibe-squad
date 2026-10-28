"""Decision ownership, global supersession, and fail-loud capsule projection."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chrono_state import decisions, resume  # noqa: E402

# The capsule lists only harness decisions stamped within
# ``resume.ACTIVE_DECISION_MAX_AGE_DAYS`` (30) of ``now`` and collapses the rest
# to a count (audit Rec 3.1). Every render here pins ``now`` and every synthetic
# event is stamped inside that window, so the rendered set is a function of the
# fixture and never of the wall clock. ``decisions.record`` stamps the wall
# clock, which is never earlier than this pin, so recorded rows stay inside too.
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
RECENT_STAMP = "2026-10-20T00:00:00Z"
# The frozen journal's newest row is 2026-09-22T18:06:04Z. Rendering as of that
# day keeps its 2026-09-19..21 harness rows inside the window and its
# 2026-07-24 rows outside it.
FIXTURE_NOW = datetime(2026, 9, 22, 23, 59, 59, tzinfo=timezone.utc)
RECENT_HARNESS_IDS = {"DEC-969626e6da15", "DEC-45bc970d8d20", "DEC-826bc1b70eab"}


PRODUCT_IDS = {
    "DEC-217af2dae9d9", "DEC-4ba93d297fac", "DEC-95eaeb0d7670",
    "DEC-79a9dc0fc097", "DEC-357f6dc2dafa", "DEC-93d386c0b738",
    "DEC-8f77871fa933",
}
HARNESS_IDS = {
    "DEC-a5b221307538", "DEC-8ce85ea8304a", "DEC-21a60b7392ea",
    "DEC-60800610769e", "DEC-c067ae7a544c", "DEC-faae54e75561",
    "DEC-88c1dc934732", "DEC-57d16676bb86", "DEC-685acde8c0c4",
    "DEC-075565dbed4b", "DEC-b04ede009bec",
    "DEC-2026-08-18-public-history-acceptance", "DEC-969626e6da15",
    "DEC-6d136c7c8bf5", "DEC-45bc970d8d20", "DEC-826bc1b70eab",
}
ACTIVE_HARNESS_IDS = HARNESS_IDS - {"DEC-a5b221307538", "DEC-6d136c7c8bf5"}
# Frozen copy of the 2026-09-22 live journal. Tests never read host state.
# SHA-256: b941db7fe74290ec1580a0f57dbe36425206fa86ed1422d854b8dd2325056ac6
LIVE_JOURNAL_FIXTURE = r'''{"decision_id": "DEC-a5b221307538", "status": "confirmed", "statement": "Clean-in-place; keep existing private + public repos; no new repo, no history rewrite", "scope": [], "source_turn_ids": ["consolidation-session-2026-07-23"], "supersedes": [], "timestamp": "2026-07-24T04:07:49Z"}
{"decision_id": "DEC-8ce85ea8304a", "status": "confirmed", "statement": "Synthetic fixture decision DEC-8ce85ea8304a.", "scope": [], "source_turn_ids": ["fixture-turn-33"], "supersedes": [], "timestamp": "2026-07-24T04:07:49Z"}
{"decision_id": "DEC-21a60b7392ea", "status": "confirmed", "statement": "Synthetic fixture decision DEC-21a60b7392ea.", "scope": [], "source_turn_ids": ["fixture-turn-34"], "supersedes": [], "timestamp": "2026-07-24T04:07:49Z"}
{"decision_id": "DEC-60800610769e", "status": "confirmed", "statement": "Synthetic fixture decision DEC-60800610769e.", "scope": [], "source_turn_ids": ["fixture-turn-35"], "supersedes": [], "timestamp": "2026-07-24T04:07:49Z"}
{"decision_id": "DEC-c067ae7a544c", "status": "confirmed", "statement": "Synthetic fixture decision DEC-c067ae7a544c.", "scope": [], "source_turn_ids": ["fixture-turn-36"], "supersedes": [], "timestamp": "2026-07-24T04:07:49Z"}
{"decision_id": "DEC-faae54e75561", "status": "confirmed", "statement": "Synthetic fixture decision DEC-faae54e75561.", "scope": [], "source_turn_ids": ["fixture-turn-37"], "supersedes": [], "timestamp": "2026-07-24T04:07:49Z"}
{"decision_id": "DEC-88c1dc934732", "status": "confirmed", "statement": "Synthetic fixture decision DEC-88c1dc934732.", "scope": [], "source_turn_ids": ["fixture-turn-38"], "supersedes": [], "timestamp": "2026-07-24T04:07:49Z"}
{"decision_id": "DEC-57d16676bb86", "status": "confirmed", "statement": "Synthetic fixture decision DEC-57d16676bb86.", "scope": [], "source_turn_ids": ["fixture-turn-39"], "supersedes": [], "timestamp": "2026-07-24T14:56:48Z"}
{"decision_id": "DEC-685acde8c0c4", "status": "confirmed", "statement": "Synthetic fixture decision DEC-685acde8c0c4.", "scope": [], "source_turn_ids": ["fixture-turn-40"], "supersedes": [], "timestamp": "2026-07-24T14:56:48Z"}
{"decision_id": "DEC-075565dbed4b", "status": "confirmed", "statement": "Synthetic fixture decision DEC-075565dbed4b.", "scope": [], "source_turn_ids": ["fixture-turn-41"], "supersedes": [], "timestamp": "2026-07-24T15:13:25Z"}
{"decision_id": "DEC-b04ede009bec", "status": "confirmed", "statement": "Synthetic fixture decision DEC-b04ede009bec.", "scope": [], "source_turn_ids": ["fixture-turn-42"], "supersedes": [], "timestamp": "2026-07-24T16:48:15Z"}
{"decision_id": "DEC-2026-08-18-public-history-acceptance", "status": "active", "statement": "Synthetic fixture decision DEC-2026-08-18-public-history-acceptance.", "scope": "public_release", "source_turn_ids": ["fixture-turn-43"], "supersedes": "2026-07-28 refs/pull/1/head LOW-severity acceptance", "timestamp": "2026-08-18T21:50:17-07:00"}
{"decision_id": "DEC-217af2dae9d9", "status": "confirmed", "statement": "Synthetic fixture decision DEC-217af2dae9d9.", "scope": ["example-product"], "source_turn_ids": ["fixture-turn-44"], "supersedes": [], "timestamp": "2026-09-19T20:06:32Z"}
{"decision_id": "DEC-969626e6da15", "status": "confirmed", "statement": "Synthetic fixture decision DEC-969626e6da15.", "scope": ["repo-policy", "dispatch"], "source_turn_ids": ["fixture-turn-45"], "supersedes": ["DEC-a5b221307538"], "timestamp": "2026-09-19T20:06:32Z"}
{"decision_id": "DEC-6d136c7c8bf5", "status": "confirmed", "statement": "Synthetic fixture decision DEC-6d136c7c8bf5.", "scope": ["simplification", "dispatch"], "source_turn_ids": ["fixture-turn-46"], "supersedes": [], "timestamp": "2026-09-20T03:33:10Z"}
{"decision_id": "DEC-45bc970d8d20", "status": "confirmed", "statement": "Synthetic fixture decision DEC-45bc970d8d20.", "scope": ["simplification", "dispatch", "repo-policy"], "source_turn_ids": ["fixture-turn-47"], "supersedes": ["DEC-6d136c7c8bf5"], "timestamp": "2026-09-20T03:41:28Z"}
{"decision_id": "DEC-826bc1b70eab", "status": "active", "statement": "Synthetic fixture decision DEC-826bc1b70eab.", "scope": "public-export", "source_turn_ids": ["fixture-turn-48"], "supersedes": [], "timestamp": "2026-09-21T01:47:49Z"}
{"decision_id": "DEC-4ba93d297fac", "status": "confirmed", "statement": "Synthetic fixture decision DEC-4ba93d297fac.", "scope": ["example-product", "chain", "token", "oracle-deployment", "article", "contract"], "source_turn_ids": ["fixture-turn-49"], "supersedes": [], "timestamp": "2026-09-21T15:08:18Z"}
{"decision_id": "DEC-95eaeb0d7670", "status": "confirmed", "statement": "Synthetic fixture decision DEC-95eaeb0d7670.", "scope": ["example-product", "chain", "oracle-deployment", "denomination", "launch-order"], "source_turn_ids": ["fixture-turn-50"], "supersedes": [], "timestamp": "2026-09-21T16:07:22Z"}
{"decision_id": "DEC-79a9dc0fc097", "status": "confirmed", "statement": "Synthetic fixture decision DEC-79a9dc0fc097.", "scope": ["example-product", "fulfiller", "operations", "custody", "economics"], "source_turn_ids": ["fixture-turn-51"], "supersedes": [], "timestamp": "2026-09-21T21:51:11Z"}
{"decision_id": "DEC-357f6dc2dafa", "status": "confirmed", "statement": "Synthetic fixture decision DEC-357f6dc2dafa.", "scope": ["example-product", "fulfiller", "economics", "gas-cap"], "source_turn_ids": ["fixture-turn-52"], "supersedes": [], "timestamp": "2026-09-21T21:52:08Z"}
{"decision_id": "DEC-93d386c0b738", "status": "confirmed", "statement": "Synthetic fixture decision DEC-93d386c0b738.", "scope": ["example-product", "fulfiller", "retrieval", "abstention", "testnet"], "source_turn_ids": ["fixture-turn-53"], "supersedes": [], "timestamp": "2026-09-21T23:07:51Z"}
{"decision_id": "DEC-8f77871fa933", "status": "confirmed", "statement": "Synthetic fixture decision DEC-8f77871fa933.", "scope": ["example-product", "positioning", "competitor"], "source_turn_ids": ["fixture-turn-54"], "supersedes": [], "timestamp": "2026-09-22T18:06:04Z"}
'''
DECISION_REF = re.compile(r"\[(DEC-[^\]]+)\]")


def event(did, **fields):
    return {
        "decision_id": did, "status": "confirmed", "statement": f"Decision {did}",
        "home": "harness", "scope": [], "supersedes": [], "timestamp": RECENT_STAMP,
        **fields,
    }


def harness_section(text):
    return text.split("## Active decisions\n", 1)[1].split("\n## ", 1)[0]


class DecisionPartitionTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.journal = self.base / "decisions.jsonl"
        self.capsule = self.base / "resume.md"
        for patch in (
            mock.patch.object(decisions, "DECISIONS_FILE", self.journal),
            mock.patch.object(resume, "CAPSULE_PATH", self.capsule),
            mock.patch.object(resume, "registry_view", return_value={
                "live": [], "deferred": [], "unclassified": {},
            }),
            mock.patch.object(resume, "pending_completions", return_value=[]),
            mock.patch.object(resume, "active_thread_charters", return_value=[]),
            mock.patch.object(resume, "_archived_debt_rows", return_value=([], False)),
            mock.patch.object(resume, "open_work_items", return_value=[]),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, events):
        self.journal.write_text("".join(json.dumps(e) + "\n" for e in events))

    def render(self, max_tokens=3000, now=NOW):
        resume.write_capsule("partition-test", max_tokens=max_tokens, now=now)
        return self.capsule.read_text()

    def test_exact_rendered_ids_and_global_cross_home_supersession(self):
        # The two live legacy rows' IDs and field shapes; prose shortened deliberately.
        acceptance = event(
            "DEC-2026-08-18-public-history-acceptance", status="active",
            scope="public_release",
            supersedes="2026-07-28 refs/pull/1/head LOW-severity acceptance",
        )
        copyright_row = event("DEC-826bc1b70eab", status="active", scope="public-export")
        del acceptance["home"], copyright_row["home"]
        self.write([
            event("DEC-retained"),
            event("DEC-product", home="product"),
            event("DEC-old-product"),
            event("DEC-product-successor", home="product", supersedes=["DEC-old-product"]),
            event("DEC-old-unknown"),
            event("DEC-unknown-successor", home="unknown", supersedes=["DEC-old-unknown"]),
            event("DEC-old-string"),
            event("DEC-string-successor", supersedes="DEC-old-string"),
            acceptance, copyright_row,
        ])
        folded = decisions.active_decisions()
        self.assertEqual({d["decision_id"] for d in folded}, {
            "DEC-retained", "DEC-product", "DEC-product-successor",
            "DEC-unknown-successor", "DEC-string-successor",
            "DEC-2026-08-18-public-history-acceptance", "DEC-826bc1b70eab",
        })
        self.assertEqual({(d.kind, d.field, d.decision_id) for d in folded.diagnostics}, {
            ("unknown_home", "home", "DEC-unknown-successor"),
            ("legacy_normalization", "supersedes", "DEC-string-successor"),
            ("legacy_normalization", "scope", "DEC-2026-08-18-public-history-acceptance"),
            ("legacy_normalization", "supersedes", "DEC-2026-08-18-public-history-acceptance"),
            ("legacy_normalization", "status", "DEC-2026-08-18-public-history-acceptance"),
            ("legacy_normalization", "scope", "DEC-826bc1b70eab"),
            ("legacy_normalization", "status", "DEC-826bc1b70eab"),
        })
        for budget in (3000, 1):
            with self.subTest(budget=budget):
                text = self.render(budget)
                self.assertEqual(set(DECISION_REF.findall(text)), {
                    "DEC-retained", "DEC-string-successor", "DEC-unknown-successor",
                    "DEC-2026-08-18-public-history-acceptance", "DEC-826bc1b70eab",
                })
                harness = text.split("## Active decisions\n", 1)[1].split("\n## ", 1)[0]
                self.assertEqual(set(DECISION_REF.findall(harness)), {
                    "DEC-retained", "DEC-string-successor",
                    "DEC-2026-08-18-public-history-acceptance", "DEC-826bc1b70eab",
                })
                self.assertIn("2 active product decision(s)", text)
                self.assertIn("_state/chrono/decisions.jsonl", text)
                for row in folded:
                    if row["home"] == "harness":
                        self.assertIn(f"- {row['statement']} [{row['decision_id']}]", harness)
                self.assertEqual(text.count("- WARNING DECISION"), 1)
                self.assertEqual(text.count("- NOTICE DECISION LEGACY_NORMALIZATION"), 3)

    def test_legacy_product_allowlist_is_exact_and_scope_is_not_ownership(self):
        self.assertEqual(decisions.LEGACY_PRODUCT_DECISION_IDS, PRODUCT_IDS)
        legacy = [event(did, scope="example-product") for did in sorted(PRODUCT_IDS)]
        for row in legacy:
            del row["home"]
        self.write(legacy)
        folded = decisions.active_decisions()
        self.assertEqual({d["home"] for d in folded}, {"product"})
        self.assertTrue(all(d["scope"] == ["example-product"] for d in folded))
        self.assertEqual({d.kind for d in folded.diagnostics}, {"legacy_normalization"})
        self.assertIn("7 active product decision(s)", self.render())
        for scope in ([], "harness", ["dispatch"], ["example-product"]):
            self.assertEqual(decisions.decision_home({
                "decision_id": "DEC-unclassified", "scope": scope,
            }), "unknown")
        for home in ("harness", "product", "unknown", None):
            self.assertEqual(decisions.decision_home({
                "decision_id": "DEC-8f77871fa933", "home": home,
            }), home if home in ("harness", "product") else "unknown")

    def test_live_journal_fixture_renders_recent_harness_ids_and_counts_the_rest(self):
        self.journal.write_text(LIVE_JOURNAL_FIXTURE)
        rows = {}
        for line in LIVE_JOURNAL_FIXTURE.splitlines():
            row = json.loads(line)
            rows[row["decision_id"]] = row
        self.assertEqual(len(rows), 23)
        folded = decisions.active_decisions()
        # Authority is untouched by the window: the fold still retains every
        # active row. Only the capsule's projection of it narrows.
        self.assertEqual({row["decision_id"] for row in folded},
                         ACTIVE_HARNESS_IDS | PRODUCT_IDS)
        self.assertFalse(any(d.kind == "unknown_home" for d in folded.diagnostics))
        older = ACTIVE_HARNESS_IDS - RECENT_HARNESS_IDS
        self.assertEqual(len(older), 11)

        text = self.render(now=FIXTURE_NOW)
        harness = harness_section(text)
        self.assertEqual(set(DECISION_REF.findall(harness)), RECENT_HARNESS_IDS)
        for did in RECENT_HARNESS_IDS:
            self.assertIn(f"- {rows[did]['statement']} [{did}]", harness)
        for did in older:
            self.assertNotIn(rows[did]["statement"], text)
        self.assertIn(
            "- (+11 harness decision(s) older than 30 days, omitted; see "
            "_state/chrono/decisions.jsonl)",
            harness,
        )
        self.assertIn("7 active product decision(s)", text)
        self.assertNotIn("UNKNOWN_HOME", text)
        self.assertEqual(text.count("NOTICE DECISION LEGACY_NORMALIZATION"), 2)
        self.assertLessEqual(len(text) // 4, 3000)

        # Control: the window, not the fixture, drives the collapse. Rendered as
        # of 2026-08-20 every Z-stamped active harness row is inside 30 days.
        # Only the legacy row stamped with a `-07:00` offset stays collapsed:
        # the parser reads the Z form `decisions.record` writes and nothing else,
        # so a hand-edited stamp counts as older by design.
        offset_stamped = "DEC-2026-08-18-public-history-acceptance"
        self.assertTrue(rows[offset_stamped]["timestamp"].endswith("-07:00"))
        text = self.render(now=datetime(2026, 8, 20, tzinfo=timezone.utc))
        harness = harness_section(text)
        self.assertEqual(
            set(DECISION_REF.findall(harness)), ACTIVE_HARNESS_IDS - {offset_stamped}
        )
        self.assertIn("- (+1 harness decision(s) older than 30 days", harness)

    def test_undated_and_out_of_window_harness_rows_collapse_to_a_count(self):
        undated = event("DEC-undated")
        del undated["timestamp"]
        self.write([
            event("DEC-fresh"),
            event("DEC-stale", timestamp="2026-08-01T00:00:00Z"),
            undated,
        ])
        harness = harness_section(self.render())
        self.assertEqual(DECISION_REF.findall(harness), ["DEC-fresh"])
        self.assertIn(
            "- (+2 harness decision(s) older than 30 days, omitted; see "
            "_state/chrono/decisions.jsonl)",
            harness,
        )
        self.assertNotIn("(none", harness)
        # Everything outside the window: the list says so instead of going blank.
        harness = harness_section(self.render(now=datetime(2027, 6, 1, tzinfo=timezone.utc)))
        self.assertEqual(DECISION_REF.findall(harness), [])
        self.assertIn("- (none in the last 30 days)", harness)
        self.assertIn("- (+3 harness decision(s) older than 30 days", harness)

    def test_legacy_harness_allowlist_is_exact_and_explicit_home_wins(self):
        self.assertEqual(decisions.LEGACY_HARNESS_DECISION_IDS, HARNESS_IDS)
        self.assertTrue(HARNESS_IDS.isdisjoint(PRODUCT_IDS))
        for did in sorted(HARNESS_IDS):
            with self.subTest(did=did):
                self.assertEqual(decisions.decision_home({"decision_id": did}), "harness")
                for home in ("harness", "product", "unknown", None):
                    self.assertEqual(decisions.decision_home({
                        "decision_id": did, "home": home,
                    }), home if home in ("harness", "product") else "unknown")

    def test_unknown_home_only_warns_for_latest_active_rows(self):
        self.write([
            event("DEC-retired", home="unknown"),
            event("DEC-successor", home="product", supersedes=["DEC-retired"]),
            event("DEC-reclassified", home="unknown"),
            event("DEC-reclassified"),
            event("DEC-terminal", home="unknown", status="superseded"),
            event("DEC-unclassified", home="unknown"),
            event("DEC-unclassified", home="unknown", statement="latest unknown"),
        ])
        folded = decisions.active_decisions()
        self.assertEqual([(d.kind, d.decision_id, d.line) for d in folded.diagnostics],
                         [("unknown_home", "DEC-unclassified", 7)])
        text = self.render()
        self.assertEqual(text.count("WARNING DECISION UNKNOWN_HOME"), 1)
        self.assertIn("line 7 home: ownership unknown", text)
        self.assertIn("[DEC-unclassified]", text)
        self.assertNotIn("[DEC-retired]", text)
        self.assertNotIn("[DEC-terminal]", text)

    def test_normalization_notices_collapse_repeated_events_per_decision(self):
        self.write([
            event("DEC-legacy", status="active", scope="policy", supersedes="DEC-old"),
            event("DEC-legacy", status="active", scope="repo"),
            event("DEC-successor", supersedes=["DEC-legacy"]),
        ])
        text = self.render()
        self.assertEqual(text.count("NOTICE DECISION LEGACY_NORMALIZATION"), 1)
        self.assertIn("lines 1,2: scope, supersedes converted to lists", text)
        self.assertIn("legacy active retained; confirmation not inferred [DEC-legacy]", text)
        self.assertNotIn("WARNING DECISION", text)

    def test_record_omitted_home_is_unknown_and_explicit_homes_project(self):
        unknown = decisions.record("omitted home", "confirmed", ["turn-1"])
        harness = decisions.record("harness policy", "confirmed", [], home="harness")
        product = decisions.record("product policy", "confirmed", [], home="product")
        stored = [json.loads(line) for line in self.journal.read_text().splitlines()]
        self.assertEqual([d["home"] for d in stored], ["unknown", "harness", "product"])
        text = self.render()
        self.assertEqual(set(DECISION_REF.findall(text)), {unknown, harness})
        self.assertNotIn(product, text)
        self.assertIn("WARNING DECISION UNKNOWN_HOME", text)

    def test_record_rejects_invalid_new_fields_before_appending(self):
        decisions.record("positive control", "confirmed", [], home="harness")
        before = self.journal.read_bytes()
        for fields in ({"home": "guess"}, {"scope": "harness"},
                       {"supersedes": "DEC-old"}, {"supersedes": [None]}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                decisions.record("bad", "confirmed", [], **fields)
        with self.assertRaises(ValueError):
            decisions.record("bad status", "active", [])
        self.assertEqual(self.journal.read_bytes(), before)

    def test_diagnostics_survive_supersession_and_unknown_status_edges(self):
        self.write([
            event("DEC-old"),
            event("DEC-future", status="future-status", home="product",
                  scope=42, supersedes=["DEC-old", None]),
            event("DEC-retired-diagnostic", home="unknown"),
            event("DEC-terminal", status="superseded", home="product",
                  supersedes=["DEC-retired-diagnostic"]),
        ])
        folded = decisions.active_decisions()
        self.assertEqual(list(folded), [])
        self.assertEqual({d.kind for d in folded.diagnostics}, {
            "unrecognized_status", "malformed_field",
        })
        text = self.render(1)
        self.assertEqual(set(DECISION_REF.findall(text)), {
            "DEC-future",
        })
        self.assertIn("future-status", text)
        self.assertEqual(text.count("- WARNING DECISION"), len(folded.diagnostics))

    def test_latest_status_for_same_id_does_not_resurrect_old_authority(self):
        for status in ("superseded", "unrecognized"):
            with self.subTest(status=status):
                self.write([event("DEC-reused"), event("DEC-reused", status=status)])
                self.assertEqual(list(decisions.active_decisions()), [])
                self.assertEqual("UNRECOGNIZED_STATUS" in self.render(), status == "unrecognized")

    def test_malformed_events_are_loud_and_do_not_hide_valid_neighbors(self):
        self.write([
            event("DEC-good"), [],
            event(["invalid-id"], supersedes=["DEC-good"]),
            event("DEC-bad-fields", home=[], status={}, scope={}, supersedes={}, statement=None),
        ])
        with self.journal.open("a") as stream:
            stream.write("{broken json\n")
        folded = decisions.active_decisions()
        self.assertEqual(list(folded), [])
        self.assertEqual({d.kind for d in folded.diagnostics}, {
            "malformed_event", "malformed_field", "unrecognized_status",
        })
        text = self.render()
        self.assertEqual(text.count("- WARNING DECISION"), len(folded.diagnostics))
        self.assertIn("line 5 journal", text)
        self.assertNotIn("[DEC-good]", text)

    def test_missing_and_unreadable_journal_are_distinct(self):
        self.assertEqual(decisions.active_decisions().diagnostics, ())
        self.assertNotIn("WARNING DECISION", self.render())
        self.journal.write_bytes(b"\xff")
        self.assertEqual(decisions.active_decisions().diagnostics[0].kind, "unreadable_journal")
        self.assertIn("WARNING DECISION UNREADABLE_JOURNAL", self.render(1))


if __name__ == "__main__":
    unittest.main()
