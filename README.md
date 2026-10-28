<div align="center">

# Vibe Squad

**One coordinator. Four model families. Seventy-one specialists, all written in Markdown you can read and edit.**

![models](https://img.shields.io/badge/models-Codex%20%C2%B7%20Claude%20%C2%B7%20Gemini%20%C2%B7%20Grok-informational)
![license](https://img.shields.io/badge/license-MIT-blue)
![orchestration](https://img.shields.io/badge/orchestration-native%20CLIs%20%C2%B7%20isolated%20worktrees-success)
![version](https://img.shields.io/badge/version-v2.0.0-blue)

<br>

![One operator talks to Chrono, which routes a Markdown task to one of 71 model-bound specialists in its own git worktree; risky work receives cross-family review, and a Markdown result returns to Chrono.](assets/hero/vibe-squad-hero.svg)

*One task, one specialist, one fresh CLI in its own git worktree; cross-family review when risky.*

</div>

---

Vibe Squad is for people who like **vibe coding** but want more structure than one enormous chat. You talk to **Chrono**, the coordinator. Chrono turns your goal into a plan, picks the specialist and model that fit each part, runs workers in isolated git worktrees, and calls for independent review when the risk warrants it.

You can ask it to:

- "Build a polished landing page, test it in the browser, and show me the result."
- "Research this product idea, compare the competitors, and turn it into a cited brief."
- "Audit this authorized smart-contract target, keep every test in scope, and package only reproduced findings."

**What's underneath**

- **The right model for each job** — every specialist is tied to the model that suits its work, not
  to whichever one you happen to be chatting with.
- **Each task runs in its own worktree** — a copy of the repo, with a written list of the files it
  may change, and nothing merges until its checks pass. That list is enforced when changes are
  integrated, not while the worker runs: a worker executes on your host with your permissions, so
  treat this as an autonomy tool, not a containment boundary. See the note under Quickstart.
- **A different model checks work that triggers review** — the reviewer comes from another family.
- **It remembers** — what a run learns is written down and read back before the next one, so a
  lesson paid for once isn't paid for twice.

The product is the instruction layer, and it is Markdown all the way down. Modes, specialist briefs, capability cards, and skills are files you can open and change. Code handles only what must not be guessed: launching, worktree isolation, identity checks, atomic publication, admission control, and Git-integration boundaries. Judgment stays with the models.

## Quickstart

macOS, for now. You need `tmux`, `fswatch`, `jq`, `curl`, Python 3.13, `uv`, and authenticated native CLIs for Claude, Codex, Gemini (`agy`), and Grok.

Memory lives outside the public repository. Create a private vault once:

```bash
mkdir -p "$HOME/Obsidian-Chrono"
printf '%s\n' '{"vault_id":"my-private-vault","schema_version":1}' \
  > "$HOME/Obsidian-Chrono/.chrono-vault"
export CHRONO_VAULT_ROOT="$HOME/Obsidian-Chrono"
```

Then clone and launch:

```bash
git clone https://github.com/mtarcure/claude-vibe-squad.git
cd claude-vibe-squad
uv sync                                # create the pinned Python 3.13 environment
bash docs/install/install-pre-commit-hook.sh  # install the reviewed local guards
bin/squad doctor
bin/squad up
```

The installer copies the private-memory leak guard and a reviewed validator bundle into Git's
private hooks directory. It does not point `core.hooksPath` at tracked files, so checking out another
branch cannot replace the code Git runs on your next commit. The installed hook also checks release
versions, specialists, and capability homes, with additional checks triggered by the staged paths;
CI runs repository-wide validation too. If an older clone has `core.hooksPath=.githooks`, migrate it
by running the installer. The installer clears the local value and refuses success if a global or
system value still overrides the private hooks directory, reporting the origin that must be
removed. See the [install guide](docs/install/README.md#5-install-the-local-leak-guard).

That is the whole required path. There is **no background daemon to install first**. `bin/squad up` runs on a fresh clone, notices once that the optional launchd daemon is absent, and continues.

**Optional: the launchd routines.** `bash bin/install-routines.sh --daemon-only` installs a background daemon; `bash bin/install-routines.sh` adds the optional routine agents too. The daemon buys exactly two things: the live `● daemon` segment in the tmux status bar, and the documented `POST /mcp/<server>/<tool>` HTTP bridge. Without it the status bar reads `● daemon offline` and that `curl` path is unavailable; the MCP servers themselves are unaffected. Dispatch, worktree isolation, review, memory, and the coordinator are untouched, because none of them talk to it. See [the daemon guide](docs/install/daemon.md).

**Before you install them, know what they run.** Each agent executes code from your
checkout on every launch, with no immutable snapshot — so checking out a branch you
have not reviewed (a fork's pull request above all) in a clone where they are loaded
runs that branch's code as your user, within about two minutes for the fastest agent.
The daemon and nightly agents source your shell secrets, and `com.vibesquad.chrome`
owns your authenticated browser, so that execution carries real authority. The
installer manages four of these agents (`com.vibesquad.daemon`, plus the optional
`com.claudevibesquad.nightly`, `com.vibesquad.dream`, and `com.chrono.squad-monitor`);
the fifth, `com.vibesquad.chrome`, is installed by no repository command, and
`bin/install-routines.sh --status` only reports it as `operator-managed`. Unload the agents before checking out an
unreviewed branch and reload afterwards; [the daemon guide](docs/install/daemon.md#security-every-listed-agent-runs-code-from-your-checkout)
lists every label and its exact `launchctl` commands. A fix separating the installed
code root from the live data root is planned.

`bin/squad up` opens a tmux control room with Chrono and a status window. Each specialist starts as a fresh native CLI process for its task; there are no permanent per-model panes. Detach with `Ctrl-b d`, return with `bin/squad attach`.

`--safe` is not a permission mode. It suppresses the first-run autonomy warning and skips the pre-flight `doctor` check, and that is all. The coordinator and board-spawned workers run with the same permissions either way, because worker launch flags are a fixed controller ABI the supervisor re-derives and refuses to launch on mismatch. Nothing in either path sandboxes later agent actions. A task's `read_scope` and `write_scope` are declarations: `write_scope` is enforced when committed changes are integrated, not while the worker acts, and `read_scope` is not an action-time read barrier. Review the isolated worktree, verification contract, and held-category policy before enabling broader autonomy.

See [Getting started](docs/getting-started.md) for provider authentication, vault setup, and your first request.

## Three engagement states, one conversation

You ask in plain language. Chrono always names the mode and asks for your approval before starting.

**`modeless` is the default for ordinary work**, such as routine changes and simple research.
It needs no mode setup. It follows the stricter of Project and Bounty's permissions and limits,
including a shorter time limit of 90 minutes per task. Choose Project or Bounty deliberately
when you need one of their workflows:

- **[Project](shared/modes/project.md)** covers software, research, content and media, operations, and learning. Its lifecycle is scope → plan → build → verify → review when required → deliver → remember.
- **[Bounty](shared/modes/bounty.md)** covers authorized offensive-security work: confirm what is in scope before testing, then reproduce the impact and run controls to rule out false positives before calling it a finding. Chrono and the specialists must follow these rules, but the rules do not automatically block a task from starting. At the end, a verifier requires the run record to declare a scope check, an exact list of allowed targets covering every declared target, and reproduction by a different model family with an evidence file for each finding. It checks those declarations and the files' hashes, not whether the evidence proves the findings.

A task file with no `mode:` defaults to `modeless` (empty or unknown values are rejected), while `scripts/send-task.sh` requires an explicit `--mode modeless`, `--mode project`, or `--mode bounty`.

Project and Bounty are the two defined modes; `modeless` needs no mode file. Triage means deciding how to assign work, not choosing another mode; parallel work means running several independently assigned tasks, with no special "panel" or "swarm" system.


## Dispatch, settlement, and the board

```mermaid
flowchart LR
    OP([You]) --> CO[Chrono]
    CO --> PLAN[Markdown task<br/>and optional capability]
    PLAN --> RUN[Fresh native CLI<br/>in isolated worktree]
    RUN --> CHECK[Tests, plus cross-family<br/>review when triggered]
    CHECK --> RESULT[Atomic result<br/>back to Chrono]
    RESULT --> OP
```

**A task is a Markdown file, and that file is the contract.** Which specialist, which model, what it
may read, what it may change, and what counts as done:

```yaml
---
specialist:    security-analyst        # the role, chosen for the job
to_model:      gpt-codex               # the model bound to that role
mode:          project
write_scope:   [src/auth]              # the only files it may change
read_scope:    [src, docs/auth.md]
reviews:       none                    # or the task id this reviews
review_triggers: [blast_radius]        # what forces a second opinion
---

Audit the session-handling path and report anything exploitable.
Do not change behaviour; findings only.
```

Everything the worker may do is on that page. Workers never talk to each other or to you — each
writes one result and Chrono integrates it.

Four things hold that together:

1. **Chrono is the only operator-facing voice.** Specialists report to Chrono, not to you.
2. **Routing is by fit, not preference.** Each specialist is bound to the model that suits its work,
   with a different-family backup and review route, all in one file:
   [`shared/specialist-runtime-map.tsv`](shared/specialist-runtime-map.tsv).
3. **Workers are isolated and results settle atomically.** Each mutating attempt gets its own git
   worktree and a narrow write scope; artifacts are written before their completion envelope, so
   partially published work never reads as done.
4. **Dangerous actions are refused before a worker starts, not asked about later.** Deleting files,
   changing credentials, publishing — withheld at launch, and deletion refused again at merge unless
   an exact pre-approved file list allows it.

**Review is part of settling, not a separate ceremony.** Every specialist has a route to a reviewer
from another model family. Review is mandatory only when the packet carries one of the four
[change-level triggers](shared/protocol.md#mandatory-review-behavior); otherwise Chrono verifies
and closes the task. The reviewer's job is to find a concrete reason the work is wrong; `REJECT`
is a useful result, not a failed run. The machine checks reviewer independence and evidence
identity — it does not vote on whether the work is any good. Which of those rules are actually under
test is written down in a tracked census that names the untested ones too.

**The board carries work across conversations.** Every run leaves its task file and receipt behind,
so a failure is diagnosed from those rather than from a chat log, and something raised but not
finished is picked up next session instead of dying with the thread.

![A live Vibe Squad board: Chrono working in the left pane while seven specialists run in parallel on the right](assets/demo/phase3-swarm.gif)

*A live board: seven specialists running in parallel across Claude and Codex.*

## Memory that compounds

Durable memory is private Markdown outside the repository. Chrono records what a run learned and
recalls it before the next one, so a lesson paid for once is not paid for twice.

Each note carries where it came from and how sensitive it is. There is no automatic contradiction
flag: recall does not mark a note "contested," and a newer note does not silently win. Conflicts are
resolved by explicit supersession links and human curation, and recall ranks on recorded usage
feedback and provenance. Obsidian is an optional way to read the same files by hand.

There is no graph database here, and no fashionable label applied for its own sake.

## Specialists, skills, and tools

**71 specialist briefs** live under `departments/` and `shared/specialists/`, each validated on every
push by CI. The installed local pre-commit hook also validates specialists and capability homes
and blocks private-memory artifacts before they enter history. A brief is prose: what the role is
for, how it should think, what it must refuse. Adding one means writing Markdown, not registering a class.

```text
71 specialists          claude  ██████████████████████████████   30
                        codex   ██████████████████████           22
                        gemini  ██████████████████               18
                        grok    █                                 1
```

The `gemini` row is a lane name, not a binary. That lane runs on the **agy** CLI, which fronts Gemini and Google's other products; the standalone `gemini` CLI is retired.

Around them sit **94 skills** (methodology documents that Chrono or a specialist reads when the
work calls for it), **four catalogued plugins**, and **8 MCP servers** covering memory, research,
recon, and media tooling. The skill count is unique skill names: 91 under `.claude/skills/` (86
for specialists, 5 for Chrono) plus 3 bundled with the `chrono-vault` plugin. The per-runtime
mirror copies under `.agents/skills/` and `model-lanes/gemini/.agents/skills/` are collapsed.

The guarded security MCPs (`guarded-semgrep`, `guarded-slither`, `guarded-solodit`) were retired
on 2026-10-27. Security audits run the Semgrep and Slither CLIs directly.

The public `plugins/` tree has five directories: the four catalogued plugins, and
`security-mcp-stack`, which has no marketplace manifest and now only records that retirement.

`chrono-dedup` exists in the private source and is not distributed here because the
[export path policy](tools/export/policy/path-policy.json) withholds its tree. Like the four
catalogued plugins, it declares AGPL-3.0-or-later separately from the root MIT licence; see
[the plugin licence register](THIRD_PARTY.md#plugins). The licence alone is not the withholding rule.
This checkout supplies no download or installation source for it. Ask the maintainer for a
separately distributed source release, or build a replacement prior-art service against your own
corpus; neither is installed by cloning this repository.

References to `chrono-dedup` in workflow cards, specialist methods, tool recommendations, and
expected host-tool descriptions describe an optional private setup. Those steps require a
separately provisioned service; use manual searches of public disclosures and your own prior
findings when it is absent. The shipped capability source records it as `needs-operator-install`,
and the generated adapters do not advertise it as available. Even a private checkout must
explicitly configure and verify the service before changing that declaration locally.

All four families run through their providers' **native CLIs** — never swapped for an MCP relay or a
direct API fallback. Three authenticate on subscription or managed-login paths; the Grok board lane is
the exception and requires an xAI API key (`model-lanes/lane-capabilities.tsv` declares
`xai-api-key-only`). Utility services are separate.

One rule governs all of it: **a configured tool is not a working tool until a live probe says so.**
Declared, delivered, and actual are three different things, and only actual counts. That probe is
[`bin/canary.sh`](bin/canary.sh), which reports **PASS**, **FAIL**, or **NOT MEASURED** — it never
lets "did not run" pass for "works".

The rule earns its keep. A model id can be accepted by the CLI, self-report correctly, and still be
served by a different model; only the usage record shows it. Findings like that are recorded as
dated probes, each with the literal command and its literal result.

## Design principles

- Change behaviour by editing Markdown, not by writing code.
- Ask in ordinary language. You never pick a model by hand.
- Use code only for what must not be guessed: identity, ordering, hashes, what a
  worker may change, and what may be deleted.
- Prefer deleting an unused framework to wiring it in because it exists.
- Keep private memory, credentials, and target data out of the public repository.

## Status

**v2.0.0** is the current release; **v1.1.1** was the first public one. It runs as the maintainer's
daily driver rather than as a demo. [CHANGELOG.md](CHANGELOG.md) records what each release changed,
including the parts that are still rough.

Start with the [documentation index](docs/README.md), the
[architecture](docs/architecture.md), or [how to add a specialist](docs/adding-a-specialist.md).

## Contributing and license

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md). Keep generated state, mailbox traffic, credentials, private memory, and authorized-target evidence out of commits.

Except where a file or an accompanying third-party notice says otherwise, the original contents of this repository are Copyright (c) 2026 Matthew A. and licensed under the **MIT License**. See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
