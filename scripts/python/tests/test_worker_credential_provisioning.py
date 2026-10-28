from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace
from typing import Any
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "bin" / "board-supervisor.sh"
RESEARCH_ARSENAL = ROOT / "plugins" / "chrono-research-arsenal" / "mcp_server.py"
CAPABILITY_SOURCE = ROOT / "model-lanes" / "specialist-lane-capabilities.v1.json"
sys.path.insert(0, str(ROOT / "scripts" / "python"))

from lane_capability_enforcement import (  # noqa: E402
    RESEARCH_API_KEY_NAMES,
    load_tool_classes,
    partition_absent_mcps,
)
import worktree_isolation as wti  # noqa: E402


def balanced_call(source: str, marker: str) -> str:
    """Return one complete call expression starting at ``marker``.

    The supervisor is a bash file with an embedded Python program, so it cannot
    be parsed whole. Slicing a call by paren balance lets a test EXECUTE the
    real expression instead of asserting on its text, which is the difference
    between pinning behavior and pinning a string.
    """

    start = source.index(marker)
    depth = 0
    for offset in range(source.index("(", start), len(source)):
        character = source[offset]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return source[start : offset + 1]
    raise AssertionError(f"unbalanced call for marker {marker!r}")


# Hermetic: no system/global config, no credential helper, no terminal prompt.
# One home for these four settings — a second copy is how one fixture quietly
# starts reading the operator's real ~/.gitconfig.
FIXTURE_GIT_ENV = MappingProxyType(
    {
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
)


def run_git(
    case: unittest.TestCase,
    *args: str,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one git command against a throwaway fixture repository."""

    completed = subprocess.run(
        ["/usr/bin/git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=dict(FIXTURE_GIT_ENV),
    )
    if check and completed.returncode != 0:
        case.fail(
            f"git {args!r} failed with {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return completed


def make_fixture_repo(case: unittest.TestCase, root: Path) -> Path:
    """Initialise a committed one-file repository on branch ``v2``."""

    repo = root / "repo"
    repo.mkdir()
    run_git(case, "init", "-q", "-b", "v2", cwd=repo)
    run_git(case, "config", "user.email", "test@example.com", cwd=repo)
    run_git(case, "config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    run_git(case, "add", "README.md", cwd=repo)
    run_git(case, "commit", "-q", "-m", "fixture", cwd=repo)
    return repo


def add_linked_worktree(case: unittest.TestCase, repo: Path, root: Path) -> Path:
    """Register a linked worktree that predates any WorktreePool instance."""

    run_git(
        case,
        "worktree",
        "add",
        "-q",
        "-b",
        f"worktree/existing/{root.name}",
        str(root),
        "v2",
        cwd=repo,
    )
    return root


def credential_status(
    case: unittest.TestCase, worktree_root: Path
) -> subprocess.CompletedProcess[str]:
    return run_git(
        case,
        "status",
        "--porcelain",
        "--untracked-files=normal",
        "--",
        ".role-capabilities",
        cwd=worktree_root,
    )


def check_ignore(
    case: unittest.TestCase, worktree_root: Path
) -> subprocess.CompletedProcess[str]:
    return run_git(
        case,
        "check-ignore",
        "-v",
        "--",
        ".role-capabilities",
        cwd=worktree_root,
        check=False,
    )


def plant_credential_config(worktree_root: Path) -> Path:
    config = worktree_root / ".role-capabilities" / "kimi-mcp.json"
    config.parent.mkdir()
    config.write_text("synthetic config\n", encoding="utf-8")
    return config


def strip_exclusion_rule(repo: Path) -> None:
    """Remove the containment rule from the common exclude, leaving the rest."""

    exclude_path = repo / ".git" / "info" / "exclude"
    try:
        original = exclude_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    retained = [
        line for line in original.splitlines() if line != "/.role-capabilities/"
    ]
    exclude_path.write_text(
        ("\n".join(retained) + "\n") if retained else "", encoding="utf-8"
    )


class WorkerCredentialProvisioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.supervisor_source = SUPERVISOR.read_text(encoding="utf-8")
        cls.research_arsenal_source = RESEARCH_ARSENAL.read_text(encoding="utf-8")

    def _credential_functions(self):
        start = self.supervisor_source.index("MANAGED_CREDENTIAL_NAMES = (")
        end = self.supervisor_source.index("\n\ntrusted_environment =", start)
        namespace = {
            "MappingProxyType": MappingProxyType,
            "load_solodit_api_key": mock.Mock(return_value="synthetic-solodit"),
            "load_grok_api_key": mock.Mock(return_value="synthetic-xai"),
            "load_github_mcp_token": mock.Mock(return_value="synthetic-github"),
        }
        exec(compile(self.supervisor_source[start:end], "board-supervisor.sh", "exec"), namespace)
        return namespace

    def _scrubbed_environment(self, worker_lane, ambient):
        start = self.supervisor_source.index("def trusted_worker_environment(")
        end = self.supervisor_source.index("\n\n", self.supervisor_source.index("    return environment", start))
        namespace = {
            "os": os,
            "Path": Path,
            "attempt_id": None,
            "_validated_trusted_host_path": lambda: "/usr/bin:/bin",
        }
        exec(compile(self.supervisor_source[start:end], "board-supervisor.sh", "exec"), namespace)
        with mock.patch.dict(os.environ, ambient, clear=True):
            return namespace["trusted_worker_environment"](worker_lane)

    def test_grok_loader_reads_only_xai_and_rejects_invalid_values(self) -> None:
        start = self.supervisor_source.index("def load_grok_api_key():")
        end = self.supervisor_source.index("\n\ndef load_github_mcp_token", start)
        namespace = {"os": os, "subprocess": subprocess}
        exec(compile(self.supervisor_source[start:end], "board-supervisor.sh", "exec"), namespace)
        with (
            mock.patch.dict(os.environ, {"HOME": "/synthetic/home"}, clear=True),
            mock.patch.object(subprocess, "run") as run,
        ):
            run.return_value = SimpleNamespace(returncode=0, stdout="synthetic-xai")
            self.assertEqual(namespace["load_grok_api_key"](), "synthetic-xai")
            command = run.call_args.args[0][-1]
            self.assertIn("XAI_API_KEY", command)
            for retired in ("PERPLEXITY_API_KEY", "FIRECRAWL_API_KEY"):
                self.assertNotIn(retired, command)
            self.assertEqual(run.call_args.kwargs["env"], {
                "HOME": "/synthetic/home", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            })
            for value in ("", "synthetic\n", "synthetic\t", "synthetic\x00", "x" * 16385):
                with self.subTest(value_length=len(value)):
                    run.return_value = SimpleNamespace(returncode=0, stdout=value)
                    self.assertIsNone(namespace["load_grok_api_key"]())
            run.return_value = SimpleNamespace(returncode=1, stdout="synthetic-xai")
            self.assertIsNone(namespace["load_grok_api_key"]())
            run.side_effect = OSError("synthetic unavailable store")
            self.assertIsNone(namespace["load_grok_api_key"]())

    def test_arxiv_needs_no_credentials_and_grok_snapshot_is_reused(self) -> None:
        namespace = self._credential_functions()
        load = namespace["load_managed_credentials"]
        project = namespace["project_worker_credentials"]
        for worker_lane in ("claude", "codex", "gemini", "kimi", "unsupported"):
            with self.subTest(worker_lane=worker_lane):
                snapshot, missing = load(worker_lane, ["chrono-research-arsenal"])
                self.assertEqual(dict(snapshot), {})
                self.assertEqual(missing, ())
        for loader in ("load_solodit_api_key", "load_grok_api_key", "load_github_mcp_token"):
            namespace[loader].assert_not_called()

        snapshot, missing = load("grok", [])
        self.assertEqual(dict(snapshot), {"XAI_API_KEY": "synthetic-xai"})
        self.assertEqual(missing, ())
        with self.assertRaises(TypeError):
            snapshot["XAI_API_KEY"] = "replacement"
        namespace["load_grok_api_key"].return_value = None
        base = {"PATH": "/usr/bin:/bin", "XAI_API_KEY": "ambient-xai"}
        health = project(base, snapshot)
        self.assertEqual(health, {"PATH": "/usr/bin:/bin", "XAI_API_KEY": "synthetic-xai"})
        self.assertEqual(project(health, snapshot), health)
        namespace["load_grok_api_key"].assert_called_once_with()
        self.assertEqual(base["XAI_API_KEY"], "ambient-xai")
        self.assertEqual(project(base, MappingProxyType({})), {"PATH": "/usr/bin:/bin"})
        missing_snapshot, missing = load("grok", ["chrono-research-arsenal"])
        self.assertEqual(dict(missing_snapshot), {})
        self.assertEqual(missing, ("XAI_API_KEY",))

    def test_each_lane_projects_only_credentials_for_authorized_mcps(self) -> None:
        namespace = self._credential_functions()
        load = namespace["load_managed_credentials"]
        project = namespace["project_worker_credentials"]
        supplied = {
            "SOLODIT_API_KEY": "synthetic-solodit",
            "XAI_API_KEY": "synthetic-xai",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "synthetic-github",
        }
        retired = {"PERPLEXITY_API_KEY", "FIRECRAWL_API_KEY"}
        ambient = {
            "HOME": "/synthetic/home",
            **{name: f"ambient-{name.lower()}" for name in set(supplied) | retired},
        }
        cases = (
            ((), set()),
            (("guarded-solodit",), {"SOLODIT_API_KEY"}),
            (("chrono-research-arsenal",), set()),
            (("github",), {"GITHUB_PERSONAL_ACCESS_TOKEN"}),
            (("guarded-solodit", "chrono-research-arsenal", "github"),
             {"SOLODIT_API_KEY", "GITHUB_PERSONAL_ACCESS_TOKEN"}),
        )
        for worker_lane in ("claude", "codex", "gemini", "grok", "kimi", "unsupported"):
            base = self._scrubbed_environment(worker_lane, ambient)
            self.assertEqual(base["HOME"], ambient["HOME"])
            self.assertFalse((set(supplied) | retired).intersection(base))
            for authorized_mcps, mcp_names in cases:
                with self.subTest(worker_lane=worker_lane, authorized_mcps=authorized_mcps):
                    expected = ({"XAI_API_KEY"} if worker_lane == "grok" else
                                set() if worker_lane in {"kimi", "unsupported"} else mcp_names)
                    snapshot, missing = load(worker_lane, authorized_mcps)
                    # Still scrub live managed names even if a caller adds an ambient value.
                    projected = project({**base, **{name: "ambient" for name in supplied}}, snapshot)
                    self.assertEqual(missing, ())
                    self.assertEqual(dict(snapshot), {name: supplied[name] for name in expected})
                    self.assertEqual(projected, {**base, **dict(snapshot)})
                    self.assertFalse(retired.intersection(projected))

        # Missing credentials remain observable for live MCPs, never for arXiv.
        for loader in ("load_solodit_api_key", "load_grok_api_key", "load_github_mcp_token"):
            namespace[loader].return_value = None
        for worker_lane in ("claude", "codex", "gemini"):
            snapshot, missing = load(worker_lane, ["guarded-solodit", "github", "chrono-research-arsenal"])
            self.assertEqual(dict(snapshot), {})
            self.assertEqual(missing, ("GITHUB_PERSONAL_ACCESS_TOKEN", "SOLODIT_API_KEY"))

    def test_arxiv_search_requests_relevance_ordering(self) -> None:
        start = self.research_arsenal_source.index("@mcp.tool()\ndef arxiv_search(")
        end = self.research_arsenal_source.index(
            '\n\n\nif __name__ == "__main__":', start
        )
        captured: dict[str, Any] = {}

        class FakeMcp:
            @staticmethod
            def tool():
                return lambda function: function

        class FakeClient:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            @staticmethod
            def results(_search: Any) -> tuple[Any, ...]:
                return ()

        def fake_search(**kwargs: Any) -> object:
            captured.update(kwargs)
            return object()

        relevance = object()
        submitted_date = object()
        descending = object()
        fake_arxiv = SimpleNamespace(
            Client=FakeClient,
            Search=fake_search,
            SortCriterion=SimpleNamespace(
                Relevance=relevance,
                SubmittedDate=submitted_date,
            ),
            SortOrder=SimpleNamespace(Descending=descending),
            HTTPError=RuntimeError,
        )
        namespace = {
            "Any": Any,
            "arxiv": fake_arxiv,
            "mcp": FakeMcp(),
            "_ok": lambda payload: {"ok": True, "result": payload},
            "_err": lambda reason, **extra: {
                "ok": False,
                "error": reason,
                **extra,
            },
            "_openalex_fallback": lambda *_args: {},
        }
        exec(
            compile(
                self.research_arsenal_source[start:end],
                "mcp_server.py",
                "exec",
            ),
            namespace,
        )

        response = namespace["arxiv_search"](
            "vector databases", max_results=3, categories=["cs.IR"]
        )

        self.assertTrue(response["ok"])
        self.assertEqual(captured["query"], "(cat:cs.IR) AND (vector databases)")
        self.assertIs(captured["sort_by"], relevance)
        self.assertIsNot(captured["sort_by"], submitted_date)
        self.assertIs(captured["sort_order"], descending)

    def test_codex_config_does_not_add_research_credentials(self) -> None:
        start = self.supervisor_source.index("def _toml_table_header(")
        end = self.supervisor_source.index("\n\ndef _validated_trusted_host_path", start)
        namespace = {"re": re, "json": json, "os": os, "GUARDED_MCP_PREFIX": "guarded-"}
        exec(compile(self.supervisor_source[start:end], "board-supervisor.sh", "exec"), namespace)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base"
            base.mkdir()
            config = ('[mcp_servers.chrono-research-arsenal]\ncommand = "/usr/bin/false"\n'
                      '[mcp_servers.chrono-vault]\ncommand = "/usr/bin/false"\n')
            (base / "config.toml").write_text(config)
            overlay = root / "overlay.toml"
            overlay.write_text('[mcp_servers.guarded-solodit]\ncommand = "/usr/bin/false"\n')
            ambient = {name: f"synthetic-{name}" for name in RESEARCH_API_KEY_NAMES}
            with mock.patch.dict(os.environ, ambient, clear=True):
                added = namespace["_prepare_codex_home"](base, overlay, root / "spawn", "synthetic-context")
            rendered = (root / "spawn/config.toml").read_text()
            self.assertEqual(added, ["guarded-solodit"])
            self.assertIn('CHRONO_VAULT_CONTEXT = "synthetic-context"', rendered)
            self.assertIn('[mcp_servers.chrono-research-arsenal]', rendered)
            for name, value in ambient.items():
                self.assertNotIn(name, rendered)
                self.assertNotIn(value, rendered)
            self.assertEqual((base / "config.toml").read_text(), config)

    def test_codex_gate_enables_authorized_servers_without_forwarding_research_keys(self) -> None:
        start = self.supervisor_source.index("    for server_name in configured_mcps:")
        end = self.supervisor_source.index("    # F6:", start)
        import textwrap
        namespace = {
            "re": re,
            "configured_mcps": ["chrono-research-arsenal", "github"],
            "authorized_mcps": ["chrono-research-arsenal"],
            "capability_lane_args": [],
            "deny": self.fail,
        }
        exec(compile(textwrap.dedent(self.supervisor_source[start:end]), "board-supervisor.sh", "exec"), namespace)
        self.assertEqual(namespace["capability_lane_args"], [
            "-c", "mcp_servers.chrono-research-arsenal.enabled=true",
            "-c", 'mcp_servers.github={enabled=false,command="/usr/bin/false"}',
        ])

    def test_bounty_researcher_preferred_github_absence_degrades(self) -> None:
        classes = load_tool_classes(
            repo_root=ROOT,
            lane="codex",
            specialist="bounty-researcher",
        )
        mcp_classes = {
            name.removeprefix("mcp:"): details
            for name, details in classes.items()
            if name.startswith("mcp:")
        }
        blocking, degraded = partition_absent_mcps(
            authorized=["chrono-vault", "github"],
            configured=["chrono-vault"],
            mcp_classes=mcp_classes,
        )
        self.assertEqual(blocking, ())
        self.assertEqual(degraded, ("github",))

    def test_required_or_unknown_mcp_absence_still_blocks(self) -> None:
        for classes in (
            {"github": {"requirement": "required"}},
            {"github": {"requirement": "unexpected"}},
            {},
        ):
            with self.subTest(classes=classes):
                blocking, degraded = partition_absent_mcps(
                    authorized=["github"],
                    configured=[],
                    mcp_classes=classes,
                )
                self.assertEqual(blocking, ("github",))
                self.assertEqual(degraded, ())

    def test_supervisor_does_not_use_legacy_research_key_projection(self) -> None:
        # No live supervisor path may revive the retired research-key loader
        # or forwarding.
        for retired in ("RESEARCH_API_KEY_NAMES", "load_research_api_keys",
                        "kimi_research_environment", "PERPLEXITY_API_KEY", "FIRECRAWL_API_KEY"):
            self.assertNotIn(retired, self.supervisor_source)
        namespace = self._credential_functions()
        self.assertEqual(set(namespace["MANAGED_CREDENTIAL_NAMES"]), {
            "SOLODIT_API_KEY", "XAI_API_KEY", "GITHUB_PERSONAL_ACCESS_TOKEN",
        })

    def test_worktree_pool_excludes_credential_configs_for_existing_and_new_attempts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_fixture_repo(self, root)

            pool_root = root / "pool"
            pool_root.mkdir()
            existing_root = add_linked_worktree(
                self, repo, pool_root / ("d-" + "a" * 32)
            )
            plant_credential_config(existing_root)

            # Positive control for the defect: before WorktreePool applies its
            # local exclusion, an already-live linked worktree exposes the
            # credential-bearing directory as ordinary untracked content.
            before = credential_status(self, existing_root)
            self.assertEqual(before.stdout, "?? .role-capabilities/\n")

            pool = wti.WorktreePool(repo, pool_root, base_branch="v2")
            fresh = pool.provision(
                "TASK-2026-08-30-1030-sec02-test",
                "d-" + "b" * 32,
            )
            plant_credential_config(fresh.worktree_root)
            legitimate_output = fresh.worktree_root / "worker-result.txt"
            legitimate_output.write_text("legitimate output\n", encoding="utf-8")

            for worktree_root in (existing_root, fresh.worktree_root):
                with self.subTest(worktree_root=worktree_root.name):
                    status = credential_status(self, worktree_root)
                    self.assertEqual(status.stdout, "")
                    ignored = check_ignore(self, worktree_root)
                    self.assertIn("info/exclude", ignored.stdout)
                    self.assertIn("/.role-capabilities/", ignored.stdout)

            visible_output = run_git(
                self,
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                legitimate_output.name,
                cwd=fresh.worktree_root,
            )
            self.assertEqual(visible_output.stdout, "?? worker-result.txt\n")

            # Mutation-sensitive negative control: remove exactly the
            # containment rule and prove the same path becomes visible again.
            exclude_path = repo / ".git" / "info" / "exclude"
            original_line_count = len(
                exclude_path.read_text(encoding="utf-8").splitlines()
            )
            strip_exclusion_rule(repo)
            self.assertLess(
                len(exclude_path.read_text(encoding="utf-8").splitlines()),
                original_line_count,
            )

            no_rule = check_ignore(self, fresh.worktree_root)
            self.assertEqual(no_rule.returncode, 1)
            for worktree_root in (existing_root, fresh.worktree_root):
                exposed = credential_status(self, worktree_root)
                self.assertEqual(exposed.stdout, "?? .role-capabilities/\n")

    # The test above is satisfied by EITHER install site alone: info/exclude is
    # common to the whole repository, so installing the rule during provision()
    # of a new attempt retroactively covers the pre-existing fixture worktree
    # too. The two tests below separate the sites, one each.

    def test_pool_construction_alone_retrofits_an_already_live_worktree(
        self,
    ) -> None:
        """Pins the __init__ retrofit; no provision() call can satisfy it.

        Constructing the pool over a pre-existing worktree and provisioning
        nothing is the only state the reassert cannot have produced.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_fixture_repo(self, root)
            pool_root = root / "pool"
            pool_root.mkdir()
            existing_root = add_linked_worktree(
                self, repo, pool_root / ("d-" + "c" * 32)
            )
            plant_credential_config(existing_root)

            # Positive control: the rule is genuinely absent beforehand, so a
            # later pass cannot be inherited from the fixture's own state.
            self.assertEqual(check_ignore(self, existing_root).returncode, 1)
            self.assertEqual(
                credential_status(self, existing_root).stdout,
                "?? .role-capabilities/\n",
            )

            pool = wti.WorktreePool(repo, pool_root, base_branch="v2")
            # Nothing was provisioned, so only __init__ can have installed it.
            self.assertEqual(pool.active(), ())

            ignored = check_ignore(self, existing_root)
            self.assertEqual(ignored.returncode, 0)
            self.assertIn("info/exclude", ignored.stdout)
            self.assertIn("/.role-capabilities/", ignored.stdout)
            self.assertEqual(credential_status(self, existing_root).stdout, "")

    def test_provision_reasserts_the_rule_after_a_post_construction_edit(
        self,
    ) -> None:
        """Pins the provision() reassert; the __init__ retrofit cannot satisfy it.

        The rule __init__ installed is deleted after construction, exactly the
        operator edit the reassert exists for, so only the reassert can put it
        back before the new worktree is created.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_fixture_repo(self, root)
            pool_root = root / "pool"
            pool_root.mkdir()
            existing_root = add_linked_worktree(
                self, repo, pool_root / ("d-" + "d" * 32)
            )
            plant_credential_config(existing_root)

            pool = wti.WorktreePool(repo, pool_root, base_branch="v2")

            # An operator rewrites the repository-local excludes after the pool
            # was constructed, keeping their own patterns and dropping ours.
            exclude_path = repo / ".git" / "info" / "exclude"
            exclude_path.write_text(
                "# operator pattern\n/scratch/\n/.role-capabilities/\n",
                encoding="utf-8",
            )
            strip_exclusion_rule(repo)

            # Positive control: the rule really is gone at this point.
            self.assertEqual(check_ignore(self, existing_root).returncode, 1)
            self.assertEqual(
                credential_status(self, existing_root).stdout,
                "?? .role-capabilities/\n",
            )

            fresh = pool.provision(
                "TASK-2026-08-30-1030-sec02-reassert",
                "d-" + "e" * 32,
            )
            plant_credential_config(fresh.worktree_root)

            for worktree_root in (existing_root, fresh.worktree_root):
                with self.subTest(worktree_root=worktree_root.name):
                    ignored = check_ignore(self, worktree_root)
                    self.assertEqual(ignored.returncode, 0)
                    self.assertIn("info/exclude", ignored.stdout)
                    self.assertIn("/.role-capabilities/", ignored.stdout)
                    self.assertEqual(
                        credential_status(self, worktree_root).stdout, ""
                    )

            # The reassert appends; it must not discard operator patterns.
            self.assertIn("/scratch/", exclude_path.read_text(encoding="utf-8"))

    def test_exclusion_lock_failure_raises_the_module_error_and_frees_the_fd(
        self,
    ) -> None:
        """A raw OSError from flock would escape the only handler there is.

        `bin/board-supervisor.sh` catches WorktreeIsolationError at the
        WorktreePool construction site and nothing else, so every OS call in
        this helper has to convert.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_fixture_repo(self, root)

            operations: list[int] = []
            lock_descriptors: list[int] = []
            closed: list[int] = []
            real_open, real_close = os.open, os.close

            def failing_flock(descriptor: int, operation: int) -> None:
                operations.append(operation)
                raise OSError(errno.EDEADLK, "resource deadlock avoided")

            def spy_open(path: Any, flags: int, mode: int = 0o777) -> int:
                descriptor = real_open(path, flags, mode)
                if str(path).endswith("board-integration.lock"):
                    lock_descriptors.append(descriptor)
                return descriptor

            def spy_close(descriptor: int) -> None:
                closed.append(descriptor)
                real_close(descriptor)

            with mock.patch.object(wti.fcntl, "flock", failing_flock):
                with mock.patch.object(wti.os, "open", spy_open):
                    with mock.patch.object(wti.os, "close", spy_close):
                        with self.assertRaises(wti.WorktreeIsolationError) as caught:
                            wti._ensure_worker_credential_exclusion(repo)

            self.assertIn("board-integration.lock", str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, OSError)
            # Released, and no LOCK_UN issued for a lock never acquired.
            self.assertEqual(len(lock_descriptors), 1)
            self.assertIn(lock_descriptors[0], closed)
            self.assertEqual(operations, [wti.fcntl.LOCK_EX])

            # Positive control: the flock is what failed, not the fixture — the
            # same call installs the rule once flock works again.
            wti._ensure_worker_credential_exclusion(repo)
            self.assertIn(
                "/.role-capabilities/",
                (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8"),
            )
    def test_credential_gate_authorizes_brokered_servers_too(self) -> None:
        # Cause 1, executed rather than asserted as text: a Kimi role declares
        # `lead:chrono-research-arsenal`, which `load_projection` strips into
        # `brokered_mcps`, leaving `mcps` empty.
        snippet = balanced_call(
            self.supervisor_source,
            "credential_snapshot, credential_missing = load_managed_credentials(",
        )
        captured: dict[str, Any] = {}

        def fake_load(worker_lane: str, authorized: Any) -> tuple[Any, tuple[()]]:
            captured["lane"] = worker_lane
            captured["authorized"] = tuple(authorized)
            return MappingProxyType({}), ()

        namespace = {
            "lane": "kimi",
            "capability_projection": {
                "mcps": [],
                "brokered_mcps": ["chrono-research-arsenal", "chrono-vault"],
            },
            "load_managed_credentials": fake_load,
        }
        exec(compile(snippet, "board-supervisor.sh", "exec"), namespace)

        self.assertEqual(captured["lane"], "kimi")
        self.assertIn("chrono-research-arsenal", captured["authorized"])

        # Negative control: the same expression on a direct-declaration role
        # must not invent an authorization the projection never made.
        namespace["capability_projection"] = {"mcps": ["guarded-solodit"], "brokered_mcps": []}
        exec(compile(snippet, "board-supervisor.sh", "exec"), namespace)
        self.assertEqual(captured["authorized"], ("guarded-solodit",))

    def test_capability_source_contains_only_typed_mcp_requirements(self) -> None:
        payload = json.loads(CAPABILITY_SOURCE.read_text(encoding="utf-8"))
        requirements = {
            capability.get("requirement")
            for entry in payload["entries"]
            for capability in entry.get("mcps", [])
        }
        self.assertEqual(requirements, {"preferred", "required"})


if __name__ == "__main__":
    unittest.main()
