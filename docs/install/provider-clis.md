# Provider CLIs

`bin/squad up` checks for `claude`, `codex`, `agy`, and `grok` and exits
1 if any is missing:

```text
ERROR: missing required command(s): claude grok
Fix: install/login the missing CLIs, and install core tools with: brew install jq tmux fswatch uv
```

All four are required. `shared/launch-dependencies.sh` is the executable
authority for the complete required-command set.

Model inference always runs through these native CLIs. Vibe Squad never
substitutes an MCP relay or a direct model API for a model lane.

## Summary

| CLI | Installed via | Package / source | Auth |
|---|---|---|---|
| `claude` | Anthropic native installer | `https://claude.ai/install.sh` | `claude` → `/login` (subscription or managed login) |
| `codex` | npm (global) | `@openai/codex` | `codex login` |
| `agy` (the `gemini` lane) | Standalone binary — no package manager | Antigravity (Google): <https://antigravity.google> | personal OAuth |
| `grok` | Vendor install — no package manager | xAI Grok CLI | `XAI_API_KEY` policy in `model-lanes/lane-capabilities.tsv` |

The `codex` (npm) package identity was confirmed against the
live registry on 2026-08-13; `claude` installs from Anthropic's script and
`agy`/`grok` are vendor-distributed, so none of those three is a package-manager
entry. Version numbers move; the package/vendor names are the stable part.

## claude

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

This installs into `~/.local/share/claude/versions/` and links
`~/.local/bin/claude`, so `~/.local/bin` must be on your `PATH`.

Authenticate by starting `claude` and running `/login`. Claude Code uses its
supported subscription or managed-login path; it does not take an API key here.

Check:

```bash
command -v claude && claude --version
```

## codex

```bash
npm install -g @openai/codex
```

Authenticate:

```bash
codex login
```

Check:

```bash
command -v codex && codex --version
```

## gemini lane (`agy`)

`agy` is Antigravity's CLI, distributed by Google as a standalone binary — there
is no Homebrew, npm, or `uv` package. Download it from Antigravity
(<https://antigravity.google>) and put it on your `PATH` (for example under
`~/.local/bin`), then run it interactively to complete personal OAuth. The
routing identifier remains `gemini`, but the standalone `gemini` binary and its
API-key lane are retired. `GEMINI_API_KEY` is used only by optional metered
media-provider operations; it does not authenticate this model lane.

Check:

```bash
command -v agy && agy --version
```

## grok

`grok` is xAI's Grok CLI. There is no confirmed one-line public package command
for it, so obtain it from xAI's official distribution and put it on your `PATH`;
a working install links `grok` from under `~/.grok`, so that directory (or
wherever you place the binary) must be on your `PATH`. Follow the auth policy
declared for the lane in `model-lanes/lane-capabilities.tsv`; do not infer lane
authentication from the separate native-search subscription.

Check:

```bash
command -v grok && grok --version
```

## Verify all four at once

This is the same list `bin/launch-squad.sh` checks:

```bash
source shared/launch-dependencies.sh
for dep in "${SQUAD_REQUIRED_COMMANDS[@]}"; do
  command -v "$dep" >/dev/null 2>&1 || echo "MISSING: $dep"
done
```

Silence means `bin/squad up` will get past its dependency gate.

## Authentication is not verified by presence

`command -v` and `--version` prove a binary exists. Neither proves it is logged
in. `bin/squad doctor` deliberately performs no login or inference, so it
reports CLI authentication as **could not determine** rather than guessing.

The cheapest real check is to start each CLI once, interactively, and confirm it
does not prompt you to log in.

## PATH note

`claude` installs outside Homebrew, into `~/.local/bin`. If `claude`
is reported missing right after a successful install, that directory is
almost certainly not on your `PATH`:

```bash
case ":$PATH:" in *":$HOME/.local/bin:"*) echo "on PATH" ;; *) echo "NOT on PATH" ;; esac
```
