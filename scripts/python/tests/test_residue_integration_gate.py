"""Terminal residue gates exercise real Git and the reviewed hook driver."""
from dataclasses import asdict
from pathlib import Path
import json
import fcntl
import os
import runpy
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from test_worktree_isolation import ROOT, _git, _init_repo
import worktree_isolation as wti
import dispatch_context_builder as dcb
import registry_reconciler as rr

RESPONSE = "departments/coding/outbox/TASK-2026-10-27-1320-test-response.md"
CODE = "shared/capabilities/probe.md"


@pytest.fixture
def attempt(tmp_path):
    tmp_path = tmp_path.resolve()
    repo = _init_repo(tmp_path, integration_branch="v2")
    _git(["checkout", "-q", "main"], cwd=repo)
    # This fixture is an installed snapshot, not a worker hook. Use the real
    # driver and its real trigger selection, with small deterministic validators.
    driver = ROOT / "scripts/hooks/pre-commit"
    source = tmp_path / "reviewed-source"
    for relative in runpy.run_path(str(driver))["SNAPSHOT_INPUTS"]:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("exit 0\n" if relative.endswith(".sh") else "")
    (source / "bin/validate-capabilities.sh").write_text(
        'if grep -q bad "$VAULT_ROOT/shared/capabilities/probe.md"; then\n'
        '  echo "capability fixture refused residue"; exit 1\nfi\n'
        'case "$*" in *--self-test*)\n'
        '  if grep -q selftest "$VAULT_ROOT/shared/capabilities/probe.md"; then\n'
        '    echo "capability self-test refused residue"; exit 1\n  fi;; esac\n'
    )
    hooks = wti.git_common_dir(repo) / "hooks"
    snapshot = hooks / ".vibe-squad-snapshot.fixture"
    snapshot.mkdir()
    subprocess.run([sys.executable, "-I", "-B", str(driver), "--build-snapshot",
                    str(source), str(snapshot)], check=True, capture_output=True)
    shutil.copyfile(snapshot / "manifest.json", hooks / "vibe-squad-pre-commit-snapshot.json")
    hook = hooks / "vibe-squad-pre-commit"
    shutil.copyfile(driver, hook)
    (hooks / "vibe-squad-validate-release-version.py").write_text("raise SystemExit(0)\n")
    pool = tmp_path / "pool"
    handle = wti.WorktreePool(repo, pool, base_branch="main").provision(
        "TASK-2026-10-27-1320-test", "d-" + "a" * 32)
    authority = {
        "task_id": handle.task_id, "attempt_id": handle.attempt_id,
        "repo_root": str(repo), "pool_root": str(pool),
        "write_paths": [CODE, RESPONSE],
        "expected_result_path": RESPONSE, "expected_outbox_path": RESPONSE,
    }
    for name, content in ((CODE, "good\n"), (RESPONSE, "worker report\n")):
        path = handle.worktree_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return SimpleNamespace(repo=repo, handle=handle, authority=authority, hook=hook)


def finish(attempt, status="complete", *, prepared=None, authorized_deletes=()):
    # Run the production supervisor decision verbatim. No mock integration or
    # reimplementation can accidentally make the status/validation gate pass.
    source = (ROOT / "bin/board-supervisor.sh").read_text()
    start = source.index('        _work_recovery_window[0] = False\n', source.index('    result_path = Path(expected_result_path)'))
    end = source.index('        try:\n            # The bridge', start)
    if prepared is None:
        response = attempt.handle.worktree_root / RESPONSE
        response.parent.mkdir(parents=True, exist_ok=True)
        body = response.read_text() if response.exists() else "worker report"
        response.write_text("---\n" + (f"status: {status}\n" if status is not None else "")
                            + "---\n\n" + body)
        prepared = dcb.prepare_worktree_outputs(
            attempt.repo, attempt.handle.worktree_root, dict(attempt.authority, lane="codex"))
    context = {
        "wti": wti, "handle": attempt.handle, "authority": attempt.authority,
        "repo_path": attempt.repo, "Path": Path, "shutil": shutil,
        "_validated_trusted_host_path": lambda: os.environ.get("PATH", os.defpath),
        "prepared_outputs": prepared,
        "authorized_delete_paths": authorized_deletes, "_work_recovery_window": [True], "asdict": asdict,
    }
    def block(reason):
        raise SystemExit(75)
    context["block_after_provision"] = block
    try:
        exec(textwrap.dedent(source[start:end]), context)
    except SystemExit as exc:
        assert exc.code == 75
    return context["integration_result"]


def assert_preserved(attempt, receipt, before):
    assert receipt["status"] == "preserved"
    assert _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip() == before
    assert not (attempt.repo / CODE).exists()
    evidence = receipt["evidence_preservation"]
    assert evidence["evidence_ref"] == f"refs/heads/{attempt.handle.branch}"
    assert evidence["evidence_ref"] in evidence["evidence_location"]
    assert _git(["show", f'{evidence["evidence_ref"]}:{CODE}'], cwd=attempt.repo).stdout
    assert "board: preserve terminal evidence" in _git(
        ["log", "-1", "--format=%s", evidence["evidence_ref"]], cwd=attempt.repo).stdout
    assert "board: worker residue" not in _git(
        ["log", "--format=%s", f'{before}..{evidence["evidence_ref"]}'], cwd=attempt.repo).stdout


@pytest.mark.parametrize("status", ["needs_human", "blocked", "failed", "", "cut off", "partial", "in progress", None])
def test_noncomplete_residue_is_preserved_with_ref(attempt, status):
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    # A missing validator must not prevent non-complete evidence preservation.
    attempt.hook.rename(attempt.hook.with_name("retained-fixture-hook"))
    assert_preserved(attempt, finish(attempt, status), before)


@pytest.mark.parametrize("content", ["bad\n", "selftest\n"])
def test_complete_refused_by_trusted_validation_is_preserved(attempt, content):
    (attempt.handle.worktree_root / CODE).write_text(content)
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    receipt = finish(attempt)
    assert_preserved(attempt, receipt, before)
    assert "trusted residue pre-commit validation failed" in receipt["reason"]
    assert "refused residue" in receipt["reason"]


@pytest.mark.parametrize("status", ["complete", "needs_review"])
def test_complete_clean_residue_integrates_and_excludes_staged_report(attempt, status):
    # The staged report would fail the real leak guard if it entered the index.
    (attempt.handle.worktree_root / RESPONSE).write_text("---\nsensitivity: restricted\n---\nreport\n")
    _git(["add", RESPONSE], cwd=attempt.handle.worktree_root)
    receipt = finish(attempt, status)
    assert receipt["status"] == "integrated"
    assert receipt["worktree_integration"]["status"] == "integrated"
    assert (attempt.repo / CODE).read_text() == "good\n"
    assert not (attempt.repo / RESPONSE).exists()
    assert "evidence_preservation" not in receipt


def test_missing_trusted_hook_fails_closed(attempt):
    attempt.hook.rename(attempt.hook.with_name("retained-fixture-hook"))
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    receipt = finish(attempt)
    assert_preserved(attempt, receipt, before)
    assert "snapshot is unavailable" in receipt["reason"]


def test_worker_planted_hooks_cannot_replace_trusted_validator(attempt):
    (attempt.handle.worktree_root / CODE).write_text("bad\n")
    planted = attempt.handle.worktree_root / "hooks"
    planted.mkdir()
    marker = attempt.repo.parent / "planted-hook-fired"
    hook = planted / "pre-commit"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    hook.chmod(0o755)
    _git(["config", "core.hooksPath", str(planted)], cwd=attempt.repo)
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    assert_preserved(attempt, finish(attempt), before)
    assert not marker.exists()


def test_supervisor_passes_captured_status_and_receipt_names_ref(attempt):
    source = (ROOT / "bin/board-supervisor.sh").read_text()
    # Execute the actual supervisor gate and receipt assembly without launching
    # processes or touching live board state.
    start = source.index('        _work_recovery_window[0] = False\n', source.index('    result_path = Path(expected_result_path)'))
    end = source.index('        try:\n            # The bridge', start)
    context = {
        "wti": wti, "handle": attempt.handle, "authority": attempt.authority,
        "repo_path": attempt.repo, "Path": Path, "shutil": shutil,
        "_validated_trusted_host_path": lambda: os.environ.get("PATH", os.defpath),
        "prepared_outputs": SimpleNamespace(status="needs_human", status_recognized=True),
        "authorized_delete_paths": (), "_work_recovery_window": [True], "asdict": asdict,
        "block_after_provision": lambda reason: pytest.fail(reason),
    }
    exec(textwrap.dedent(source[start:end]), context)
    result = context["integration_result"]
    receipt_start = source.index('        receipt.update({\n', source.index('    result_path = Path(expected_result_path)'))
    receipt_end = source.index('        if bridge_receipt.get("mode_exit_verification")', receipt_start)
    bridge = dict.fromkeys(("artifact_path", "artifact_sha256", "artifact_promotions",
                           "envelope_path", "envelope_sha256", "candidate_tree_health"), "fixture")
    bridge["status"] = "needs_human"
    import hashlib
    context.update(receipt={}, bridge_receipt=bridge, result_path=Path(RESPONSE),
                   result_bytes=b"report", hashlib=hashlib, asdict=asdict)
    exec(textwrap.dedent(source[receipt_start:receipt_end]), context)
    receipt = json.loads(json.dumps(context["receipt"]))
    assert receipt["response_status"] == "needs_human"
    assert receipt["worktree_integration"]["status"] == "preserved"
    assert receipt["evidence_preservation"]["evidence_ref"] == f"refs/heads/{attempt.handle.branch}"
    assert 'if execution_kind == "lane" and not receipt.get("evidence_preservation"):' in source
    assert result["status"] == "preserved"


def test_corrupted_snapshot_fails_closed(attempt):
    validator = attempt.hook.parent / ".vibe-squad-snapshot.fixture/bin/validate-capabilities.sh"
    validator.write_text("exit 0\n")
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    receipt = finish(attempt)
    assert_preserved(attempt, receipt, before)
    assert "reviewed snapshot changed" in receipt["reason"]


def test_real_leak_guard_refuses_restricted_residue(attempt):
    (attempt.handle.worktree_root / CODE).write_text("---\nsensitivity: restricted\n---\n")
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    receipt = finish(attempt)
    assert_preserved(attempt, receipt, before)
    assert "pre-commit leak guard: COMMIT BLOCKED" in receipt["reason"]


def test_supervisor_validation_failure_blocks_before_publishing(attempt):
    (attempt.handle.worktree_root / CODE).write_text("bad\n")
    source = (ROOT / "bin/board-supervisor.sh").read_text()
    start = source.index('        _work_recovery_window[0] = False\n', source.index('    result_path = Path(expected_result_path)'))
    end = source.index('        try:\n            # The bridge', start)
    context = {
        "wti": wti, "handle": attempt.handle, "authority": attempt.authority,
        "repo_path": attempt.repo, "Path": Path, "shutil": shutil,
        "_validated_trusted_host_path": lambda: os.environ.get("PATH", os.defpath),
        "prepared_outputs": SimpleNamespace(status="complete", status_recognized=True),
        "authorized_delete_paths": (), "_work_recovery_window": [True], "asdict": asdict,
    }
    def block(reason):
        assert "trusted residue pre-commit validation failed" in reason
        assert context["_work_recovery_window"] == [False]
        raise SystemExit(75)
    context["block_after_provision"] = block
    with pytest.raises(SystemExit, match="75"):
        exec(textwrap.dedent(source[start:end]), context)
    # Exercise the raw failure receipt's evidence attachment too. The terminal
    # finalizer may refresh it, but a direct supervisor receipt must name the ref.
    start = source.index('    integration_result = globals().get("integration_result", {})')
    end = source.index('    if controller_quarantine is not None:', start)
    context["payload"] = {}
    context["work_recovery"] = {}
    exec(textwrap.dedent(source[start:end]), context)
    assert context["payload"]["evidence_preservation"]["evidence_ref"] == f"refs/heads/{attempt.handle.branch}"


@pytest.mark.parametrize("content", ["bad\n", "selftest\n", "---\nsensitivity: restricted\n---\n"])
def test_committed_work_cannot_bypass_trusted_guard(attempt, content):
    root = attempt.handle.worktree_root
    (root / CODE).write_text(content)
    # Positive control: these same bytes fail when left as residue.
    with pytest.raises(wti.WorktreeIsolationError, match="validation failed"):
        wti.validate_worker_residue(attempt.handle, attempt.authority["write_paths"],
                                    (RESPONSE,), attempt.hook)
    _git(["add", CODE], cwd=root)
    _git(["-c", "core.hooksPath=/dev/null", "commit", "-qm", "bypass worker hook"], cwd=root)
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    assert_preserved(attempt, finish(attempt), before)


def test_restricted_intermediate_commit_cannot_hide_behind_clean_head(attempt):
    root = attempt.handle.worktree_root
    for content in ("---\nsensitivity: restricted\n---\n", "good\n"):
        (root / CODE).write_text(content)
        _git(["add", CODE], cwd=root)
        _git(["commit", "-qm", content.strip()], cwd=root)
    receipt = finish(attempt)
    assert receipt["status"] == "preserved"
    assert "pre-commit leak guard: COMMIT BLOCKED" in receipt["reason"]


@pytest.mark.parametrize("content", ["bad\n", "good\n"])
def test_failed_return_path_preserves_commits_without_integrating(attempt, content):
    root = attempt.handle.worktree_root
    (root / CODE).write_text(content)
    _git(["add", CODE], cwd=root)
    _git(["commit", "-qm", "worker before timeout"], cwd=root)
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    source = (ROOT / "bin/board-supervisor.sh").read_text()
    block = source.split("# BEGIN committed-work-recovery\n", 1)[1].split("# END committed-work-recovery", 1)[0]
    namespace = dict(wti=wti, asdict=asdict, launch_mode="trusted", execution_kind="lane",
                     handle=attempt.handle, authority=attempt.authority,
                     write_board_note=lambda note: None)
    exec(block, namespace)
    namespace["_work_recovery_window"][0] = True
    receipt = namespace["recover_committed_work_for_block"]()
    assert_preserved(attempt, receipt, before)


def test_lingering_writer_cannot_land_post_validation_bytes(attempt, monkeypatch):
    real = wti.commit_worker_residue
    def lingering_writer(handle, *args, **kwargs):
        (handle.worktree_root / CODE).write_text("bad\n")
        return real(handle, *args, **kwargs)
    monkeypatch.setattr(wti, "commit_worker_residue", lingering_writer)
    receipt = finish(attempt)
    assert receipt["status"] == "preserved"
    assert "uncommitted in-scope" in receipt["reason"]
    assert not (attempt.repo / CODE).exists()
    # The controller-created candidate commit contains only validated bytes.
    commits = _git(["log", "--all", "--format=%H", "--grep=board: worker residue"], cwd=attempt.repo).stdout.splitlines()
    assert len(commits) == 1
    assert _git(["show", f"{commits[0]}:{CODE}"], cwd=attempt.repo).stdout == "good\n"


def test_worker_head_change_after_validation_is_refused(attempt, monkeypatch):
    real = wti.commit_worker_residue
    def rewrite_head(handle, *args, **kwargs):
        result = real(handle, *args, **kwargs)
        _git(["commit", "--allow-empty", "-qm", "late worker commit"], cwd=handle.worktree_root)
        return result
    monkeypatch.setattr(wti, "commit_worker_residue", rewrite_head)
    receipt = finish(attempt)
    assert receipt["status"] == "preserved"
    assert "HEAD changed after trusted validation" in receipt["reason"]


@pytest.fixture
def external_attempt(tmp_path, monkeypatch):
    from test_external_work_repo import init_repo
    root = tmp_path.resolve()
    squad = init_repo(root / "squad", "main")
    work = init_repo(root / "client", "client-main")
    pool = root / "pool"
    handle = wti.WorktreePool(work, pool, base_branch="client-main", external_work_repo=True).provision(
        "TASK-2026-10-27-1320-test", "d-" + "b" * 32)
    authority = dict(task_id=handle.task_id, attempt_id=handle.attempt_id,
                     repo_root=str(squad), pool_root=str(pool), work_repo_root=str(work),
                     work_base_branch="client-main", write_paths=["code.txt", RESPONSE],
                     expected_result_path=RESPONSE, expected_outbox_path=RESPONSE)
    (handle.worktree_root / "code.txt").write_text("good\n")
    return SimpleNamespace(repo=squad, work=work, handle=handle, authority=authority)


@pytest.mark.parametrize("status", ["complete", "needs_review"])
@pytest.mark.parametrize("committed", [False, True])
def test_external_clean_work_integrates_without_squad_snapshot(external_attempt, status, committed):
    a = external_attempt
    if committed:
        _git(["add", "code.txt"], cwd=a.handle.worktree_root)
        _git(["commit", "-qm", "worker commit"], cwd=a.handle.worktree_root)
    before = _git(["rev-parse", "client-main"], cwd=a.work).stdout
    receipt = finish(a, status)
    assert receipt["status"] == "integrated"
    assert receipt["candidate_validation"] == "external-no-hook"
    assert _git(["rev-parse", "client-main"], cwd=a.work).stdout == before
    assert _git(["show", f"board/{a.handle.task_id}:code.txt"], cwd=a.work).stdout == "good\n"


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("content", ["good\n", "bad\n"])
def test_external_defers_own_hook(external_attempt, configured, content):
    a = external_attempt
    hooks = a.work / "client-hooks" if configured else wti.git_common_dir(a.work) / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = a.work.parent / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f'#!/bin/sh\nprintf ran > "{marker}"\n! git show :code.txt | grep -q bad\n')
    hook.chmod(0o755)
    if configured:
        _git(["config", "core.hooksPath", "client-hooks"], cwd=a.work)
    (a.handle.worktree_root / "code.txt").write_text(content)
    receipt = finish(a)
    assert not marker.exists()
    assert receipt["status"] == "integrated"
    assert receipt["candidate_validation"] == "external-hook-deferred"
    # Positive control: the installed hook really would execute if invoked.
    subprocess.run([str(hook)], cwd=a.work, capture_output=True)
    assert marker.read_text() == "ran"



@pytest.mark.parametrize("status", ["blocked", "needs_human", "cut off"])
def test_external_noncomplete_work_is_preserved(external_attempt, status):
    a = external_attempt
    receipt = finish(a, status)
    assert receipt["status"] == "preserved"
    assert _git(["branch", "--list", f"board/{a.handle.task_id}"], cwd=a.work).stdout == ""
    ref = receipt["evidence_preservation"]["evidence_ref"]
    assert _git(["show", f"{ref}:code.txt"], cwd=a.work).stdout == "good\n"


def test_external_hook_cannot_rewrite_validated_index(external_attempt):
    a = external_attempt
    hook = wti.git_common_dir(a.work) / "hooks/pre-commit"
    hook.write_text('#!/bin/sh\nprintf bad > code.txt\ngit add code.txt\n')
    hook.chmod(0o755)
    receipt = finish(a)
    assert receipt["status"] == "integrated"
    assert receipt["candidate_validation"] == "external-hook-deferred"
    assert _git(["show", f"board/{a.handle.task_id}:code.txt"], cwd=a.work).stdout == "good\n"


def test_supervisor_resolves_hook_python_from_trusted_path(attempt, monkeypatch):
    observed = []
    real = wti.validate_worker_residue
    def validate(*args, **kwargs):
        observed.append(kwargs["interpreter"])
        return real(*args, **kwargs)
    monkeypatch.setattr(wti, "validate_worker_residue", validate)
    monkeypatch.setattr(sys, "executable", "/not-the-hook-python")
    assert finish(attempt)["status"] == "integrated"
    assert observed == [Path(shutil.which("python3"))]


def test_python_without_tomllib_is_refused(attempt, tmp_path):
    old = tmp_path / "python39"
    old.write_text('#!/bin/sh\necho "ModuleNotFoundError: tomllib" >&2\nexit 1\n')
    old.chmod(0o755)
    with pytest.raises(wti.WorktreeIsolationError, match="requires Python 3.11.*tomllib"):
        wti.validate_worker_residue(attempt.handle, attempt.authority["write_paths"],
                                    (RESPONSE,), attempt.hook, interpreter=old)


def test_real_snapshot_validates_real_squad_sources(tmp_path):
    # This test executes the production validators, not the deterministic
    # capability fixture, under the interpreter selected like the installed shim.
    driver = ROOT / "scripts/hooks/pre-commit"
    hooks = tmp_path.resolve() / "hooks"
    hooks.mkdir()
    snapshot = hooks / ".vibe-squad-snapshot.real"
    snapshot.mkdir()
    python = Path(shutil.which("python3"))
    subprocess.run([str(python), "-I", "-B", str(driver), "--build-snapshot", str(ROOT), str(snapshot)],
                   check=True, capture_output=True)
    shutil.copyfile(snapshot / "manifest.json", hooks / "vibe-squad-pre-commit-snapshot.json")
    hook = hooks / "vibe-squad-pre-commit"
    shutil.copyfile(driver, hook)
    shutil.copyfile(ROOT / "scripts/python/validate_release_version.py", hooks / "vibe-squad-validate-release-version.py")
    base = _git(["rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
    tree = _git(["rev-parse", "HEAD^{tree}"], cwd=ROOT).stdout.strip()
    handle = SimpleNamespace(repo_root=ROOT, base_commit=base, external_work_repo=False)
    wti._validate_candidate_tree(handle, tree, hook, python, os.environ["PATH"])


def test_concurrent_target_merge_validates_exact_final_tree(attempt, monkeypatch):
    # The candidate is valid alone; a change outside its scope makes the final
    # merged tree fail the same validator. Neither path may bypass validation.
    source = attempt.hook.parent / ".vibe-squad-snapshot.fixture/bin/validate-capabilities.sh"
    source.write_text(source.read_text() + '\nif test -e "$VAULT_ROOT/target-blocker"; then exit 1; fi\n')
    import hashlib
    pointer = attempt.hook.parent / "vibe-squad-pre-commit-snapshot.json"
    manifest = json.loads(pointer.read_text())
    manifest["files"]["bin/validate-capabilities.sh"] = hashlib.sha256(source.read_bytes()).hexdigest()
    pointer.write_text(json.dumps(manifest))
    real = wti.commit_worker_residue
    def advance_target(handle, *args, **kwargs):
        result = real(handle, *args, **kwargs)
        (attempt.repo / "target-blocker").write_text("unrelated target work\n")
        _git(["add", "target-blocker"], cwd=attempt.repo)
        _git(["commit", "-qm", "concurrent target"], cwd=attempt.repo)
        return result
    monkeypatch.setattr(wti, "commit_worker_residue", advance_target)
    receipt = finish(attempt)
    assert receipt["status"] == "preserved"
    assert "validation failed" in receipt["reason"]
    assert not (attempt.repo / CODE).exists()
    assert (attempt.repo / "target-blocker").exists()


def test_concurrent_disjoint_target_clean_merge_integrates(attempt, monkeypatch):
    real = wti.commit_worker_residue
    def advance_target(handle, *args, **kwargs):
        result = real(handle, *args, **kwargs)
        (attempt.repo / "unrelated.txt").write_text("target\n")
        _git(["add", "unrelated.txt"], cwd=attempt.repo)
        _git(["commit", "-qm", "concurrent target"], cwd=attempt.repo)
        return result
    monkeypatch.setattr(wti, "commit_worker_residue", advance_target)
    receipt = finish(attempt)
    assert receipt["status"] == "integrated"
    assert receipt["worktree_integration"]["exact_worker_history"] is False
    assert (attempt.repo / CODE).read_text() == "good\n"
    assert (attempt.repo / "unrelated.txt").read_text() == "target\n"


def test_validator_reads_candidate_instead_of_out_of_scope_disk(attempt):
    validator = attempt.hook.parent / ".vibe-squad-snapshot.fixture/bin/validate-capabilities.sh"
    validator.write_text(validator.read_text() + '\nif test -e "$VAULT_ROOT/not-in-candidate"; then exit 1; fi\n')
    import hashlib
    pointer = attempt.hook.parent / "vibe-squad-pre-commit-snapshot.json"
    manifest = json.loads(pointer.read_text())
    manifest["files"]["bin/validate-capabilities.sh"] = hashlib.sha256(validator.read_bytes()).hexdigest()
    pointer.write_text(json.dumps(manifest))
    (attempt.handle.worktree_root / "not-in-candidate").write_text("worker probe\n")
    assert finish(attempt)["status"] == "integrated"
    assert not (attempt.repo / "not-in-candidate").exists()


def test_completed_response_without_code_needs_no_hook(attempt):
    attempt.authority["write_paths"] = [RESPONSE]
    attempt.hook.rename(attempt.hook.with_name("retained-fixture-hook"))
    receipt = finish(attempt)
    assert receipt["status"] == "integrated"
    assert receipt["worktree_integration"]["status"] == "no-committed-in-scope-changes"
    assert receipt["candidate_validation"] == "no-code-changes"


def test_external_installed_symlink_hook_is_deferred(external_attempt):
    a = external_attempt
    script = a.work / "installed-pre-commit"
    script.write_text('#!/bin/sh\nexit 1\n')
    script.chmod(0o755)
    hook = wti.git_common_dir(a.work) / "hooks/pre-commit"
    hook.symlink_to(script)
    receipt = finish(a)
    assert receipt["status"] == "integrated"
    assert receipt["candidate_validation"] == "external-hook-deferred"


def test_external_deferred_hook_preserves_unchanged_media(external_attempt):
    a = external_attempt
    # Re-provision after adding the media baseline, without mutating live state.
    media = a.work / "media/fixture.txt"
    media.parent.mkdir()
    media.write_text("required baseline\n")
    _git(["add", "media/fixture.txt"], cwd=a.work)
    _git(["commit", "-qm", "media baseline"], cwd=a.work)
    a.handle = wti.WorktreePool(a.work, Path(a.authority["pool_root"]),
                               base_branch="client-main", external_work_repo=True).provision(
        a.handle.task_id, "d-" + "e" * 32)
    a.authority["attempt_id"] = a.handle.attempt_id
    (a.handle.worktree_root / "code.txt").write_text("good\n")
    hook = wti.git_common_dir(a.work) / "hooks/pre-commit"
    hook.write_text('#!/bin/sh\ngrep -q "required baseline" media/fixture.txt\n')
    hook.chmod(0o755)
    assert finish(a)["status"] == "integrated"


@pytest.mark.parametrize("status", ["complete", "needs_review"])
def test_squad_clean_committed_work_integrates(attempt, status):
    root = attempt.handle.worktree_root
    _git(["add", CODE], cwd=root)
    _git(["commit", "-qm", "clean worker commit"], cwd=root)
    receipt = finish(attempt, status)
    assert receipt["status"] == "integrated"
    assert receipt["candidate_validation"] == "squad-snapshot"
    assert (attempt.repo / CODE).read_text() == "good\n"


def test_every_supervisor_integration_consumes_validated_candidate():
    import ast
    source = (ROOT / "bin/board-supervisor.sh").read_text()
    start = source.index("<<'PYEOF'", source.index('exec "$python_bin"')) + len("<<'PYEOF'\n")
    tree = ast.parse(source[start:source.index("\nPYEOF\n", start)])
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "integrate_worktree_commits"]
    assert len(calls) == 1  # The failure route preserves; only finished work lands.
    for call in calls:
        keywords = {item.arg: item.value for item in call.keywords}
        assert ast.unparse(keywords["candidate"]) == "candidate"
        assert ast.unparse(keywords["authorized_delete_paths"]) == "authorized_delete_paths"


@pytest.mark.parametrize("raw", ["", "cut off", "partial", "in progress", None])
def test_unknown_status_bridge_remains_noncomplete(attempt, raw):
    response = attempt.handle.worktree_root / RESPONSE
    response.write_text("---\n" + (f"status: {raw}\n" if raw is not None else "")
                        + "---\n\nUnfinished work.\n")
    prepared = dcb.prepare_worktree_outputs(
        attempt.repo, attempt.handle.worktree_root, dict(attempt.authority, lane="codex"))
    assert prepared.status == "blocked"
    assert prepared.status_recognized is False
    assert "status: blocked\n" in prepared.envelope_bytes.decode()
    assert rr.resolve_worker_status({"mandatory_review": False}, prepared.status) == ("blocked", False)
    before = _git(["rev-parse", "main"], cwd=attempt.repo).stdout.strip()
    assert_preserved(attempt, finish(attempt, prepared=prepared), before)


@pytest.mark.parametrize("raw", ["completed", "done", "complete_with_scoped_exclusion", "COMPLETE"])
def test_closed_completion_aliases_integrate_through_bridge(attempt, raw):
    assert finish(attempt, raw)["status"] == "integrated"
    assert (attempt.repo / CODE).read_text() == "good\n"


def test_synthesized_envelope_preserves_without_completion_claim(attempt):
    result = "report.md"
    (attempt.handle.worktree_root / result).write_text("Usable partial artifact.\n")
    (attempt.handle.worktree_root / RESPONSE).rename(attempt.handle.worktree_root / "original-report.md")
    attempt.authority["expected_result_path"] = result
    attempt.authority["write_paths"].append(result)
    prepared = dcb.prepare_worktree_outputs(
        attempt.repo, attempt.handle.worktree_root, dict(attempt.authority, lane="codex"))
    assert prepared.status == "blocked" and not prepared.status_recognized
    assert finish(attempt, prepared=prepared)["status"] == "preserved"
    assert not (attempt.repo / CODE).exists()


@pytest.mark.parametrize("in_scope", [False, True])
def test_external_hook_never_loads_candidate_script(external_attempt, in_scope):
    a = external_attempt
    script = a.work / "scripts/lint.sh"
    script.parent.mkdir()
    marker = a.work.parent / "candidate-code-ran"
    script.write_text("#!/bin/sh\nexit 0\n")
    _git(["add", "scripts/lint.sh"], cwd=a.work)
    _git(["commit", "-qm", "controller lint"], cwd=a.work)
    a.handle = wti.WorktreePool(a.work, Path(a.authority["pool_root"]), base_branch="client-main",
                               external_work_repo=True).provision(a.handle.task_id, "d-" + "f" * 32)
    a.authority["attempt_id"] = a.handle.attempt_id
    (a.handle.worktree_root / "code.txt").write_text("good\n")
    worker_script = a.handle.worktree_root / "scripts/lint.sh"
    worker_script.write_text(f'#!/bin/sh\nprintf executed > "{marker}"\n')
    _git(["add", "scripts/lint.sh"], cwd=a.handle.worktree_root)
    _git(["commit", "-qm", "worker script"], cwd=a.handle.worktree_root)
    hook = wti.git_common_dir(a.work) / "hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexec sh ./scripts/lint.sh\n")
    hook.chmod(0o755)
    if in_scope:
        a.authority["write_paths"].append("scripts/lint.sh")
    receipt = finish(a)
    assert not marker.exists()
    if in_scope:
        assert receipt["status"] == "integrated"
        assert receipt["candidate_validation"] == "external-hook-deferred"
    else:
        assert receipt["status"] == "preserved"
        assert "outside the integration scope" in receipt["reason"]
    # Positive control for the exact candidate-script loading chain.
    subprocess.run([str(hook)], cwd=a.handle.worktree_root, check=True)
    assert marker.read_text() == "executed"


@pytest.mark.parametrize("kind", ["out-of-scope", "committed-delete", "residue-delete", "cbse"])
def test_cheap_gates_precede_hook_resolution(attempt, monkeypatch, kind):
    wt = attempt.handle.worktree_root
    if kind == "out-of-scope":
        (wt / "outside.txt").write_text("outside\n")
        _git(["add", "outside.txt"], cwd=wt)
        _git(["commit", "-qm", "outside scope"], cwd=wt)
    elif kind == "cbse":
        (wt / ".envrc").write_text("echo worker\n")
    else:
        attempt.authority["write_paths"].append("README.md")
        (wt / "README.md").unlink()
        if kind == "committed-delete":
            _git(["add", "README.md"], cwd=wt)
            _git(["commit", "-qm", "unauthorized delete"], cwd=wt)
    monkeypatch.setattr(wti, "_integration_hook", lambda *args: pytest.fail("hook resolved before cheap gates"))
    assert finish(attempt)["status"] == "preserved"


def test_authorized_residue_delete_passes_validation_gate(attempt):
    attempt.authority["write_paths"].append("README.md")
    (attempt.handle.worktree_root / "README.md").unlink()
    receipt = finish(attempt, authorized_deletes=("README.md",))
    assert receipt["status"] == "integrated"
    assert receipt["worktree_integration"]["deleted_paths"] == ("README.md",)
    assert not (attempt.repo / "README.md").exists()


def _finish_with_target_advances(attempt, monkeypatch, *, advances, mutation=None):
    lock = wti.git_common_dir(attempt.repo) / "board-integration.lock"
    observed = []
    validate = wti._validate_candidate_tree
    commit = wti.commit_worker_residue

    def lock_is_free():
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)

    # Positive control: this probe detects a held integration lock.
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert not lock_is_free()
        fcntl.flock(held, fcntl.LOCK_UN)

    def advance(name):
        (attempt.repo / name).write_text(f"target {len(observed)}\n")
        _git(["add", name], cwd=attempt.repo)
        _git(["commit", "-qm", name], cwd=attempt.repo)

    def commit_then_advance(*args, **kwargs):
        result = commit(*args, **kwargs)
        advance("unrelated.txt")
        return result

    def validate_unlocked(*args, **kwargs):
        observed.append(lock_is_free())
        result = validate(*args, **kwargs)
        if 2 <= len(observed) <= advances + 1:
            advance("late-target.txt")
            if mutation is not None:
                mutation()
        return result

    monkeypatch.setattr(wti, "commit_worker_residue", commit_then_advance)
    monkeypatch.setattr(wti, "_validate_candidate_tree", validate_unlocked)
    receipt = finish(attempt)
    assert all(observed)
    assert lock_is_free()
    return receipt, observed


def test_merge_validation_releases_lock(attempt, monkeypatch):
    receipt, observed = _finish_with_target_advances(attempt, monkeypatch, advances=0)
    assert observed == [True, True]
    assert receipt["status"] == "integrated"
    assert receipt["worktree_integration"]["attempts"] == 1
    assert (attempt.repo / CODE).read_text() == "good\n"


def test_target_advance_during_merge_validation_retries_and_lands(attempt, monkeypatch):
    receipt, observed = _finish_with_target_advances(attempt, monkeypatch, advances=1)
    assert receipt["status"] == "integrated"
    assert receipt["worktree_integration"]["attempts"] == 2
    assert observed == [True] * 3
    assert (attempt.repo / CODE).read_text() == "good\n"
    assert (attempt.repo / "unrelated.txt").exists()
    assert (attempt.repo / "late-target.txt").exists()


def test_target_advance_on_every_attempt_is_refused_after_the_budget(attempt, monkeypatch):
    receipt, observed = _finish_with_target_advances(attempt, monkeypatch, advances=3)
    assert receipt["status"] == "preserved"
    assert "advanced concurrently" in receipt["reason"]
    assert "attempts=3" in receipt["reason"]
    assert observed == [True] * 4  # One residue validation plus three merge attempts.
    assert not (attempt.repo / CODE).exists()
    assert _git(["show", f"main:late-target.txt"], cwd=attempt.repo).stdout == "target 4\n"


@pytest.mark.parametrize("mutation, reason", [
    ("checkout", "target checkout changed"),
    ("worker-head", "worker HEAD changed before atomic integration"),
    ("dirty", "target worktree changed before atomic integration"),
    ("scope", "target write scope changed"),
])
def test_target_advance_does_not_retry_other_refusals(attempt, monkeypatch, mutation, reason):
    def mutate():
        if mutation == "checkout":
            _git(["checkout", "-qb", "other-target"], cwd=attempt.repo)
        elif mutation == "worker-head":
            _git(["commit", "--allow-empty", "-qm", "late worker"], cwd=attempt.handle.worktree_root)
        else:
            path = attempt.repo / CODE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("target change\n")
            if mutation == "scope":
                _git(["add", CODE], cwd=attempt.repo)
                _git(["commit", "-qm", "target scope drift"], cwd=attempt.repo)

    receipt, observed = _finish_with_target_advances(
        attempt, monkeypatch, advances=1, mutation=mutate)
    assert receipt["status"] == "preserved"
    assert reason in receipt["reason"]
    assert observed == [True, True]


def test_many_commits_run_full_validation_only_on_final_tree(attempt, monkeypatch):
    calls = []
    validate = wti._validate_candidate_tree

    def record(handle, tree, *args, **kwargs):
        calls.append((tree, kwargs.get("leaks_only", False)))
        return validate(handle, tree, *args, **kwargs)

    monkeypatch.setattr(wti, "_validate_candidate_tree", record)
    wt = attempt.handle.worktree_root
    for i in range(5):
        (wt / CODE).write_text(f"good {i}\n")
        _git(["add", CODE], cwd=wt)
        _git(["commit", "-qm", f"step {i}"], cwd=wt)
    assert finish(attempt)["status"] == "integrated"
    assert [leaks_only for _, leaks_only in calls] == [True] * 4 + [False]


def test_intermediate_validation_error_fixed_at_head_is_allowed(attempt):
    wt = attempt.handle.worktree_root
    for content in ("bad\n", "good\n"):
        (wt / CODE).write_text(content)
        _git(["add", CODE], cwd=wt)
        _git(["commit", "-qm", content.strip()], cwd=wt)
    assert finish(attempt)["status"] == "integrated"


def test_candidate_commit_preserves_unchanged_index_stat_cache(attempt, tmp_path):
    wt = attempt.handle.worktree_root
    _git(["status", "--porcelain"], cwd=wt)
    before = _git(["ls-files", "--debug", "--", "README.md"], cwd=wt).stdout
    assert "mtime: 0:0" not in before
    candidate = wti.validate_worker_residue(attempt.handle, attempt.authority["write_paths"],
                                            (RESPONSE,), attempt.hook)
    # Positive control: the retired operation clears stat data in a copied index.
    index = tmp_path / "copied-index"
    shutil.copyfile(wti._GIT_BINDINGS[wt][0] / "index", index)
    wti._checked_git(["read-tree", candidate.tree], cwd=wt, index_file=index)
    control = wti._checked_git(["ls-files", "--debug", "--", "README.md"], cwd=wt, index_file=index)
    assert "mtime: 0:0" in control
    wti.commit_worker_residue(attempt.handle, attempt.authority["write_paths"],
                              exclude_paths=(RESPONSE,), candidate=candidate)
    assert _git(["ls-files", "--debug", "--", "README.md"], cwd=wt).stdout == before


@pytest.mark.parametrize("configured", [False, True])
def test_external_dangling_symlink_hook_is_deferred(external_attempt, configured):
    a = external_attempt
    hooks = a.work / "client-hooks" if configured else wti.git_common_dir(a.work) / "hooks"
    hooks.mkdir(exist_ok=True)
    if configured:
        _git(["config", "core.hooksPath", "client-hooks"], cwd=a.work)
    hook = hooks / "pre-commit"
    hook.symlink_to(hooks / "missing-hook")
    assert os.path.lexists(hook) and not hook.exists()
    receipt = finish(a)
    assert receipt["status"] == "integrated"
    assert receipt["candidate_validation"] == "external-hook-deferred"
    assert _git(["show", f"board/{a.handle.task_id}:code.txt"], cwd=a.work).stdout == "good\n"


@pytest.mark.parametrize("configured", [False, True])
def test_external_directory_hook_is_deferred(external_attempt, configured):
    a = external_attempt
    hooks = a.work / "client-hooks" if configured else wti.git_common_dir(a.work) / "hooks"
    hooks.mkdir(exist_ok=True)
    if configured:
        _git(["config", "core.hooksPath", "client-hooks"], cwd=a.work)
    (hooks / "pre-commit").mkdir()
    receipt = finish(a)
    assert receipt["status"] == "integrated"
    assert receipt["candidate_validation"] == "external-hook-deferred"
    assert _git(["show", f"board/{a.handle.task_id}:code.txt"], cwd=a.work).stdout == "good\n"
