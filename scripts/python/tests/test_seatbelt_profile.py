from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "python"))

import seatbelt_profile as seatbelt  # noqa: E402
import seatbelt_probe  # noqa: E402
import qualify_macos_build as qualify  # noqa: E402
from seatbelt_probe import (  # noqa: E402
    LaneSmokeResult,
    ProbeEffect,
    ProbeSuiteResult,
    ProviderEgressResult,
    run_probe_suite,
)


VERIFIED_HOST = seatbelt.HostCompatibility(
    sandbox_exec=Path("/usr/bin/sandbox-exec"),
    macos_build="25E253",
    canary_sha256="0" * 64,
)


def compile_for_test(spec: seatbelt.ProfileSpec) -> seatbelt.CompiledProfile:
    return seatbelt.compile_profile(
        spec,
        compatibility_verifier=lambda: VERIFIED_HOST,
    )


class AccountHomeValidationTests(unittest.TestCase):
    def test_synthetic_account_home_derives_all_lexical_lane_roots(self) -> None:
        script = """
import json
import os
import sys
import types

fake_pwd = types.ModuleType("pwd")
fake_pwd.getpwuid = lambda uid: types.SimpleNamespace(
    pw_dir=os.environ["SYNTHETIC_ACCOUNT_HOME"]
)
sys.modules["pwd"] = fake_pwd
import seatbelt_profile

print(json.dumps({
    "host_home": str(seatbelt_profile.HOST_HOME),
    "local_bin_root": str(seatbelt_profile.LOCAL_BIN_ROOT),
    "grok_bin_root": str(seatbelt_profile.GROK_BIN_ROOT),
    "uv_root": str(seatbelt_profile.UV_ROOT),
    "uv_tools_root": str(seatbelt_profile.UV_TOOLS_ROOT),
    "uv_python_root": str(seatbelt_profile.UV_PYTHON_ROOT),
    "claude": str(seatbelt_profile.LANE_CLI_PATHS["claude"]),
    "grok": str(seatbelt_profile.LANE_CLI_PATHS["grok"]),
    "default_path": seatbelt_profile.DEFAULT_LANE_PATH,
}, sort_keys=True))
"""
        with tempfile.TemporaryDirectory() as directory:
            synthetic_home = Path(directory).resolve()
            environment = dict(os.environ)
            environment.update(
                {
                    "HOME": str(synthetic_home),
                    "PYTHONPATH": str(ROOT / "scripts" / "python"),
                    "SYNTHETIC_ACCOUNT_HOME": str(synthetic_home),
                }
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                check=False,
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=environment,
                timeout=10,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        paths = json.loads(completed.stdout)
        local_bin_root = synthetic_home / ".local" / "bin"
        grok_bin_root = synthetic_home / ".grok" / "bin"
        uv_root = synthetic_home / ".local" / "share" / "uv"
        self.assertEqual(paths["host_home"], str(synthetic_home))
        self.assertEqual(paths["local_bin_root"], str(local_bin_root))
        self.assertEqual(paths["grok_bin_root"], str(grok_bin_root))
        self.assertEqual(paths["uv_root"], str(uv_root))
        self.assertEqual(paths["uv_tools_root"], str(uv_root / "tools"))
        self.assertEqual(paths["uv_python_root"], str(uv_root / "python"))
        self.assertEqual(paths["claude"], str(local_bin_root / "claude"))
        self.assertEqual(paths["grok"], str(grok_bin_root / "grok"))
        self.assertTrue(
            paths["default_path"].startswith(f"{grok_bin_root}:{local_bin_root}:")
        )
        maintainer_home = "/" + "Users" + "/chrono"
        self.assertNotIn(maintainer_home, json.dumps(paths, sort_keys=True))

    def test_mismatched_or_relative_home_is_ignored_at_import(self) -> None:
        # $HOME is not authoritative: the passwd euid home is. A sandboxed
        # board spawn runs with a differing (or even relative) $HOME and must
        # still import cleanly, deriving HOST_HOME from the euid record rather
        # than raising. This guards against re-introducing the import-time
        # ProfileValidationError that broke every board launch.
        probe = (
            "import os, pwd, seatbelt_profile as s; "
            "assert str(s.HOST_HOME) == pwd.getpwuid(os.geteuid()).pw_dir, s.HOST_HOME"
        )
        with tempfile.TemporaryDirectory() as directory:
            mismatched_home = str(Path(directory).resolve())
            for environment_home in (mismatched_home, "relative-home"):
                with self.subTest(environment_home=environment_home):
                    environment = dict(os.environ)
                    environment.update(
                        {
                            "HOME": environment_home,
                            "PYTHONPATH": str(ROOT / "scripts" / "python"),
                        }
                    )
                    completed = subprocess.run(
                        [sys.executable, "-c", probe],
                        check=False,
                        capture_output=True,
                        text=True,
                        cwd=str(ROOT),
                        env=environment,
                        timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_account_home_must_be_owned_by_effective_uid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            synthetic_home = Path(directory).resolve()
            with self.assertRaisesRegex(
                seatbelt.ProfileValidationError,
                "owned by the effective UID",
            ):
                seatbelt._validate_account_home(
                    synthetic_home,
                    effective_uid=os.geteuid() + 1,
                    environment_home=str(synthetic_home),
                )

    def test_board_trusted_and_strict_paths_share_the_validated_default(self) -> None:
        source = (ROOT / "bin" / "board-supervisor.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'candidate = os.environ.get("TRUSTED_HOST_PATH", DEFAULT_LANE_PATH)',
            source,
        )
        self.assertIn(
            'environment["PATH"] = _validated_trusted_host_path()',
            source,
        )
        self.assertIn(
            'prepared.environment["PATH"] = DEFAULT_LANE_PATH',
            source,
        )
        self.assertIn(
            "not Path(component).is_absolute()",
            source,
        )
        maintainer_home = "/" + "Users" + "/chrono"
        self.assertNotIn(maintainer_home, source)


class SeatbeltProfileCompilerTests(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: Seatbelt profile rendering uses Darwin profile semantics",
    )
    def test_profile_is_deny_default_and_grants_only_declared_operations(self) -> None:
        compiled = compile_for_test(
            seatbelt.ProfileSpec(
                read_paths=(ROOT / "docs",),
                write_paths=(ROOT / "_state",),
                executable_paths=(Path("/bin/sh"),),
                allow_fork=True,
                broker_port=43123,
            )
        )

        self.assertTrue(compiled.text.startswith("(version 1)\n(deny default)\n"))
        self.assertIn('(allow file-read*', compiled.text)
        self.assertIn('(allow file-write*', compiled.text)
        self.assertIn('(allow process-exec (literal "/bin/sh"))', compiled.text)
        self.assertIn("(allow process-fork)", compiled.text)
        self.assertIn(
            '(allow network-outbound (remote tcp "localhost:43123"))',
            compiled.text,
        )
        self.assertNotIn("127.0.0.1:43123", compiled.text)
        self.assertNotIn("(allow default)", compiled.text)
        self.assertNotIn("(allow network*)", compiled.text)
        self.assertNotIn('(subpath "/")', compiled.text)

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: Seatbelt profile rendering uses Darwin profile semantics",
    )
    def test_profile_hash_is_deterministic_and_scope_bound(self) -> None:
        base = seatbelt.ProfileSpec(
            read_paths=(ROOT / "docs",),
            write_paths=(ROOT / "_state",),
            executable_paths=(Path("/bin/sh"),),
            broker_port=43123,
        )
        reordered = seatbelt.ProfileSpec(
            read_paths=(ROOT / "docs", ROOT / "docs"),
            write_paths=(ROOT / "_state",),
            executable_paths=(Path("/bin/sh"), Path("/bin/sh")),
            broker_port=43123,
        )
        changed = seatbelt.ProfileSpec(
            read_paths=(ROOT / "docs",),
            write_paths=(ROOT / "scripts" / "python",),
            executable_paths=(Path("/bin/sh"),),
            broker_port=43123,
        )

        first = compile_for_test(base)
        second = compile_for_test(base)
        canonical_duplicate = compile_for_test(reordered)
        scope_changed = compile_for_test(changed)

        self.assertEqual(first.text, second.text)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.sha256, canonical_duplicate.sha256)
        self.assertNotEqual(first.sha256, scope_changed.sha256)

    def test_paths_are_realpaths_and_identity_change_is_rejected(self) -> None:
        normalized = seatbelt.normalize_realpath(ROOT / "scripts" / "python")
        self.assertEqual(normalized.path, (ROOT / "scripts" / "python").resolve())

        before = mock.Mock(st_dev=1, st_ino=2, st_mode=0o040755)
        after = mock.Mock(st_dev=1, st_ino=3, st_mode=0o040755)
        stat_fn = mock.Mock(side_effect=(before, after))
        with self.assertRaises(seatbelt.PathIdentityError):
            seatbelt.normalize_realpath(
                ROOT / "scripts" / "python",
                stat_fn=stat_fn,
                realpath_fn=lambda value: str(value),
            )

    def test_invalid_broker_port_is_rejected(self) -> None:
        for port in (0, 65536, True):
            with self.subTest(port=port):
                with self.assertRaises(seatbelt.ProfileValidationError):
                    compile_for_test(
                        seatbelt.ProfileSpec(
                            read_paths=(ROOT / "docs",),
                            write_paths=(ROOT / "_state",),
                            executable_paths=(Path("/bin/sh"),),
                            broker_port=port,
                        )
                    )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: exercises Darwin Seatbelt profile compilation",
    )
    def test_root_read_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            seatbelt.ProfileValidationError,
            "root read scope",
        ):
            compile_for_test(
                seatbelt.ProfileSpec(
                    read_paths=(Path("/"),),
                    write_paths=(ROOT / "_state",),
                    executable_paths=(Path("/bin/sh"),),
                )
            )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: exercises Darwin Seatbelt profile compilation",
    )
    def test_root_write_scope_and_non_executable_grant_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            seatbelt.ProfileValidationError,
            "root write scope",
        ):
            compile_for_test(
                seatbelt.ProfileSpec(
                    write_paths=(Path("/"),),
                    executable_paths=(Path("/bin/sh"),),
                )
            )

        with self.assertRaisesRegex(
            seatbelt.ProfileValidationError,
            "executable",
        ):
            compile_for_test(
                seatbelt.ProfileSpec(
                    write_paths=(ROOT / "_state",),
                    # Any existing non-executable file proves the point. This
                    # named a doc under docs/superpowers/plans/, and when that
                    # directory was deleted the fixture vanished -- so a security
                    # test started failing on a FileNotFoundError that had
                    # nothing to do with executable-bit validation. CLAUDE.md is
                    # load-bearing for the repo and cannot quietly disappear.
                    executable_paths=(ROOT / "CLAUDE.md",),
                )
            )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: Seatbelt profile rendering uses Darwin profile semantics",
    )
    def test_broker_port_is_bound_into_profile_hash(self) -> None:
        common = dict(
            read_paths=(ROOT / "docs",),
            write_paths=(ROOT / "_state",),
            executable_paths=(Path("/bin/sh"),),
        )
        first = compile_for_test(seatbelt.ProfileSpec(**common, broker_port=43123))
        second = compile_for_test(seatbelt.ProfileSpec(**common, broker_port=43124))
        self.assertNotEqual(first.sha256, second.sha256)

    def test_missing_or_renamed_sandbox_exec_fails_closed(self) -> None:
        with self.assertRaises(seatbelt.SandboxCompatibilityError):
            seatbelt.verify_sandbox_compatibility(
                sandbox_exec=ROOT / "not-sandbox-exec",
                build_reader=lambda: "25E253",
            )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: sandbox compatibility check requires the Darwin sandbox-exec host",
    )
    def test_unrecognized_host_build_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            seatbelt.SandboxCompatibilityError,
            "unrecognized macOS build",
        ):
            seatbelt.verify_sandbox_compatibility(
                build_reader=lambda: "UNKNOWN-BUILD",
                runner=mock.Mock(),
            )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: sandbox compatibility check requires the Darwin sandbox-exec host",
    )
    def test_only_individually_qualified_builds_pass_the_gate(self) -> None:
        # Admission is an exact enumeration: a neighbouring build must still
        # refuse, so admission is never a range.
        self.assertEqual(seatbelt.KNOWN_MACOS_BUILDS, frozenset({"25E253", "26A428"}))
        passed = mock.Mock(returncode=0, stdout="", stderr="")
        for build in ("25E253", "26A428"):
            with self.subTest(build=build):
                compatibility = seatbelt.verify_sandbox_compatibility(
                    build_reader=lambda build=build: build,
                    runner=mock.Mock(return_value=passed),
                )
                self.assertEqual(compatibility.macos_build, build)
        with self.assertRaisesRegex(
            seatbelt.SandboxCompatibilityError,
            "unrecognized macOS build",
        ):
            seatbelt.verify_sandbox_compatibility(
                build_reader=lambda: "26A427",
                runner=mock.Mock(return_value=passed),
            )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: sandbox compatibility check requires the Darwin sandbox-exec host",
    )
    def test_failed_sandbox_canary_raises_typed_error(self) -> None:
        failed = mock.Mock(returncode=1, stdout="", stderr="canary denied")
        with self.assertRaisesRegex(
            seatbelt.SandboxCompatibilityError,
            "compatibility canary failed",
        ):
            seatbelt.verify_sandbox_compatibility(
                build_reader=lambda: "25E253",
                runner=mock.Mock(return_value=failed),
            )

    def test_compatibility_failure_emits_no_profile(self) -> None:
        failure = seatbelt.SandboxCompatibilityError("sandbox unavailable")
        verifier = mock.Mock(side_effect=failure)
        with mock.patch.object(seatbelt, "_render_profile") as render:
            with self.assertRaises(seatbelt.SandboxCompatibilityError):
                seatbelt.compile_profile(
                    seatbelt.ProfileSpec(
                        read_paths=(ROOT / "docs",),
                        write_paths=(ROOT / "_state",),
                        executable_paths=(Path("/bin/sh"),),
                        broker_port=43123,
                    ),
                    compatibility_verifier=verifier,
                )
        render.assert_not_called()

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: exercises Darwin Seatbelt profile compilation",
    )
    def test_lane_derived_root_read_is_rejected_before_render(self) -> None:
        with (
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_executable_paths",
                return_value=(),
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_executable_aliases",
                return_value=(),
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_library_grants",
                return_value=((), (), ()),
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_read_paths",
                return_value=(Path("/"),),
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_read_literals",
                return_value=(),
            ),
            self.assertRaisesRegex(
                seatbelt.ProfileValidationError,
                "lane runtime read scope may not be root",
            ),
            seatbelt.scoped_lane_launch_profile(lane="grok"),
        ):
            compile_for_test(
                seatbelt.ProfileSpec(
                    read_paths=(ROOT / "docs",),
                    write_paths=(ROOT / "_state",),
                    executable_paths=(Path("/bin/sh"),),
                )
            )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: exercises Darwin Seatbelt profile compilation",
    )
    def test_lane_profiles_grant_sysctl_read_and_others_do_not(self) -> None:
        # Lane runtimes abort (or lose os.uname) without sysctl-read; the
        # grant must reach every lane and nothing else.
        spec = seatbelt.ProfileSpec(
            write_paths=(ROOT / "_state",),
            executable_paths=(Path("/bin/sh"),),
        )
        with (
            mock.patch.object(
                seatbelt, "installed_lane_cli_executable_paths", return_value=()
            ),
            mock.patch.object(
                seatbelt, "installed_lane_cli_executable_aliases", return_value=()
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_library_grants",
                return_value=((), (), ()),
            ),
            mock.patch.object(
                seatbelt, "installed_lane_cli_read_paths", return_value=()
            ),
            mock.patch.object(
                seatbelt, "installed_lane_cli_read_literals", return_value=()
            ),
        ):
            for lane in sorted(seatbelt.LANE_CLI_PATHS):
                for broker_relay in (False, True):
                    with self.subTest(lane=lane, broker_relay=broker_relay):
                        with seatbelt.scoped_lane_launch_profile(
                            lane=lane, broker_relay=broker_relay
                        ):
                            compiled = compile_for_test(spec)
                        self.assertIn("(allow sysctl-read)", compiled.text)
            with seatbelt.scoped_lane_launch_profile(offline_probe=True):
                offline = compile_for_test(spec)
        self.assertNotIn("sysctl", offline.text)
        self.assertNotIn("sysctl", compile_for_test(spec).text)
        self.assertIn(
            "(allow sysctl-read)",
            compile_for_test(
                seatbelt.ProfileSpec(
                    write_paths=(ROOT / "_state",), allow_sysctl_read=True
                )
            ).text,
        )

    def test_python_cli_reads_exact_bounded_tool_and_python_distribution_roots(
        self,
    ) -> None:
        alias = seatbelt.LOCAL_BIN_ROOT / "grok"
        entrypoint = seatbelt.UV_TOOLS_ROOT / "example-cli" / "bin" / "grok"
        distribution = "cpython-3.13.5-macos-aarch64-none"
        interpreter = (
            seatbelt.UV_PYTHON_ROOT
            / distribution
            / "bin"
            / "python3"
        )

        def fake_realpath(value: object) -> str:
            return str(entrypoint if Path(value) == alias else value)

        with (
            mock.patch.dict(
                seatbelt.LANE_CLI_PATHS,
                {"grok": alias},
                clear=True,
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_executable_paths",
                return_value=(entrypoint, interpreter),
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_library_grants",
                return_value=((), (), ()),
            ),
            mock.patch.object(
                seatbelt,
                "_shebang_interpreter",
                return_value=interpreter,
            ),
            mock.patch.object(
                seatbelt.os.path,
                "realpath",
                side_effect=fake_realpath,
            ),
        ):
            reads = seatbelt.installed_lane_cli_read_paths(
                lanes=("grok",),
                include_offline=False,
            )
            literals = seatbelt.installed_lane_cli_read_literals(
                lanes=("grok",),
                include_offline=False,
            )

        self.assertIn(seatbelt.UV_TOOLS_ROOT / "example-cli", reads)
        self.assertIn(seatbelt.UV_PYTHON_ROOT / distribution, reads)
        self.assertNotIn(Path("/"), reads)
        self.assertNotIn(Path("/"), literals)

    def test_unexpected_uv_relative_interpreter_is_rejected(self) -> None:
        alias = seatbelt.LOCAL_BIN_ROOT / "grok"
        entrypoint = seatbelt.UV_TOOLS_ROOT / "example-cli" / "bin" / "grok"
        interpreter = seatbelt.UV_ROOT / "unreviewed" / "bin" / "python3"

        def fake_realpath(value: object) -> str:
            return str(entrypoint if Path(value) == alias else value)

        with (
            mock.patch.dict(
                seatbelt.LANE_CLI_PATHS,
                {"grok": alias},
                clear=True,
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_executable_paths",
                return_value=(entrypoint, interpreter),
            ),
            mock.patch.object(
                seatbelt,
                "installed_lane_cli_library_grants",
                return_value=((), (), ()),
            ),
            mock.patch.object(
                seatbelt,
                "_shebang_interpreter",
                return_value=interpreter,
            ),
            mock.patch.object(
                seatbelt.os.path,
                "realpath",
                side_effect=fake_realpath,
            ),
            self.assertRaisesRegex(
                seatbelt.ProfileValidationError,
                "outside the reviewed Python subtree",
            ),
        ):
            seatbelt.installed_lane_cli_read_paths(
                lanes=("grok",),
                include_offline=False,
            )


SUCCESS = ProbeEffect(True, 0, None, "", "", True)
DENIED = ProbeEffect(False, 73, errno.EPERM, "", "", False, elapsed_ms=0)


def passing_suite(**overrides: ProbeEffect) -> ProbeSuiteResult:
    fields = dict(
        fixture_root="fixture",
        deny_profile_sha256="a" * 64,
        broker_profile_sha256="b" * 64,
        declared_write=SUCCESS,
        undeclared_write=DENIED,
        network_deny=DENIED,
        exact_broker_port=SUCCESS,
        wrong_broker_port=DENIED,
        allowed_read=ProbeEffect(
            True, 0, None, "", "", True,
            content=seatbelt_probe._READ_FIXTURE_CONTENT,
        ),
        denied_read=ProbeEffect(False, 73, errno.EPERM, "", "", True),
        allowed_exec=ProbeEffect(
            True, 0, None, seatbelt_probe._EXEC_MARKER + "\n", "", True
        ),
        denied_exec=ProbeEffect(False, 75, errno.EPERM, "", "", True),
        nonloopback_deny=DENIED,
        nonloopback_unsandboxed_control=ProbeEffect(
            False, 73, errno.ETIMEDOUT, "", "", None, elapsed_ms=1500
        ),
    )
    fields.update(overrides)
    return ProbeSuiteResult(**fields)


def passing_egress(**overrides: object) -> ProviderEgressResult:
    fields = dict(
        provider_host="api.example.test",
        provider_address="192.0.2.10",
        egress_rule=seatbelt_probe.PROVIDER_EGRESS_RULE,
        deny_profile_sha256="c" * 64,
        egress_profile_sha256="d" * 64,
        provider_connect=ProbeEffect(True, 0, None, "", "", None, elapsed_ms=40),
        provider_deny_control=DENIED,
        egress_other_port_denied=DENIED,
        host_scoped_rule_returncode=65,
        host_scoped_rule_stderr="host must be * or localhost",
    )
    fields.update(overrides)
    return ProviderEgressResult(**fields)


def passing_lane(**overrides: object) -> LaneSmokeResult:
    fields = dict(
        lane="grok",
        command=("/lane/grok", "--version"),
        profile_sha256="e" * 64,
        canary_passed=True,
        canary_details=(),
        returncode=0,
        stdout="grok, version 1\n",
        stderr="",
    )
    fields.update(overrides)
    return LaneSmokeResult(**fields)


class ProbeHarnessResultTests(unittest.TestCase):
    def test_probe_report_passes_only_when_every_effect_matches(self) -> None:
        report = passing_suite()
        self.assertTrue(report.passed)
        self.assertFalse(
            passing_suite(
                wrong_broker_port=ProbeEffect(True, 0, None, "", "")
            ).passed
        )

    def test_each_new_effect_fails_the_suite_on_its_own(self) -> None:
        content = seatbelt_probe._READ_FIXTURE_CONTENT
        marker = seatbelt_probe._EXEC_MARKER
        cases = {
            "allowed read returned other bytes": dict(
                allowed_read=ProbeEffect(True, 0, None, "", "", True, content="x")
            ),
            "denied read succeeded": dict(
                denied_read=ProbeEffect(True, 0, None, "", "", True, content=content)
            ),
            "denied read failed with a non-sandbox errno": dict(
                denied_read=ProbeEffect(False, 73, errno.ENOENT, "", "", True)
            ),
            "denied read target absent (no positive control)": dict(
                denied_read=ProbeEffect(False, 73, errno.EPERM, "", "", False)
            ),
            "allowed exec did not run the target": dict(
                allowed_exec=ProbeEffect(True, 0, None, "", "", True)
            ),
            "denied exec ran the target": dict(
                denied_exec=ProbeEffect(True, 0, None, marker, "", True)
            ),
            "denied exec failed with a non-sandbox errno": dict(
                denied_exec=ProbeEffect(False, 75, errno.ENOENT, "", "", True)
            ),
            "denied exec target not executable (no positive control)": dict(
                denied_exec=ProbeEffect(False, 75, errno.EPERM, "", "", False)
            ),
            "non-loopback connect succeeded": dict(
                nonloopback_deny=ProbeEffect(True, 0, None, "", "", elapsed_ms=5)
            ),
            "non-loopback denial timed out instead of EPERM": dict(
                nonloopback_deny=ProbeEffect(
                    False, 73, errno.ETIMEDOUT, "", "", elapsed_ms=1500
                )
            ),
            "non-loopback EPERM arrived only after network wait": dict(
                nonloopback_deny=ProbeEffect(
                    False, 73, errno.EPERM, "", "", elapsed_ms=1400
                )
            ),
            "non-loopback denial has no timing": dict(
                nonloopback_deny=ProbeEffect(False, 73, errno.EPERM, "", "")
            ),
            "host stack itself answers EPERM (control fails)": dict(
                nonloopback_unsandboxed_control=ProbeEffect(
                    False, 73, errno.EPERM, "", "", elapsed_ms=0
                )
            ),
        }
        for label, override in cases.items():
            with self.subTest(label):
                self.assertFalse(passing_suite(**override).passed)

    def test_group_properties_isolate_their_clause(self) -> None:
        report = passing_suite(
            denied_exec=ProbeEffect(True, 0, None, "", "", True)
        )
        self.assertFalse(report.execs_passed)
        self.assertTrue(report.reads_passed)
        self.assertTrue(report.writes_passed)
        self.assertTrue(report.loopback_passed)
        self.assertTrue(report.arbitrary_network_denial_passed)

    def test_provider_egress_needs_connect_and_both_denial_controls(self) -> None:
        self.assertTrue(passing_egress().passed)
        cases = {
            "connect failed": dict(provider_connect=DENIED),
            "no-network profile also connected": dict(
                provider_deny_control=ProbeEffect(True, 0, None, "", "")
            ),
            "grant not port-scoped": dict(
                egress_other_port_denied=ProbeEffect(True, 0, None, "", "")
            ),
            "other-port refusal was not the sandbox": dict(
                egress_other_port_denied=ProbeEffect(
                    False, 73, errno.ECONNREFUSED, "", ""
                )
            ),
        }
        for label, override in cases.items():
            with self.subTest(label):
                self.assertFalse(passing_egress(**override).passed)

    def test_lane_smoke_needs_canary_clean_exit_and_output(self) -> None:
        self.assertTrue(passing_lane().passed)
        cases = {
            "crashed": dict(returncode=-5),
            "canary failed": dict(canary_passed=False),
            "no output": dict(stdout=""),
            "launch raised": dict(returncode=None, error="HygieneError: x"),
        }
        for label, override in cases.items():
            with self.subTest(label):
                self.assertFalse(passing_lane(**override).passed)

    def test_effect_parses_exact_errno_and_timing(self) -> None:
        denied = subprocess.CompletedProcess(
            [], 73, '{"stage":"connect","errno":1,"elapsed_ms":0}\n', ""
        )
        effect = seatbelt_probe._effect(denied)
        self.assertFalse(effect.succeeded)
        self.assertEqual(effect.errno, errno.EPERM)
        self.assertEqual(effect.elapsed_ms, 0)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file.txt"
            target.write_text("payload", encoding="utf-8")
            read = seatbelt_probe._read_effect(
                subprocess.CompletedProcess([], 0, '{"bytes":7}\npayload', ""),
                target=target,
            )
            self.assertEqual(read.content, "payload")
            self.assertTrue(read.target_exists)
            refused = seatbelt_probe._read_effect(
                subprocess.CompletedProcess(
                    [], 73, '{"stage":"open","errno":1,"elapsed_ms":0}\n', ""
                ),
                target=target,
            )
            self.assertIsNone(refused.content)
            self.assertEqual(refused.errno, errno.EPERM)


class QualifyMacosBuildTests(unittest.TestCase):
    def test_candidate_is_admitted_in_process_only(self) -> None:
        before = seatbelt.KNOWN_MACOS_BUILDS
        verifier = qualify.candidate_verifier("27Z999")
        self.assertIn("27Z999", verifier.keywords["allowed_builds"])
        self.assertTrue(before <= verifier.keywords["allowed_builds"])
        self.assertIs(seatbelt.KNOWN_MACOS_BUILDS, before)
        self.assertNotIn("27Z999", seatbelt.KNOWN_MACOS_BUILDS)
        source = (ROOT / "scripts" / "python" / "qualify_macos_build.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("seatbelt_profile.py\").write", source)
        self.assertNotIn("KNOWN_MACOS_BUILDS =", source)

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS-only: sandbox compatibility check requires the Darwin sandbox-exec host",
    )
    def test_candidate_verifier_passes_candidate_and_refuses_neighbours(self) -> None:
        passed = mock.Mock(returncode=0, stdout="", stderr="")
        verifier = qualify.candidate_verifier("27Z999")
        compatibility = verifier(
            build_reader=lambda: "27Z999", runner=mock.Mock(return_value=passed)
        )
        self.assertEqual(compatibility.macos_build, "27Z999")
        with self.assertRaisesRegex(
            seatbelt.SandboxCompatibilityError, "unrecognized macOS build"
        ):
            verifier(
                build_reader=lambda: "27Z998", runner=mock.Mock(return_value=passed)
            )

    def test_qualification_refuses_a_host_not_running_the_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = Path(directory) / "fixtures"
            with self.assertRaisesRegex(
                qualify.QualificationError, "must be qualified on a host"
            ):
                qualify.qualify(
                    "27Z999",
                    fixture_root=fixture_root,
                    lanes=("grok",),
                    provider_host="api.example.test",
                    build_reader=lambda: "26A428",
                )
            self.assertFalse(fixture_root.exists())

    def test_items_cover_every_section_four_clause(self) -> None:
        items = qualify.build_items(
            compatibility=VERIFIED_HOST,
            compatibility_error=None,
            suite=passing_suite(),
            egress=passing_egress(),
            lanes=[passing_lane()],
        )
        self.assertEqual(tuple(items), qualify.ITEM_NAMES)
        self.assertTrue(all(item["passed"] for item in items.values()))
        self.assertEqual(
            items["reads"]["evidence"]["denied_read"]["errno"],
            errno.EPERM,
        )

        crashed = qualify.build_items(
            compatibility=VERIFIED_HOST,
            compatibility_error=None,
            suite=passing_suite(),
            egress=passing_egress(),
            lanes=[passing_lane(), passing_lane(lane="claude", returncode=-5)],
        )
        self.assertFalse(crashed["lane_smoke"]["passed"])
        self.assertFalse(crashed["lane_smoke"]["evidence"]["claude"]["passed"])
        empty = qualify.build_items(
            compatibility=VERIFIED_HOST,
            compatibility_error=None,
            suite=passing_suite(),
            egress=passing_egress(),
            lanes=[],
        )
        self.assertFalse(empty["lane_smoke"]["passed"])

    def test_report_binds_build_hashes_timestamp_and_fails_on_any_item(self) -> None:
        items = qualify.build_items(
            compatibility=VERIFIED_HOST,
            compatibility_error=None,
            suite=passing_suite(denied_read=SUCCESS),
            egress=passing_egress(),
            lanes=[passing_lane()],
        )
        report = qualify.build_report(
            candidate_build="27Z999",
            host_build="27Z999",
            product_version="28.0",
            sandbox_exec_sha256="f" * 64,
            items=items,
            profiles={"deny": "a" * 64, "lane:grok": "e" * 64},
            fixture_root="fixture",
            qualified_at="2026-09-27T00:00:00Z",
        )
        self.assertEqual(report["schema"], qualify.SCHEMA)
        self.assertEqual(report["candidate_build"], "27Z999")
        self.assertEqual(report["qualified_at"], "2026-09-27T00:00:00Z")
        self.assertEqual(report["profile_sha256"]["lane:grok"], "e" * 64)
        self.assertEqual(report["sandbox_exec_sha256"], "f" * 64)
        self.assertFalse(report["already_admitted"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_items"], ["reads"])
        json.dumps(report)

    def test_failed_canary_still_writes_a_failing_report(self) -> None:
        def refuse() -> seatbelt.HostCompatibility:
            raise seatbelt.SandboxCompatibilityError("canary failed: denied")

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(qualify, "candidate_verifier", return_value=refuse),
            mock.patch.object(qualify, "_product_version", return_value="28.0"),
            mock.patch.object(qualify, "_sha256_file", return_value="f" * 64),
            mock.patch.object(seatbelt_probe, "run_probe_suite") as suite,
        ):
            report = qualify.qualify(
                "27Z999",
                fixture_root=Path(directory) / "fixtures",
                lanes=("grok",),
                provider_host="api.example.test",
                build_reader=lambda: "27Z999",
            )
        suite.assert_not_called()
        self.assertFalse(report["passed"])
        self.assertEqual(report["failed_items"], sorted(qualify.ITEM_NAMES))
        self.assertIn(
            "canary failed",
            report["items"]["compatibility_canary"]["evidence"]["error"],
        )

    def test_main_exit_codes_and_atomic_output(self) -> None:
        def report(passed: bool) -> dict[str, object]:
            return {"passed": passed, "failed_items": [] if passed else ["reads"]}

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            arguments = [
                "--candidate-build", "27Z999",
                "--output", str(output),
                "--fixture-root", str(Path(directory) / "fixtures"),
                "--lane", "grok",
            ]
            for passed, expected in ((True, 0), (False, 1)):
                with (
                    self.subTest(passed=passed),
                    mock.patch.object(qualify, "qualify", return_value=report(passed)),
                    mock.patch("sys.stdout"),
                    mock.patch("sys.stderr"),
                ):
                    self.assertEqual(qualify.main(arguments), expected)
                    self.assertEqual(
                        json.loads(output.read_text(encoding="utf-8"))["passed"],
                        passed,
                    )
            self.assertEqual(
                sorted(path.name for path in Path(directory).iterdir()),
                ["result.json"],
            )
            output.unlink()
            with (
                mock.patch.object(
                    qualify,
                    "qualify",
                    side_effect=qualify.QualificationError("wrong host"),
                ),
                mock.patch("sys.stderr"),
            ):
                self.assertEqual(qualify.main(arguments), 2)
            self.assertFalse(output.exists())


@unittest.skipUnless(
    sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
    "real Seatbelt probes require macOS sandbox-exec",
)
class RealSeatbeltProbeTests(unittest.TestCase):
    def test_declared_write_denied_write_and_loopback_port_invariants(self) -> None:
        fixture_root = (
            ROOT
            / "_state"
            / "v2-finalization-2026-07-21-build"
            / "probe-fixtures"
            / f"unittest-{time.time_ns()}"
        )
        real_subprocess_run = subprocess.run

        def run_with_compiler_temp(*args: object, **kwargs: object):
            command = args[0] if args else kwargs.get("args")
            if (
                isinstance(command, (list, tuple))
                and command
                and command[0] == "/usr/bin/clang"
            ):
                environment = dict(kwargs.get("env") or {})
                environment["TMPDIR"] = tempfile.gettempdir()
                kwargs["env"] = environment
            return real_subprocess_run(*args, **kwargs)

        # Managed test runners may deny Darwin's confstr-backed temp lookup.
        # Give only the retained probe compiler a known writable temp root;
        # the compiled Seatbelt profiles and every effect probe remain real.
        with mock.patch.object(
            seatbelt_probe.subprocess,
            "run",
            side_effect=run_with_compiler_temp,
        ):
            result = run_probe_suite(fixture_root)

        self.assertTrue(result.declared_write.succeeded)
        self.assertTrue(result.declared_write.target_exists)
        self.assertFalse(result.undeclared_write.succeeded)
        self.assertEqual(result.undeclared_write.errno, errno.EPERM)
        self.assertFalse(result.undeclared_write.target_exists)
        self.assertFalse(result.network_deny.succeeded)
        self.assertEqual(result.network_deny.errno, errno.EPERM)
        self.assertTrue(result.exact_broker_port.succeeded)
        self.assertFalse(result.wrong_broker_port.succeeded)
        self.assertEqual(result.wrong_broker_port.errno, errno.EPERM)

        self.assertTrue(result.allowed_read.succeeded)
        self.assertEqual(
            result.allowed_read.content, seatbelt_probe._READ_FIXTURE_CONTENT
        )
        self.assertFalse(result.denied_read.succeeded)
        self.assertEqual(result.denied_read.errno, errno.EPERM)
        self.assertIsNone(result.denied_read.content)
        self.assertTrue(result.denied_read.target_exists)

        self.assertTrue(result.allowed_exec.succeeded)
        self.assertIn(seatbelt_probe._EXEC_MARKER, result.allowed_exec.stdout)
        self.assertFalse(result.denied_exec.succeeded)
        self.assertEqual(result.denied_exec.errno, errno.EPERM)
        self.assertNotIn(seatbelt_probe._EXEC_MARKER, result.denied_exec.stdout)

        self.assertFalse(result.nonloopback_deny.succeeded)
        self.assertEqual(result.nonloopback_deny.errno, errno.EPERM)
        self.assertLess(result.nonloopback_deny.elapsed_ms, 1000)
        self.assertNotEqual(
            result.nonloopback_unsandboxed_control.errno, errno.EPERM
        )
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
