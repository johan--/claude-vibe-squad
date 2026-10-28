# Changelog

<!-- Enforced by scripts/python/validate_release_version.py:
Level-two headers whose first whitespace-delimited token is Unreleased or
[Unreleased] (case-insensitive) are skipped, including suffixes like " - TBD"
or " (next)". The first remaining level-two header must be vX.Y.Z
(case-insensitive), optionally followed by " - YYYY-MM-DD", " — YYYY-MM-DD",
or " (YYYY-MM-DD)". Other release header text is rejected; a released header
is required.
-->

## v2.0.0 - 2026-10-28

This release refreshes model routing and replaces the old research-tool workflow
with parallel research across three provider families, synthesis, and source
verification. It is the first public release to include the privately prepared
v1.1.10 changes below.

Major version: Kimi and research-tool removals break existing routes and workflows.

### Added

- [Added] **Budget routes.** Eligible specialists now have throughput slots across
  Claude, Codex, and Gemini. Selecting one requires explicit
  `route_tier: throughput`; an omitted tier never selects a budget profile.
- [Added] **Sensitive-tag budget guard.** The validator rejects throughput slots
  on heightened-risk rows and on privacy, financial, or dual-use rows, except for
  the named same-lane memory-curator exception. Chrono still classifies each task's
  safety and content before permitting a downshift.
- [Added] **Disk-cleanup pools.** Idle scratch-pool children expire after 48 h.
  Explicit cleanup runs can prune old Codex daemon and Claude CLI versions,
  keeping the active version and newest other version; in-use versions stay.
- [Added] **History packing.** Archived dispatch history and rescued documents
  older than 30 days go into monthly compressed packs. Member lists, sizes, and
  content hashes are verified before originals are removed.
- [Added] **Cleanup pause.** An operator pause file suspends periodic disk sweeps
  while leaving manual surveys available.
- [Added] **Workboard tools.** `bin/workboard-append.sh` provides one event writer;
  `compact` archives history while preserving open items, their identities, and
  current focus. The live ledger is no longer tracked in Git.
- [Added] **Charter receipts.** `bin/charter-append.sh` stamps and validates
  receipts. Its `DECISION` kind also records a confirmed decision in the journal.
- [Added] **Follow-up settlement.** `scripts/send-task.sh --supersedes` closes a
  replaced task after the new dispatch is admitted; dry runs and failed
  admissions leave it open.

### Changed

- [Changed] **Model map.** Refreshed specialist primaries and review routes around
  Opus 5.5 and Fable 5.1, with Haiku 5.5, Terra (`gpt-6.1-terra`), and Gemini Flash
  budget profiles. Gemini primary routes use Gemini 4 Argon, except `summarizer`,
  which uses Gemini Flash. Codex Sol profiles use `gpt-6.1-sol`.
- [Changed] **Research workflow.** Decision-grade questions go to research workers
  across three provider families in parallel, then to `synthesizer` to reconcile
  disagreements and `content-verifier` to check key claims against sources. Quick
  lookups use one worker; `arxiv_search` remains in the research arsenal.
- [Changed] **Current guidance.** Specialist briefs, skills, capability cards, and
  runtime docs now reflect the active lanes and tools, with stale routing
  instructions and broken links repaired.
- [Changed] **Disk hygiene.** Commit `5749c8f3` (2026-09-30) removed the
  `DISK_HYGIENE_AUTODELETE=1` opt-in named in `v1.1.10` for guarded, eligible
  regenerable-pool sweeps (browser clones, stale test temporary directories, and
  test databases) during lock- and cadence-gated `--periodic` passes; rescued
  artifacts remain alert-only with no automatic deletion.
- [Changed] **Rescue copies.** Build output and caches are skipped with a manifest
  of omitted paths and sizes. `.work` directories remain preserved work product.
- [Changed] **Nightly maintenance.** Reaps orphaned MCP-spawned Chrome processes
  without restarting the persistent browser, and runs `uv cache prune`.

### Fixed

- [Fixed] **Busy-path checks.** Match full paths and descendants at path boundaries
  so sibling names do not block cleanup and live references retain their files.
- [Fixed] **Test PostgreSQL maintenance.** The database census parses numeric OIDs
  correctly and uses a configured cluster role. Log rotation now includes the
  configured test cluster's stderr log.
- [Fixed] **Focus anchor.** Takes the next action from the active charter's latest
  receipt resume point, with a workboard fallback when unavailable.
- [Fixed] **Resume capsule.** Excludes settled completion receipts, collapses older
  decisions to a journal pointer, removes stale operator-instruction replay, and
  reports the full archived-debt count when details are collapsed.
- [Fixed] **Outbox retries.** Status-less responses retry every 5 seconds for at
  most 10 minutes or 120 polls; responses older than 7 days are skipped.
- [Fixed] **Board landing.** Worker results with an unknown, empty, or synthesized
  status are preserved for review instead of merged. A job that finishes while
  another is landing retries up to three times instead of being refused.
  External work-repository hooks are never executed, and a broken hook there no
  longer refuses clean work.
- [Fixed] **Doctor dispatch volume.** The 24-hour count includes dispatch records
  without a task ID, so the volume ceiling can no longer be bypassed by them.
- [Fixed] **State archive.** Settled tasks that still owe a notice or
  notification stay in the active registry until it is delivered.
- [Fixed] **Completion notices.** A task that finished cleanly was filed to the
  coordinator's queue but never announced in its pane, while its record showed
  the notice as delivered. Every finished task now gets a pane notice; more than
  three in one sweep still arrive as a single digest.
- [Fixed] **Linux CI.** Hermetic tests no longer assume macOS tools, Homebrew
  paths, or macOS file-timestamp behaviour.

### Removed

- [Removed] **Kimi lane.** Retired its launcher and runtime support, profiles,
  adapters, validator requirements, capability declarations, and current docs.
- [Removed] **Research integrations.** Dropped the xAI search, Perplexity, and
  Firecrawl tools and their credential requirements from the research arsenal
  and runtime setup.
- [Removed] **Guarded security MCP wrappers.** Retired `guarded-semgrep`,
  `guarded-slither`, and `guarded-solodit`. The audit skills now use installed
  Semgrep and Slither CLIs directly; Solodit lookup uses a separately provisioned
  prior-art service or manual searches.

## v1.1.10 - 2026-09-26

Runtime routing now reaches the model and effort level it selected, board workers start with a
smaller capability surface, and host maintenance is safer by default. This release also adds the
review workflow used to harden a site before launch.

### Added

- **Site pre-ship review.** The new `site-preship-review` skill coordinates focused design,
  mobile, state, flow, launch-readiness, and security/infrastructure reviews, followed by bounded
  fix passes. Its checks include rendered-page inspection, accessibility, links, client-bundle
  secret scanning, redirect and security-header validation, and launch metadata.
- **Safer disk hygiene.** Board disk-pressure monitoring now reports the largest campaign storage
  users while the existing preservation-first worktree pruner remains scheduled. The new sweeps
  for rescued artifacts, browser clones, and stale test temporary directories are opt-in with
  `DISK_HYGIENE_AUTODELETE=1`, and their storage-pool records now expire.
- **Operator and authoring tools.** Namespaced `/vs-*` commands, richer board-status and
  needs-attention displays, external-task artifact locators, and the `x-reply-craft` skill make
  routine operation and content handoff easier to inspect.

### Changed

- Grok routes now use Grok 4.7. Claude routes were refreshed for Opus 5.5, and heightened
  offensive/code-security routes use the registered Daybreak profiles.
- Codex route-tier model and reasoning effort are now written into each attempt's isolated
  `CODEX_HOME`; generated adapters no longer override the selected effort.
- Claude board workers start only the plugin MCP servers projected for their specialist instead
  of launching every configured plugin server.
- Browser-media admission now forecasts 1 GiB p95 use with a 2 GiB reserve, replacing the older
  2/3 GiB estimate. Subscription-backed OpenAI image generation is also routed through the Codex
  CLI while the paid-media gate remains unchanged.

### Fixed

- Receipt reconciliation preserves settled reviews and decision diagnostics, notification
  delivery is generation-aware and pane-bound, and recovery reports the real artifact location
  and terminal state instead of silently downgrading or duplicating events.
- Board worktree pruning now handles owning repositories and external worktrees without treating
  preserved untracked work as disposable.
- Empty or malformed xAI search responses now remain typed `no_results` outcomes instead of
  becoming ambiguous failures.
- Public-export policy explicitly denies `media/launch-video/**` and `media/x-*/**`, so private
  launch assets cannot become publishable through a future broad allow rule and do not appear as
  unclassified release paths.

## v1.1.9 - 2026-09-20

Work that is not the squad itself now runs in its own repository. Plus the export step the
capability system was missing.

### Added

- **External work repositories.** A packet can name `work_repo: /absolute/main-checkout`
  (or `WORK_REPO=` through `scripts/send-task.sh`) and the board dispatches against that
  repository instead of the squad repo. Squad configuration — roles, adapters, registry,
  mailbox, memory — still comes from the squad root; the work repo supplies the worktree, its
  own base branch, both ignore checks, the worker's cwd, and the integration target.
  `write_scope` is relative to the work repo. Absent means today's squad-repo behaviour
  exactly.

  The value must be a git *main* checkout (not a linked worktree) outside the squad root with
  an attached current branch; preflight and the dispatcher each refuse anything else and never
  guess a branch. Worker commits land on `board/<TASK-ID>`. The rail never fast-forwards,
  checks out, or pushes the work repo's base branch; merging it is a separate, deliberate step
  this rail does not take.

  Skills are not projected into an external worktree. A relative read-context path that is
  absent from the work repo falls back to the squad root, so a relative skill path still
  resolves; an absolute squad-root path is unambiguous and does not rely on that fallback.

- **A capability-projection step in the exporter.** Provider assignments that are private can
  now be withheld from the public candidate (`public_projection: {withheld_providers: [...]}`)
  instead of the private source being gutted to achieve a clean public surface. The public
  candidate still discloses the withheld provider's operation name and the withheld list
  itself; that is deliberate, not an oversight.

  The same change **restores `chrono-dedup` as an available private provider**: its
  `prior_art_check` operation is linked again, it is added to the Grok lane surface, and it is
  assigned to the security specialists. Private deployments gain that capability back; the
  public projection is what withholds it.

- **Task evidence is excluded from the public export.** Board worker run artifacts under
  `tools/export/task-evidence/` are now denied by the export path policy. They are retained
  privately and no longer reach a published candidate.

### Fixed

- The launcher contract regression tested the wrong configuration root after the split, so it
  could not have caught a real break.
- `bin/send-task.sh`, `bin/vs-cancel-spawn.sh` and the registry each carried an assumption
  that the worktree and the squad root were the same repository.

### Known rough edge

For the **squad repo itself**, the rail still integrates in-scope worker commits onto its bound
base branch (normally `main`) before that task's review settles. This is not limited to tasks
that complete: when only the return path fails, committed in-scope work is still integrated by
the recovery path while the task is reported as `blocked`. External repos are protected by the
`board/<TASK-ID>` branch described above; the squad path is not.

Reviews do gate registry settlement — a non-`APPROVE` verdict fails closed and the entry is
never marked complete. **They do not gate the commit, and nothing reverts it automatically.**
Backing out a rejected change on the squad path is a manual step.

## v1.1.8 - 2026-09-19

A repair release. Two guards that had failed silently, and the coordinator's first
direct measure of its own context.

### Fixed

- The tmux coordinator check accepted only `session:index.pane`, while the focus-gate hook
  passes the pane id (`%N`) and the outbox watcher passes `session:name.pane`. Both callers
  got "no coordinator" and exited silently, so the focus gate skipped every prompt and every
  board nudge was dropped, including `needs_human` returns that then sat unseen. The helper
  now resolves any of the three spellings against tmux's own pane list and queries by pane
  id; a regression test drives it with a fake tmux (`shared/chrono-pane.sh`,
  `scripts/python/tests/test_chrono_pane_helper.py`).
- The resume capsule's `NEEDS HUMAN` lines carried only an id. They now carry the specialist,
  the lane and the specialist's own first line, bounded and path-safe.
- Two shared briefs told workers to use `status: blocked` for clarification questions;
  `needs_human` is the state that reaches the operator.

### Added

- `bin/context-tripwire.sh`, a `UserPromptSubmit` hook for the coordinator session: it reads
  the last real usage record of the coordinator's own transcript and injects a WARN at 400k
  tokens and a HARD stop at 600k (env overrides). Synthetic zero-usage records after API
  errors are skipped. Registered in `chrono/.claude/settings.json`.
- Optional `skills_applied` / `skill_gaps` keys on the worker response envelope.
- The nightly run now renders the memory curation queue so it is read at a session boundary.
- The writing specialists carry a real prose skill (`copy-refinement`, plus
  `de-ai-before-delivery` on the claude lane) and a new `persuasive-technical-writing` skill;
  `writing-skills`, which authors SKILL.md files, was retired from them. Adapter capability
  hashes were refreshed across 98 adapters after that change.

### Changed

- `shared/lifecycle.md` no longer claims there is no token counter for the coordinator.

## v1.1.7

The release where nothing was broken and almost everything was mis-measured. v1.1.6 hunted
guards that reported success while doing nothing; this one found them in the dispatch layer
itself, and found that the coordinator's own claims about the system were the least reliable
evidence in it.

### Guards that failed silently

- The write_scope conflict check skipped entirely when the task registry was absent, printing
  nothing — so a dispatch that never checked and one that checked and found nothing were
  indistinguishable from the transcript. The repo's own header already called this class
  unacceptable and had closed only its worktree instance.
- Four more in the same shape: a tree-health gate that could fall open with no trace in the
  receipt, a corrupt registry dying with a blank reason and the wrong diagnosis, frontmatter
  validated by a whole-file regex so packet body text could suppress a refusal, and MCP tool
  verification disabling itself when no audit log was found.
- The vault-root validity guard was inert for 32 of the 42 shell wrappers that source it, and an
  exported root silently overrode location derivation so a worktree wrapper resolved to the
  parent checkout.

### Prompt injection, in the site nobody checked

- A prompt-injection guard was applied to one of three places that build a prompt. The two
  unguarded ones were an authenticated HTTP route and the path that runs after every board
  response — both with no tool allowlist. Found only because a cross-family review was asked
  what the previous two same-family reviews had missed.
- The agent definition confining the child's tool surface was written non-atomically: 3.4% of
  concurrent reads saw a definition with no allowlist.

### Measurement discipline

- CI had failed on `main` every scheduled run for four days over two tracked bytecode files.
  Nothing surfaced it because nothing read the result.
- Two test files were red on `main` and no CI job ran either.
- A capability probe reported the gemini lane dead. It had tested the retired standalone CLI;
  the lane dispatches through agy, which answers. Three public documents now say so, because
  that one mis-measurement nearly moved sixteen specialists onto worse-fitting lanes.

Shipped on top of v1.1.6 (`git tag v1.1.6`, 2026-09-15); the `v1.1.7` tag is applied to `main`
by Chrono after this work is pushed, so it never names an unpushed tree.

## v1.1.6

A release-hardening release. The defect shape v1.1.5 named — something broken that reported success —
recurs throughout, and this release found more of it: a publication gate that could certify the leak
it was written to catch, a thrash detector resolving 2 of 40 dispatches, a syntax gate reading one
file of seventy-six. Shipped on top of v1.1.5 (`git tag v1.1.5`, 2026-09-01); the `v1.1.6` tag is
applied to `main` by Chrono after this work is pushed, so it never names an unpushed tree.

### The publication gate

- The gate that certifies a clean public export could certify the leak it was written to catch. Three
  review rounds closed a bypass: merging the clean baseline into an old, disjoint lineage classified
  as a clean contribution; a name-keyed allowlist that would transfer its acceptance to any new
  lineage sharing the same ref; and an audit that printed PASS having evaluated zero refs. The
  classifier now decides on contribution ancestry, the allowlist is SHA-pinned rather than name-keyed,
  and an audit that reaches an empty ref set fails instead of passing.
- A known exposure is now reported rather than suppressed. One retained pre-clean-slate PR ref is
  SHA-pinned and reported as accepted risk on every run; the moment that ref moves off its pinned SHA
  it falls back to a leak, so the exception cannot silently widen into a blanket allow.

### Routing

- The gemini lane moved to Gemini 3.8 Flash. `gemini.flash.default` resolves to `gemini-3.8-flash-medium`, and a new `gemini.flash.high` profile pins `gemini-3.8-flash-high`. Nine specialists inherited the primary bump through the profile ID alone; no runtime-map row had to change for it.
- The gemini escalation tier left `gemini-3.1-pro-high`. Twenty references across eighteen specialists — seven primary, thirteen escalation — now resolve to `gemini.flash.high`. Measured before the rebind: Artificial Analysis Intelligence Index 59 against 48, output 305 against 103 tokens/second, blended price $0.58 against $1.74 per 1M. Both models carry a 1M-token context, so `large-context-analyst` gives up nothing by escalating to Flash. Google Search grounding was confirmed live on `gemini-3.8-flash-high` against the 3.1 Pro baseline, which the `research`, `bounty-researcher` and `content-verifier` routes depend on.
- Two parity probes went through the ordinary board and both returned PASS. `research` on `gemini-3.8-flash-high` self-reported the pinned model, grounded a September 1 2026 news item with real citations, and failed closed on an undeclared capability; `accessibility-engineer` on `gemini-3.8-flash-medium` did real discipline work, executed a declared `chrono-vault` operation, and reported the absent one as `needs_tool` rather than substituting. `shared/api-catalog.md` moves Google Search grounding from `verified: needs-research` (last checked 2026-05-02) to `verified: yes` on that receipt.
- Gemini escalation is now the same model at higher effort, which is what the claude (`fable.xhigh` -> `fable.max`) and codex (`sol.high` -> `sol.ultra`) lanes already did. `gemini.pro.deep` stays in the registry pinned to 3.1 Pro, still `usage: escalation`, but no specialist row selects it any more. Whether a profile is bound is derivable from `shared/specialist-runtime-map.tsv` in one pass, so it is not written into the registry where it could go stale.

### Lane capabilities

- `sequential-thinking` is registered on the agy-backed gemini lane and declared in its `mcp_surface`. Gemini was the only lane without it; codex, claude, grok and kimi all had it. The defect was not a missing declaration — 17 of the 18 gemini specialists already asserted `availability: available` with `evidence: lane-inventory`, and the lane inventory never contained the server, so adapters projected a capability the runtime refused. A board probe confirmed `sequentialthinking` executed and returned thought-tracking state.
- `chrono-recon` is registered on the agy-backed gemini lane. It was already in gemini's declared `mcp_surface` but absent from the CLI's own configuration, the same declared-versus-live gap. The same probe: `dns_enumerate_tool` returned real A, MX, NS and TXT records for `example.com`, and a negative control against an unregistered server was refused verbatim.
- Not fixed here: `bin/doctor.sh`'s remaining MCP warning is `mcp-context-protector`, which is absent from disk. `scripts/bootstrap-mcps.sh` reports it but does not install it, and it manages only the four chrono servers, not `chrono-recon`.

### Memory

- The autocapture distiller was losing memories at roughly half rate for one afternoon, and the upgrade in this same release caused it. `autocapture` pointed at `gemini.flash.default` — the specialists' profile — so moving that to Gemini 3.8 Flash moved a background text transform with it. Measured over one fixed input on 2026-09-02: `gemini-3.8-flash-medium` produced usable JSON in 5 of 10 runs, `gemini-3.7-flash-medium` in 5 of 5, `gemini-3.8-flash-low` in 10 of 10. The medium failures were exit 0 with EMPTY stdout and a stderr saying a tool permission was auto-denied because headless mode cannot prompt: the stronger reasoning tier reaches for a tool during a job that has none, and the run dies wearing a success code. `_parse_distilled` then reports "distiller returned no JSON object" and the note is gone.
- The distiller now has its own profile, `gemini.flash.distill` -> `gemini-3.8-flash-low`, so a specialist-facing model change cannot silently move a background job again. This is the coupling that caused the incident, not the model choice.
- An unparseable answer is retried once (`DISTILL_MAX_ATTEMPTS`). A timeout and a non-zero exit still raise on the first occurrence: those states persist across a retry, and re-running a timeout costs another 120s to learn nothing. Verified by two tests that fail without the retry, plus 10 live runs with zero distillation failures.
- Context for the standing doctor warning: of the 74 failures in its 7-day window, 44 were the retired-Gemini-CLI auth error fixed at `4928b84b` (last seen 2026-09-01) and 29 were 120s timeouts (last seen 2026-08-29). The window is backward-looking; it was not reporting a live outage.

### Security

- `vault_get` (chrono-vault MCP) joined the caller's path onto the vault URL without normalising it, so `../../etc/passwd` read outside the vault. Measured before the fix: `../commands/` resolved to `/commands/`. All five traversal shapes now return `invalid vault path`.
- `prior_art_check` (chrono-dedup) fetched any URL it was given, including after a redirect, so a model-supplied address could reach loopback and RFC1918 hosts. Public destinations only, re-checked on every hop: 127.0.0.1, 169.254.169.254 and 10.0.0.5 are refused; ordinary public hosts still resolve.
- The README claimed each task "runs in its own sandbox" and "cannot touch anything else", contradicting a correct sentence 45 lines later. Worker actions are isolated by git worktree, not sandboxed, and the README now says so.
- The launchd agents that keep the board alive execute code from the mutable working checkout, and they source the operator's shell secrets first. This release documents that plainly rather than implying isolation. Separating the executed-code root from the data root is deferred and tracked, not done here.

### Checks that could not fail

- `dispatch_preflight` swallowed real git failures. `_git_paths` caught `OSError`/`TimeoutExpired` and returned `()` one level below the handler that exists to emit `advisory_scan_failed`, so a git that timed out, a missing git binary, and a non-repository root all produced the output of a clean scan. The three tests covering that handler inject `RuntimeError`, which the function does not catch — they proved the handler works while never exercising how git actually fails. Still fail-open; no longer fail-silent. `git grep`'s exit 1 for "no matches" stays a real answer.
- Several more diagnostics reported healthy while measuring almost nothing, each now fixed with a test that fails against the old behaviour. The thrash detector resolved 2 of 40 dispatches and never scanned a source namespace carrying 15% of traffic, so a repeat-ID storm and malformed dispatch rows in that namespace were invisible; it now discovers namespaces from the logs, bounds them, and keeps dedup marker labels distinct. The documented shell syntax check validated one file of seventy-six. The capability validator was blind to 73 retired demands. And the code reviewer had every output channel switched off, so it ran and reported nothing. The release audit that passed over an empty ref set is a fourth instance of the same shape, fixed under the publication gate above.

### CI

- One gate, not thirty test bugs. `dispatch_context_builder` refuses when a lane executable is absent, and 30 test files inherit it, so a runner without the five CLIs failed them all. Four rounds of patching the individually-named failures could not converge. A single stub fixture replaces the per-test patches those rounds added, and a negative control proves `SQUAD_CI_HOST_INDEPENDENT=1` does not weaken the production gate. The registry suite now runs all 7 tests instead of hiding 2.
- `git commit` ends by spawning `git maintenance run --auto --quiet --detach`, which daemonizes and keeps repacking a fixture repo the test is already deleting, so cleanup died with `Directory not empty`. 31 test files build such a repo. Reproduced on Linux at 266 of 300 trials, 0 of 300 with the guard. `gc.auto=0` alone does not fix it on every git version; `maintenance.auto` names the spawner.
- The CI base branch is now bound at the dispatch and preservation call sites rather than left to the workflow environment. A first fix set a workflow-level base-branch env and required snapshot configuration; the env half was the wrong layer and was reverted, while the reaper half that binds the base at the call site stayed. Isolated doctor-hook and reconciler dependencies, plus repaired dispatcher-guard and capability fixtures, let the affected suites run.
- The settlement census the acceptance test asserts on was refreshed to match the prose-only evidence and the elicit-instruction evidence, and the evidence count is now pinned so a future drift fails the test rather than passing quietly.

### Documentation

- README counts were overstated: 250 skills is 99 once `.agents/` and gemini mirrors collapse, and 16 MCP servers is 12. The version badge said v1.1.4 while the text below it said v1.1.5.
- README linked to two paths the public export strips, one of them holding a single denied file — so the directory did not exist publicly at all. A gate now refuses any published document that links to a stripped path, using the projector's own policy module so the two cannot disagree. The remaining public-facing pointers to withheld files were repaired, and the visual-verify mirror the wiring validator checks was refreshed so it passes on the real content.
- The root docs' licence identifier and a stale demo caption were corrected in the same count-fixing pass, and the AGPL plugin declarations are now registered in `THIRD_PARTY.md` so the licensing surface matches what ships.
- Guidance that had outlived its subject was removed: the retired-review guidance, a retired vault flag, and a Grok auth-policy claim that no longer held. The triage, skeptic and cleanup doctrine now matches `shared/specialist-runtime-map.tsv` rather than an older routing story.
- The narrated quick-start omitted a step whose absence made the launcher refuse to start; that and three other launch-blocking gaps in the walkthrough are repaired, along with un-followable install steps elsewhere in the docs.

## v1.1.5

A reliability release. Almost nothing here adds a feature — it makes the system tell the truth about itself. One defect shape recurred throughout: **something broken that reported success.** Shipped on top of v1.1.4 (`git tag v1.1.4`, 2026-08-31).

### Work that was being destroyed

- The nightly reaper preserved untracked residue on one branch of a fork and force-removed the other branch without looking inside it. Both branches now share one census, so the divergence cannot be rewritten.
- A second path found later: `git status` reports an unreadable directory as a **warning with exit 0** and emits no entry for it, so a scan that ran blind over a subtree was indistinguishable from one that found it empty. That had never cost a file only because the same permission problem also blocked the delete.
- Shutdown's group census started at "in scope" and stayed there if the census itself failed to run, so a transient failure could group-kill an unrelated process. Late-forked children were escaping the shutdown snapshot and surviving as orphans.

### Checks that could not fail

- Nine health checks reported green while the thing they guarded was broken. Each was verified by deliberately breaking its subject. The MCP probe never invoked a tool — `usable=true` meant a handshake completed. `memory-audit` passed with an entire department's memory deleted. `dispatch-toolkit-verify` passed when the toolkit could not run at all, because it only read the documentation.
- `bash -n a.sh b.sh c.sh` checks the **first** file and binds the rest as arguments. Both CI workflows had it, so the syntax gate was checking one file of sixty-six.
- The capability validator's `--self-test` passed while `main()` was stubbed to `return 0`.
- `launch-squad.sh` exited 0 whether or not the coordinator started. Two separate causes had been fixed without ever adding the gate; it now proves the coordinator is live before reporting success.

### Review and settlement

- Eight review rules had no test. Removing any one let a task close itself silently while every existing test passed. Negative controls added, each proven by mutation.
- `docs/standards/settlement-guard-coverage.md` now maps all 36 settlement guards and which are genuinely under test — **including the ones that are not**. Indexed by guard message rather than line number, after the first version's line numbers went stale by +67 within three commits.

### Capability corrections

- Memory autocapture had been dead for 12 days and 73 notes, because two parts of the system disagreed about which CLI to run and nothing checked they agreed.
- Kimi **can** load skills; a code comment denied it on the strength of a probe that had since expired.
- `dispatch-toolkit-verify` was auditing the retired `gemini` binary rather than `agy`, producing six phantom warnings that buried one real finding.
- Projected specialist skills were collected and never surfaced to the worker.

### Cleanup

- 4 GB reclaimed with receipts; 1,218 archive files and 1,366 orphaned outbox files removed; 22 local and 20 remote merged branches deleted.
- **Six of eight proposed deletions were refused after verification.** One "dead" function was called from 17 of 21 sites; one "removable" tool was claimed by six documents and its removal failed a gate.

### Known and recorded, not fixed

- `claude-fable-5-1` silently runs as Opus 5 on this account — the CLI validates the id, the model self-reports as 5.1, and only the usage record shows otherwise. See `docs/probes/`.
- The V1 settlement rail has no unforgeable binding between a landed response and its attempt.
- One error made during the cleanup itself: three referenced outbox files were deleted by a filter that compared paths without normalising both sides. Recorded with its root cause, and the two committed reapers were verified not to share the flaw.

## v1.1.4

Adds a fifth model family and makes the board reliable enough to trust with long work. Shipped on top of v1.1.3 (`git tag v1.1.3`, 2026-08-27).

### The grok lane

- Added `grok` as the fifth model lane with `smokey` as its advisor, native agent profile format, and adapters that are rejected outright if they carry no ranked routes. Grok binds as the `escalate_lane` for `research` and `bounty-researcher`.
- Applied Sol's ratified routing proposal across the roster — 13 primary rebinds — after an independent probe established what each lane actually exposes. Final distribution across 71 specialists: claude 31, codex 21, gemini 16, kimi 2, grok 1.
- The prose layer, README, brain map, model-runtime map and skill-home doc now all say five lanes. `docs/routing-map.html` is generated from `shared/specialist-runtime-map.tsv` rather than hand-maintained.

### Dispatch and settlement reliability

This is the bulk of the release, and most of it was found by measuring 275 blocked board tasks rather than by design review.

- **Finished work now reaches you even when the run behind it dies.** The detached supervisor inherits a PATH without Homebrew, so `python3` resolved to a 3.9 that has no `tomllib`, and `validate_capability_homes.py` died on import — reported as "candidate tree health check refused residue promotion", which reads as a verdict on the repository. Fixed at the environment: the supervisor already preserves the real PATH as `TRUSTED_HOST_PATH` for worker launches, and the health check now uses it too.
- **A busy machine now makes a task wait instead of dropping it.** `host_admission.py` returns `{"action": "queue", "backoff_seconds": N}` under momentary load, and the caller turned that into `die` while printing the word "queued". A busy host converted every correct dispatch into a dead one. It now retries on the admission's own backoff.
- **A task is judged on what it changed, not on the machine it ran on.** The gate ran live capability-home existence — whether `halmos`, `myth` and `anchor` are installed — at the moment of promoting a worker's residue, which a worker cannot influence and a sanitized supervisor PATH cannot answer (140 diagnostics versus 0, decided only by PATH). Live existence keeps its home in the pre-commit hook.
- **File permissions say what they mean.** Scopes are prefix paths compared on path components, so `dir/**` was a literal component matching no file — a permission that read as granted and behaved as empty, which flagged seven correct edits out of scope.
- **When something is refused, it says why.** The controller emitted the last 4000 characters of a validator that prints one passing line per file, so every stored error began mid-token inside a run of `"status":"pass"` and the cause had scrolled off. It now reports the lines that explain the failure and states how many passing lines it dropped.
- Deleted the skill-reference census: added ten hours after the last green CI run, it resolved skills through the maintainer's local plugin cache and never passed CI once.
- `bin/test` no longer OOM-kills the host. `run_moat_fast` called `node --test` with no concurrency cap, and `node --test` defaults to the CPU count — a 10-wide burst on a saturated machine, invisible to a search for concurrency flags because the parallelism is a default rather than a flag.

### Correctness of the guidance itself

- The gemini lane's skill bridge was a fourth, undeclared copy that received no content updates. `defi-invariant-check` was missing 43 lines — the entire 2026 oracle-robustness and governance section — so a specialist on that lane was auditing DeFi against the pre-2026 invariant set. Resynced, with the blob group declared so the validator now covers it.
- Bounty mode reduced to a kernel; the `KILLED` exit is closeable and archiving has a contract.
- `should_compact()` and `validate_worktree_outputs()` deleted — the first because the threshold rule lives in markdown, the second because it was genuinely dead.
- The `known-bugs` bench is classified private, keeping deferred work out of the public export.

**Release note.** This tag is cut on the correctness of its own changes rather than a fully green CI
run: six of seven jobs pass, and the remaining gate is red on tests that cannot execute in that
environment. Every change touching dispatch or settlement was reviewed by a different model family
than the one that wrote it.

## v1.1.3

The simplification release. Everything below shipped on top of v1.1.2 (`git tag v1.1.2`, 2026-08-22); the `v1.1.3` tag is applied to `main` after this work is pushed, so it never names an unpushed tree.

- Removed the fan-out and swarm dispatch transports entirely — about 5,500 deletions across the registry reconciler, dispatch context builder, and send path, with no transport CLI flag left behind. Chrono dispatches parallel lanes directly and specialists already self-fan-out through their own subagents, so the second transport was machinery with no caller.
- Dropped the `advisory` mode explored during this cycle; clearance, the verification contract, and the dispatch context builder stay closed to the two workflows, Project and Bounty.
- Wired `child_mcp_policy` instead of leaving it declared and unread: adapter validation now enforces it, so a `lead-broker-only` capability actually requires the native Kimi prompt to carry the MCP-unavailability notice.
- Consolidated work-state onto a single parser, projection, validator, and entry point, and migrated the live plan into one append-only workboard whose validator refuses any item that lacks a `why` and a `resume_action`.
- Collapsed duplicate operator notifications on both the promoted-response and terminal-receipt paths.
- Derived review-settlement provenance from the registry rather than trusting the reviewer's own frontmatter.
- Pinned guards that were live but untested with mutation-proven tests: the anti-affinity cross-family review — which a bare `if False:` could have disabled while the whole suite stayed green — and the compact subagent safety rails.

### Update — 2026-08-28

Four items deferred at the v1.1.3 publish, folded back in rather than pushed to a later release.
Shipped under the same version.

- **A lane's committed work no longer strands when only its return path fails.** A block inside the
  return-path window now integrates the worker's committed, in-scope code through the same gated
  path the success path uses, and names the commit on the receipt. The receipt still reads `blocked`
  and still exits 75 — recovery never reads as settlement. Proven on a deliberate no-envelope
  dispatch, and it went on to recover a real lane that died at its wall the same night. The operator
  notification was corrected in the same change: it read the preservation record only, so it reported
  recovered work as stranded while the commit sat on `main`.
- **`modeless` dispatch.** A prepared packet may omit `mode:` entirely; the controller renders that
  absence as an affirmative `modeless` token whose authority is the intersection of `project` and
  `bounty` on every axis, with the memory write floor computed from the aperture row rather than
  looked up, so no future column can widen it. A dropped or unknown mode is rejected, not admitted.
  The generating wrapper still refuses an omitted `--mode`: omission there is an unmade choice, and a
  wrapper must never invent a packet field. This also closed a real bypass — omitting `mode` used to
  skip verification-contract derivation altogether.
- **Canary defects fixed.** `--emit-packet` now produces a packet that passes admission unaided, and
  the skills probe reads a task-bound source that survives dispatch, so it can reach `PASS`.
- **The board's platform-bridge MCP surface is measured, not assumed.** A live worker probe
  enumerated its callable tools, and a per-server disable control from the main checkout suppressed
  the surface while the positive control restored it. It is an ordinary configurable MCP server,
  so the existing allowlist seam governs it.


## v1.1.2

The board-native release (`git tag v1.1.2`, 2026-08-22). Everything below shipped on top of v1.1.1, whose own
entry follows and is kept as the May marker it was.

- Kept Chrono as the single operator-facing coordinator while moving specialist work to fresh native Claude, Codex, Gemini, or Kimi CLI processes in isolated worktrees.
- Kept all 68 Markdown specialist briefs and exactly two workflows: Project and Bounty.
- Added exact process/receipt identity checks and artifact-first, envelope-last settlement so stale or partial work cannot appear complete.
- Preserved cross-family review routes and explicit operator holds for destructive, external, paid, credential, production, and public-release actions.
- Kept durable memory as private Markdown outside the repository, with a disposable lexical recall index.
- Converted unattended cleanup jobs to report-only while storage ownership and retention facts are completed.
- Removed disconnected policy, receipt, memory-store, and generated-output prototypes instead of turning them into parallel product architecture.
- Reworked onboarding and public-export boundaries for a simpler, more honest public release.
- Expanded production-path, adversarial, recall, and export tests; a configured surface is not called working until its real route is probed.

## v1.1.1

The first public release (`git tag v1.1.1`, 2026-05-03). Six reviewer-flagged bugs fixed on top of v1.1.0 the same day:

- `validate-specialists.sh`: an awk range collapse had made the citation checks a silent no-op, so the previously reported "39 PASS" proved nothing about them. Fixed the flag-pattern extraction for MCPs, skills, and peers.
- `dream_light.py`: replaced an over-broad `\b[A-Za-z0-9]{32,}\b` redaction — which ate git SHAs, UUIDs, and base64 — with 17 specific secret patterns (AWS, GitHub, Anthropic, Slack, JWT, Stripe, HuggingFace, OpenAI, xAI, Perplexity, Apify, Google, Discord, URL bearer params).
- `doctor.sh` retry-storm detection: pointed it at tmux CLI stdout instead of cleanup-log docs, where it could never have fired; tightened to MCP-specific failure patterns within the last hour.
- `doctor.sh` token-bleed: added a `dispatch-log.jsonl` signal alongside the artifact-count proxy, catching retry loops that produce a single artifact.
- `vibecoding_check.py`: implemented the project-mode `git_clean`, `new_code_has_tests`, and `no_destructive_ops` checks. Only `tests_pass` had existed, while the docstring claimed all four.
- `vibecoding_check.py`: the CVSS check now validates CVSS:4.0 vector structure and score range. Previously `cvss_v4: 'lol'` passed.

## v1.1.0

Tagged 2026-05-03. The v1.1 body of work, over 20 commits:

- Upgraded 39 specialist files to the v1.1 schema and added `bin/validate-specialists.sh` to enforce it, plus `bin/upgrade-specialists.py` to automate the migration.
- Updated the five LEAD.md files with v1.1 sections and repaired the broken specialist references in their decision trees.
- Added `shared/lifecycle.md` — nine lifecycle rules plus per-pane effort defaults — and per-pane effort/thinking flags in `launch-squad.sh`.
- Added `shared/api-catalog.md`, a full API/feature catalog with verified flags, following a capability inventory of CLI flags and MCPs per pane.
- Added `bin/aggregate-errors.sh` (nightly error aggregation), `spawn-specialist.sh`, and `graduation-scan.sh`.
- Added a first-week token-spend guardrail.
- Reworked two skills: the smart-contract checklist and the bounty report format.
- Fixed Claude MCP tilde-literal paths and flipped the Gemini `chrono-*` MCPs to `verified: yes` after a dispatch-toolkit reality check.

## v1.0.0

Initial public release target for the local Vibe Squad command center:

- Chrono coordinator.
- Four model leads: GPT/Codex, Claude, Gemini, Kimi.
- Markdown-first modes, specialist briefs, model lead prompts, task packets, and memory surfaces.
- Filesystem mailbox dispatch with tmux windows.
- Safety gates for review, public release, live sends, credentials, cleanup, and high-blast-radius work.
