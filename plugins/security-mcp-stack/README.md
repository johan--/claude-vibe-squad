# Security MCP stack — RETIRED 2026-10-27

**This stack is retired.** It staged the guarded security MCP trio —
`guarded-semgrep`, `guarded-slither`, and `guarded-solodit`, each routed through Trail of
Bits' `mcp-context-protector` — then retired it on 2026-10-27 and removed it from both
lanes.

**Run the CLIs directly instead.** Install Semgrep and Slither as ordinary command-line
tools (both are rows in `shared/registries/recommended-toolchain.tsv`) and run them from the
shell: no staging gate, no Context Protector wrapper, no restart ceremony. The starting
commands are in the [security arsenal guide](../../docs/tooling/security-arsenal-guide.md).
Solodit findings search has no CLI replacement: use a separately provisioned prior-art
service, or search Solodit by hand, as the `dedup-prior-art-check` skill describes.

## What retirement changed

- `model-lanes/claude/.mcp.json` and `model-lanes/claude/.mcp.security-arsenal.staged.json`
  now carry an empty `mcpServers` object.
- `model-lanes/gpt-codex/.codex/config.toml` dropped the three `guarded-*` server tables
  and keeps only its unrelated MCP servers (`playwright`, `chrome-devtools`,
  `sequential-thinking`). `model-lanes/gpt-codex/.codex/security-arsenal.staged.toml`
  stages no servers.
- `plugins/security-mcp-stack/snyk-preactivation-targets.json` carries an empty
  `mcpServers` object. The three JSON files stay byte-identical (the declared
  `security-mcp-json-mirrors` group in `shared/registries/identical-blobs.tsv`).
- `held-solodit.json` records `"activation": "retired"` and a `retired` block. Its dated
  `audit` build evidence is retained as history, not as an activation instruction.
- `preactivate-security-stack.sh` is a retired no-op that fails closed; it no longer runs
  the Snyk pre-activation scan.
- The `guarded-semgrep` / `guarded-slither` / `guarded-solodit` rows in the maintainer-local
  tool registry (`shared/registries/skill-tool-registry.tsv`, withheld from the public release)
  are marked `retired — do not invoke`, and the generated public
  `shared/registries/recommended-toolchain.tsv` omits them.

## Validator

`validate_staged.py` was rewritten to enforce the retired state instead of the former
required-and-activated trio. It now asserts the retired names are absent from every lane
and mirror config, the staged mirrors and the Snyk target carry no servers, the three JSON
mirrors stay semantically equal, `held-solodit.json` records `activation: retired`, and the
preactivation gate is a retired no-op. It still fails closed on any drift back toward the
trio.

```bash
python3 plugins/security-mcp-stack/validate_staged.py        # status: pass on the retired tree
python3 plugins/security-mcp-stack/test_validate_staged.py    # focused tests for the contract
```

> **Public distribution note.** The lane configuration files, staged review mirrors, and
> stack inputs below are operator-provided and are **not part of the public release** of
> this repository. On a public checkout these paths are absent:
>
> - `model-lanes/claude/.mcp.json`
> - `model-lanes/claude/.mcp.security-arsenal.staged.json`
> - `model-lanes/gpt-codex/.codex/config.toml`
> - `model-lanes/gpt-codex/.codex/security-arsenal.staged.toml`
> - `plugins/security-mcp-stack/held-solodit.json`
> - `plugins/security-mcp-stack/snyk-preactivation-targets.json`
> - `plugins/security-mcp-stack/preactivate-security-stack.sh`
>
> Because those files are missing, `validate_staged.py` cannot run from a public clone — it
> reads the lane configs directly and errors on the first absent path. The files are
> withheld because they encode one host's wiring, not because any single value is a secret.

## History

Google Model Armor was removed from this stack on 2026-08-10 (operator decision); it was a
GCP project service requiring application-default credentials, not an API-key endpoint, and
was never activated. The guarded trio was staged through `activated-ready` (the SDK 1.29.0
rebuild and production install are recorded in `held-solodit.json`), then retired on
2026-10-27.
