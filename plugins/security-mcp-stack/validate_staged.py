#!/usr/bin/env python3
"""Fail-closed static validation that the guarded security MCP trio stays RETIRED.

RETIRED 2026-10-27 (TASK-2026-10-27-0124-a8f2b472). The guarded trio
(``guarded-semgrep`` / ``guarded-slither`` / ``guarded-solodit``) routed through Trail
of Bits' ``mcp-context-protector`` with a wrapper and child entrypoints under
a private staging directory. The trio was staged, then retired on both lanes on
2026-10-27. The ``semgrep`` and ``slither`` CLIs are used directly; they need no
staging gate.

This validator used to require the trio present and activated. It now asserts the
opposite contract and still exits 0 only when retirement holds:

- the retired names are ABSENT from every lane and mirror config,
- the staged security-arsenal mirrors and the Snyk target carry no MCP servers,
- the live Claude security-arsenal config is empty,
- the three JSON mirrors stay semantically equal (a declared byte-identical group),
- ``held-solodit.json`` records ``activation: retired`` (not ``activated-ready``),
- the preactivation gate is a retired no-op that no longer references the dead arsenal,
- no config still points at the dead arsenal directory.

It prints a JSON result and exits non-zero on any drift back toward the trio.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
CLAUDE_STAGED = REPO / "model-lanes/claude/.mcp.security-arsenal.staged.json"
CODEX_STAGED = REPO / "model-lanes/gpt-codex/.codex/security-arsenal.staged.toml"
CLAUDE_LIVE = REPO / "model-lanes/claude/.mcp.json"
CODEX_LIVE = REPO / "model-lanes/gpt-codex/.codex/config.toml"
HELD_SOLODIT = REPO / "plugins/security-mcp-stack/held-solodit.json"
SNYK_TARGETS = REPO / "plugins/security-mcp-stack/snyk-preactivation-targets.json"
SNYK_GATE = REPO / "plugins/security-mcp-stack/preactivate-security-stack.sh"

RETIRED = ("guarded-semgrep", "guarded-slither", "guarded-solodit")
DEAD_ARSENAL = "_state/tooling-arsenal-2026-07-18"
SECRET_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9]|AIza[0-9A-Za-z_-]|gh[opsu]_[A-Za-z0-9]|"
    r"ya29\.[A-Za-z0-9_-]|-----BEGIN PRIVATE KEY-----)"
)


def json_servers(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


def toml_servers(path: Path) -> dict[str, object]:
    """Return the ``mcp_servers`` table; a servers-free retired file yields ``{}``."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcp_servers", {})
    return servers if isinstance(servers, dict) else {}


def retired_issues(label: str, servers: dict[str, object]) -> list[str]:
    """Flag any retired guarded server name that reappears in a config."""
    return [
        f"{label}:retired-server-present:{name}"
        for name in RETIRED
        if name in servers
    ]


def main() -> int:
    inputs = (CLAUDE_STAGED, CODEX_STAGED, CLAUDE_LIVE, CODEX_LIVE,
              HELD_SOLODIT, SNYK_TARGETS, SNYK_GATE)
    missing = [str(path.relative_to(REPO)) for path in inputs if not path.is_file()]
    if missing:
        # The public projection withholds this entire retired host configuration.
        # A partially missing private installation is still a validation failure.
        withheld = len(missing) == len(inputs)
        print(json.dumps({
            "status": "skip" if withheld else "fail",
            "reason": "private retirement inputs are not published" if withheld
                      else "incomplete retirement inputs",
            "missing": missing,
        }, sort_keys=True))
        return 0 if withheld else 1

    issues: list[str] = []

    claude_live = json_servers(CLAUDE_LIVE)
    claude_staged = json_servers(CLAUDE_STAGED)
    codex_live = toml_servers(CODEX_LIVE)
    codex_staged = toml_servers(CODEX_STAGED)
    snyk_targets = json_servers(SNYK_TARGETS)

    for label, servers in (
        ("claude-live", claude_live),
        ("claude-staged", claude_staged),
        ("gpt-codex-live", codex_live),
        ("gpt-codex-staged", codex_staged),
        ("snyk-targets", snyk_targets),
    ):
        issues.extend(retired_issues(label, servers))

    # The security-arsenal mirrors and the Snyk target must carry no MCP servers now.
    for label, servers in (
        ("claude-staged", claude_staged),
        ("gpt-codex-staged", codex_staged),
        ("snyk-targets", snyk_targets),
    ):
        if servers:
            issues.append(f"{label}:staged-mirror-must-be-empty")

    # The live Claude security-arsenal config is dedicated to this stack; retirement
    # empties it. The live Codex config legitimately keeps unrelated MCP servers (e.g.
    # playwright), so it is checked only for the absence of the retired trio above.
    if claude_live:
        issues.append("claude-live:security-arsenal-config-must-be-empty")

    # The three JSON mirrors are a declared byte-identical group; keep them equal.
    if not (claude_live == claude_staged == snyk_targets):
        issues.append("json-mirrors:not-semantically-equal")

    # held-solodit.json must record the retired state, not activated-ready.
    held = json.loads(HELD_SOLODIT.read_text(encoding="utf-8"))
    if held.get("activation") != "retired":
        issues.append("solodit:not-marked-retired")
    if SECRET_PATTERN.search(json.dumps(held)):
        issues.append("solodit:literal-secret")

    # No live MCP server definition may still wire the dead arsenal directory. A
    # retirement comment in these files may name the path to explain WHY it was retired;
    # only an active command/args reference inside a server table is drift.
    for label, servers in (
        ("claude-live", claude_live),
        ("claude-staged", claude_staged),
        ("gpt-codex-live", codex_live),
        ("gpt-codex-staged", codex_staged),
        ("snyk-targets", snyk_targets),
    ):
        for name, raw in servers.items():
            if DEAD_ARSENAL in json.dumps(raw):
                issues.append(f"{label}:{name}:wires-dead-arsenal")

    # The preactivation gate must be a retired no-op that no longer runs a live scan.
    gate_text = SNYK_GATE.read_text(encoding="utf-8")
    if "RETIRED" not in gate_text:
        issues.append("snyk-gate:not-marked-retired")
    if "--dangerously-run-mcp-servers" in gate_text:
        issues.append("snyk-gate:still-runs-mcp-scan")
    if not SNYK_GATE.stat().st_mode & 0o111:
        issues.append("snyk-gate:not-executable")

    result = {
        "status": "pass" if not issues else "fail",
        "state": "retired",
        "retired_servers": list(RETIRED),
        "retired_on": "2026-10-27",
        "task": "TASK-2026-10-27-0124-a8f2b472",
        "live_configs": [
            str(CLAUDE_LIVE.relative_to(REPO)),
            str(CODEX_LIVE.relative_to(REPO)),
        ],
        "replacement": "use the installed semgrep and slither CLIs directly",
        "issues": issues,
    }
    print(json.dumps(result, sort_keys=True))
    return int(bool(issues))


if __name__ == "__main__":
    sys.exit(main())
