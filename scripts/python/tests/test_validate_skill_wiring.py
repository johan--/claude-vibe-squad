"""Retirement must fail at current demand, with real provider controls."""
from __future__ import annotations

import contextlib
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts/python'))
import validate_skill_wiring as wiring


class RetirementWiringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.name = 'fixture-retired'
        self.archive = f'shared/skills/_retired/{self.name}.md'
        self.rows = [self.row(self.archive, 'no')]
        self.write(self.archive, '# Retained historical document\n')
        self.write('shared/specialist-runtime-map.tsv', 'specialist\n')
        self.write_registry()
        self.write_source([])

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def row(self, path, state):
        return dict(name=self.name, record_kind='skill', type='authored-pattern-doc',
                    path_or_source=path, verified_state=state)

    def write_registry(self):
        path = self.root / wiring.REGISTRY_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.rows[0]), delimiter='\t')
            writer.writeheader()
            writer.writerows(self.rows)

    def write_source(self, skills):
        self.write(wiring.SOURCE_RELATIVE, json.dumps({
            'schema': 'specialist-lane-capabilities/v1', 'version': 1,
            'entries': [{'specialist': 'fixture-worker', 'lane': 'gpt-codex',
                         'coverage': 'full', 'skills': skills}],
        }))

    def wire(self, home, name=None, body=''):
        name = name or self.name
        return self.write(f'{home}/{name}/SKILL.md',
                          f'---\nname: {name}\naudience: specialist\n'
                          'description: Use when preparing a fixture for a local retirement regression.\n'
                          f'---\n\n{body}\n')

    def demand(self, path='shared/mode-profiles/project/control.md'):
        return self.write(path, f'Use `{self.name}`.\n')

    def check(self):
        return wiring.check_retired_skill_demand(self.root)

    def test_retired_row_fails_for_every_state_despite_existing_file(self):
        self.demand()
        self.assertTrue((self.root / self.archive).is_file())
        for state in ('no', 'authored', 'yes', 'partial'):
            with self.subTest(state=state):
                self.rows[0]['verified_state'] = state
                self.write_registry()
                errors, _ = self.check()
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("retired-only skill 'fixture-retired'", errors[0])
                self.assertIn('control.md:1', errors[0])
                self.assertEqual(wiring.run(self.root, verbose=False), 1)

    def test_archive_row_and_explicit_historical_mentions_pass(self):
        self.write('shared/modes/project.md', f'Do not use `{self.name}`.\n')
        self.write('shared/skills/_retired/consumer.md', f'Use `{self.name}`.\n')
        self.write('departments/coding/outbox/receipt.md', f'Use `{self.name}`.\n')
        self.write('docs/archive/history.md', f'Use `{self.name}`.\n')
        errors, report = self.check()
        self.assertEqual(errors, [])
        self.assertIn('0 demanded identifier(s)', report[0])
        self.assertIn('1 mention location(s)', report[0])
        self.assertEqual(wiring.run(self.root, verbose=False), 0)

    def test_positive_control_live_registry_successor(self):
        self.demand()
        live = f'shared/skills/{self.name}/SKILL.md'
        self.write(live, f'---\nname: {self.name}\n---\nCurrent methodology.\n')
        self.rows.append(self.row(live, 'authored'))
        self.write_registry()
        self.assertEqual(wiring.run(self.root, verbose=False), 0)

    def test_missing_or_unverified_successor_fails(self):
        self.demand()
        self.rows.append(self.row('shared/skills/missing.md', 'authored'))
        self.write_registry()
        self.assertEqual(len(self.check()[0]), 1)
        self.write('shared/skills/missing.md', 'A file alone is insufficient.\n')
        self.rows[-1]['verified_state'] = 'no'
        self.write_registry()
        self.assertEqual(len(self.check()[0]), 1)

    def test_missing_archive_still_fails(self):
        self.demand()
        self.rows[0]['path_or_source'] = 'shared/skills/_retired/missing.md'
        self.write_registry()
        self.assertEqual(len(self.check()[0]), 1)

    def test_valid_same_name_live_homes_clear_retirement_without_registry_rewrite(self):
        base = self.root
        for home in (wiring.CLAUDE_SKILLS_REL, wiring.AGENTS_SKILLS_REL):
            with self.subTest(home=home):
                self.root = base / home.split('/')[0].lstrip('.')
                self.write(self.archive, '# Retained archive\n')
                self.write('shared/specialist-runtime-map.tsv', 'specialist\n')
                self.write_registry()
                self.write_source([])
                self.demand()
                self.wire(home)
                self.assertEqual(self.check()[0], [])

    def test_move_with_stale_registry_path_still_fails(self):
        self.demand()
        self.rows[0] = self.row(f'shared/skills/{self.name}.md', 'authored')
        self.write_registry()
        self.assertEqual(len(self.check()[0]), 1)
        self.write(f'shared/skills/{self.name}.md', 'Live authored replacement.\n')
        self.assertEqual(self.check()[0], [])

    def test_malformed_same_name_load_path_does_not_clear_retirement(self):
        self.demand()
        self.write(f'{wiring.AGENTS_SKILLS_REL}/{self.name}/SKILL.md', '# Not a skill\n')
        self.assertEqual(len(self.check()[0]), 1)

    def test_symlinked_archive_is_not_a_live_provider(self):
        self.demand()
        alias = self.root / wiring.CLAUDE_SKILLS_REL / self.name / 'SKILL.md'
        alias.parent.mkdir(parents=True)
        alias.symlink_to(self.root / self.archive)
        self.assertEqual(len(self.check()[0]), 1)
        self.rows.append(self.row(alias.relative_to(self.root).as_posix(), 'authored'))
        self.write_registry()
        self.assertEqual(len(self.check()[0]), 1)

    def test_current_demand_surfaces_and_skill_references_are_enumerated(self):
        surfaces = (
            'shared/capabilities/project/control.md',
            'shared/capabilities/public/project/control.md',
            'shared/mode-profiles/project/control.md',
            'shared/modes/project.md',
            'shared/specialists/reviewer.md',
            'departments/coding/specialists/worker.md',
            'shared/skills/consumer.md',
        )
        for path in surfaces:
            self.demand(path)
        self.wire(wiring.CLAUDE_SKILLS_REL, 'consumer', f'Use `{self.name}`.')
        self.demand(f'{wiring.CLAUDE_SKILLS_REL}/consumer/references/guide.md')
        self.wire(wiring.AGENTS_SKILLS_REL, 'native', f'Use `{self.name}`.')
        errors, reports = self.check()
        self.assertEqual(len(errors), 1, errors)
        for path in surfaces:
            self.assertIn(f'{path}:1', errors[0])
        self.assertIn('consumer/SKILL.md:7', errors[0])
        self.assertIn('consumer/references/guide.md:1', errors[0])
        self.assertIn('native/SKILL.md:7', errors[0])
        self.assertIn('10 demand location(s)', reports[0])

    def test_superseded_projection_is_a_mention_but_available_is_a_demand(self):
        for availability, evidence, expected in (
            ('superseded', 'superseded', 0),
            ('available', 'installed-or-shared-authored', 1),
            ('authored:stub', 'shared-skills:stub', 1),
        ):
            with self.subTest(availability=availability):
                self.write_source([dict(id=self.name, requirement='preferred',
                                        availability=availability, evidence=evidence)])
                errors, _ = self.check()
                self.assertEqual(len(errors), expected, errors)
                if expected:
                    self.assertIn('$.entries[0].skills[0].id', errors[0])

    def test_invalid_source_is_reported(self):
        self.write(wiring.SOURCE_RELATIVE, '{')
        errors, _ = self.check()
        self.assertEqual(len(errors), 1)
        self.assertIn('census unavailable', errors[0])
        self.assertEqual(wiring.run(self.root, verbose=False), 1)

    def test_identifier_boundaries_and_negation_do_not_hide_demand(self):
        self.write('shared/modes/project.md',
                   f'Use `{self.name}-successor`.\nDo not use `{self.name}`. Use `{self.name}`.\n')
        errors, reports = self.check()
        self.assertEqual(len(errors), 1)
        self.assertIn('1 demand location(s)', reports[0])
        self.assertIn('1 mention location(s)', reports[0])

    def test_full_cli_report_does_not_truncate_identifiers(self):
        self.rows = [dict(self.row(f'shared/skills/_retired/old-{i}.md', 'no'), name=f'old-{i}')
                     for i in range(30)]
        self.write_registry()
        self.write('shared/modes/project.md', '\n'.join(f'Use `old-{i}`.' for i in range(30)))
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(wiring.run(self.root), 1)
        self.assertEqual(output.getvalue().count('has no live provider'), 30)
        self.assertIn('30 demanded identifier(s)', output.getvalue())

    def test_domain_checklist_table_is_a_dependency_with_negation_controls(self):
        for heading in ('Skills', 'Per-domain checklists / references', 'Dependencies'):
            for cell, expected in ((f'`{self.name}`', 1),
                                   (f'Do not use `{self.name}`', 0),
                                   (f'`{self.name}` was retired', 0)):
                with self.subTest(heading=heading, cell=cell):
                    self.wire(wiring.CLAUDE_SKILLS_REL, 'consumer',
                              f'| Domain | {heading} |\n|---|---|\n| Example | {cell} |')
                    self.assertEqual(len(self.check()[0]), expected)

    def test_historical_and_plain_reference_table_is_not_a_dependency(self):
        for heading in ('Historical skills', 'References'):
            with self.subTest(heading=heading):
                self.wire(wiring.CLAUDE_SKILLS_REL, 'consumer',
                          f'| Domain | {heading} |\n|---|---|\n| Example | `{self.name}` |')
                self.assertEqual(self.check()[0], [])


class GeminiBridgeContentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.content = (
            b'---\nname: fixture-skill\naudience: specialist\n'
            b'description: Use when checking a materialized bridge fixture for content drift.\n'
            b'---\n\n# Fixture\nCanonical body.\n'
        )
        self.canonical = self.write(wiring.CLAUDE_SKILLS_REL, 'fixture-skill', self.content)
        self.shared = self.write(wiring.AGENTS_SKILLS_REL, 'fixture-skill', self.content)
        self.bridge_home = f'{wiring.GEMINI_BRIDGE_REL}/skills'
        self.bridge = self.write(self.bridge_home, 'fixture-skill', self.content)

    def write(self, home, name, content):
        path = self.root / home / name / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_exact_copy_passes_and_subset_is_allowed(self):
        self.write(wiring.AGENTS_SKILLS_REL, 'unbridged', b'An intentionally unbridged skill.\n')
        self.assertEqual(wiring.check_gemini_bridge(self.root), [])
        self.assertEqual(wiring.run(self.root, verbose=False), 0)

    def test_body_description_audience_and_whitespace_drift_fail_then_pass(self):
        mutations = {
            'body': self.content.replace(b'Canonical body.', b'Stale body.'),
            'description': self.content.replace(b'checking a materialized', b'generating a stale'),
            'missing audience': self.content.replace(b'audience: specialist\n', b''),
            'changed audience': self.content.replace(b'audience: specialist', b'audience: chrono'),
            'whitespace': self.content + b'\n',
            'line endings': self.content.replace(b'\n', b'\r\n'),
        }
        for kind, content in mutations.items():
            with self.subTest(kind=kind):
                self.bridge.write_bytes(content)
                errors = wiring.check_gemini_bridge(self.root)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(str(self.bridge.relative_to(self.root)), errors[0])
                self.assertIn('bytes differ from canonical .claude/skills/fixture-skill/SKILL.md', errors[0])
                self.assertEqual(wiring.run(self.root, verbose=False), 1)
                self.bridge.write_bytes(self.content)
                self.assertEqual(wiring.run(self.root, verbose=False), 0)

    def test_canonical_wins_even_when_shared_and_bridge_share_the_same_drift(self):
        stale = self.content + b'Stale shared and bridge copies.\n'
        self.shared.write_bytes(stale)
        self.bridge.write_bytes(stale)
        errors = wiring.check_gemini_bridge(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('canonical .claude/skills/fixture-skill/SKILL.md', errors[0])

    def test_agents_native_skill_uses_shared_home(self):
        content = b'Native skill content.\n'
        self.write(wiring.AGENTS_SKILLS_REL, 'native', content)
        bridge = self.write(self.bridge_home, 'native', content)
        self.assertEqual(wiring.check_gemini_bridge(self.root), [])
        bridge.write_bytes(content + b'Drift.\n')
        errors = wiring.check_gemini_bridge(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('canonical .agents/skills/native/SKILL.md', errors[0])

    def test_only_named_canary_is_exempt_from_identity(self):
        self.write(wiring.CLAUDE_SKILLS_REL, 'probe-canary', b'Claude canary.\n')
        self.write(wiring.AGENTS_SKILLS_REL, 'probe-canary', b'Shared canary.\n')
        canary = self.write(self.bridge_home, 'probe-canary', b'Gemini canary.\n')
        self.assertNotEqual(canary.read_bytes(), b'Claude canary.\n')
        self.assertEqual(wiring.check_gemini_bridge(self.root), [])
        self.write(wiring.AGENTS_SKILLS_REL, 'probe-canary-other', b'Shared.\n')
        self.write(self.bridge_home, 'probe-canary-other', b'Different.\n')
        errors = wiring.check_gemini_bridge(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('probe-canary-other/SKILL.md: bytes differ', errors[0])

    def test_canary_still_requires_membership(self):
        self.write(self.bridge_home, 'probe-canary', b'Gemini canary.\n')
        errors = wiring.check_gemini_bridge(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('absent from the shared .agents/skills home: probe-canary', errors[0])

    def test_canary_symlink_is_rejected_even_with_matching_membership(self):
        source = self.write(wiring.AGENTS_SKILLS_REL, 'probe-canary', b'Shared canary.\n')
        path = self.root / self.bridge_home / 'probe-canary'
        path.mkdir()
        (path / 'SKILL.md').symlink_to(source)
        errors = wiring.check_gemini_bridge(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('probe-canary/SKILL.md: is a symlink', errors[0])

    def test_read_failure_is_reported_instead_of_escaping_the_validator(self):
        read_bytes = Path.read_bytes

        def unreadable(path):
            if path == self.bridge:
                raise PermissionError('fixture access denied')
            return read_bytes(path)

        with mock.patch.object(Path, 'read_bytes', unreadable):
            errors = wiring.check_gemini_bridge(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('cannot compare bridge content', errors[0])
        self.assertIn('fixture access denied', errors[0])


if __name__ == '__main__':
    unittest.main()
