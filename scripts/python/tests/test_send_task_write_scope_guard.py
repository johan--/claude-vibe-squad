"""Body scope declarations must be resolved before staging a dispatch packet."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "scripts/send-task.sh"
DECLARATION = "Write scope: `path/a`, `path/b`\n"
FIX = ('body declares a write scope but WRITE_SCOPE is not set; '
       'set WRITE_SCOPE="p1, p2"')


class WriteScopeGuardTests(unittest.TestCase):
    def _run(self, body_text=DECLARATION, *, scope=None, override=None,
             source=None, dry_run=False):
        temporary = tempfile.TemporaryDirectory(prefix="write-scope-wrapper-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for directory in ("shared", "bin", "tools", "tmp", "departments/coding/inbox"):
            (root / directory).mkdir(parents=True)
        (root / "shared/lead-windows.sh").write_text(
            "COMPATIBILITY_NAMESPACES=(coding)\n"
            "MODEL_LANES=(gpt-codex claude gemini grok)\n"
            'is_compatibility_namespace() { [[ "$1" == coding ]]; }\n',
            encoding="utf-8",
        )
        body = root / "body.md"
        body.write_text(body_text, encoding="utf-8")
        executables = {
            "bin/send-task.sh": '#!/bin/bash\ncp "$1" "$VAULT_ROOT/departments/coding/inbox/packet.md"\n',
            "tools/uuidgen": "#!/bin/sh\nprintf '12345678-1234-1234-1234-123456789abc\\n'\n",
            "tools/mktemp": '#!/bin/sh\nprintf "staging\\n" >> "$VAULT_ROOT/staging-attempted"\nexec /usr/bin/mktemp "$@"\n',
        }
        for relative, content in executables.items():
            executable = root / relative
            executable.write_text(content, encoding="utf-8")
            executable.chmod(0o755)
        wrapper = WRAPPER
        if source is not None:
            wrapper = root / "send-task.sh"
            wrapper.write_text(source, encoding="utf-8")
        environment = {
            "PATH": f"{root / 'tools'}:/usr/bin:/bin",
            "VAULT_ROOT": str(root),
            "TMPDIR": str(root / "tmp"),
            "REVIEWS": "none",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if scope is not None:
            environment["WRITE_SCOPE"] = scope
        if override is not None:
            environment["WRITE_SCOPE_BODY_ONLY_OK"] = override
        command = ["bash", str(wrapper), "coding", str(body), "backend-engineer",
                   "gpt-codex", "--mode", "modeless"]
        if dry_run:
            command.append("--dry-run")
        result = subprocess.run(command, env=environment, capture_output=True,
                                text=True, check=False, timeout=30)
        return result, root

    def _assert_refused(self, result, root):
        self.assertNotEqual(result.returncode, 0, "body-only scope was accepted")
        self.assertIn(FIX, result.stderr)
        self.assertFalse((root / "staging-attempted").exists())
        self.assertEqual(list((root / "tmp").iterdir()), [])
        self.assertEqual(list((root / "departments/coding/inbox").iterdir()), [])

    def _assert_passed(self, result, root):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((root / "staging-attempted").exists())
        packet = (root / "departments/coding/inbox/packet.md").read_text(encoding="utf-8")
        frontmatter, body = packet.split("\n---\n", 1)
        scope_line = next(line for line in frontmatter.splitlines()
                          if line.startswith("write_scope:"))
        self.assertIn("departments/coding/outbox/TASK-", scope_line)
        return scope_line, body

    def test_declaration_without_scope_is_refused_before_staging(self):
        declarations = (
            DECLARATION,
            "write scope: path/a\n",
            "  **WrItE ScOpE:** path/a\n",
            "- Write scope: path/a\n",
            "\t- **WRITE SCOPE:** path/a\n",
        )
        for declaration in declarations:
            for scope in (None, ""):
                with self.subTest(declaration=declaration, scope=scope):
                    self._assert_refused(*self._run("Task details.\n" + declaration, scope=scope))

    def test_dry_run_is_also_refused_before_staging(self):
        self._assert_refused(*self._run(dry_run=True))

    def test_explicit_scope_passes_and_reaches_frontmatter(self):
        scope, body = self._assert_passed(*self._run(scope="path/a, path/b"))
        self.assertTrue(scope.endswith(", path/a, path/b]"), scope)
        self.assertEqual(body, "\n" + DECLARATION)

    def test_body_without_declaration_passes_with_only_response_scope(self):
        for body_text in ("Inspect the code.\n", "Discuss Write scope: as prose.\n", ""):
            with self.subTest(body=body_text):
                scope, body = self._assert_passed(*self._run(body_text))
                self.assertNotIn(",", scope)
                self.assertEqual(body, "\n" + body_text)

    def test_deliberate_override_passes_without_adding_body_paths(self):
        for scope_input in (None, ""):
            with self.subTest(scope=scope_input):
                scope, body = self._assert_passed(*self._run(scope=scope_input, override="1"))
                self.assertNotIn(",", scope)
                self.assertEqual(body, "\n" + DECLARATION)

    def test_other_override_values_do_not_bypass_refusal(self):
        for override in ("", "0", "true", "01"):
            with self.subTest(override=override):
                self._assert_refused(*self._run(override=override))

    def test_refusal_assertion_detects_guard_removal(self):
        source = WRAPPER.read_text(encoding="utf-8")
        mutated, count = re.subn(
            r'^if \[\[ -z "\$\{WRITE_SCOPE:-\}".*?^fi\n',
            "", source, count=1, flags=re.MULTILINE | re.DOTALL,
        )
        self.assertEqual(count, 1, "mutation must remove exactly one guard")
        result, root = self._run(source=mutated)
        scope, _ = self._assert_passed(result, root)
        self.assertNotIn(",", scope)
        with self.assertRaisesRegex(AssertionError, "body-only scope was accepted"):
            self._assert_refused(result, root)


if __name__ == "__main__":
    unittest.main()
