# Vibe Squad Routing

Chrono is the only controller and the only operator-facing voice.

```text
Operator -> Chrono -> gpt-codex | claude | gemini | grok -> specialists
```

Markdown is the interface. Chrono writes task packets; model leads execute them; specialists are markdown role files. This document is the **narrative source of truth** for how routing works. The **machine source of truth** is `shared/specialist-runtime-map.tsv` (per-specialist rows) plus the profile/policy registries; where a specific value is in question, the TSV and registries win.

The runtime map's structured route columns own lane, profile, and reviewer selection. Its `notes` column
describes role purpose, rationale, and tool constraints; do not maintain another routing table in that prose.

## 1. Routing principle — flat, quality-fit

Routing is chosen **per specialist on capability**, never by folder location.

- `source_namespace` — where the specialist markdown lives (`coding | security | content | sysmgmt | research | shared`). A **role/specialist-location label. It is not the mailbox (see `compatibility_namespace` below) and never chooses the model.**
- `compatibility_namespace` — a legacy compatibility label carried on the packet; it does **not** select the mailbox. Every packet is transported through the canonical mailbox root `departments/coding`, irrespective of this value (`shared/protocol.md` § board-native transport, enforced by `dispatch_context_builder.py` `CANONICAL_MAILBOX_ROOT`).
- `to_model` — which model/CLI vehicle executes the task, taken from the specialist's row in the runtime map. Each dispatch spawns a fresh CLI of that model; there are no persistent lane windows.
- Folder location, namespace, and mailbox never determine model choice. Two specialists in the same namespace can run on different lanes; the same capability class can span namespaces.

## 2. Per-specialist chain

Every specialist row carries a full chain, resolved from the profile registry:

- `primary_lane` + `primary_profile` — the best-fit lane for the work.
- `backup_lane` + `backup_profile` — a genuine second-best, **cross-family** on capability (different provider from primary).
- `escalate_lane` + `escalate_profile` — the stronger variant/effort, engaged by `escalation_policy`.
- `review_lane` + `review_profile` — a separate provider-family reviewer lane. Every `mandatory_review: true` packet must preserve `anti_affinity: author_family`; same-lane self-review never satisfies it.
- `throughput_lane` + `throughput_profile` + `throughput_policy` — the bulk/downshift route, gated (see §5).
- `failover_policy`, `escalation_policy` — versioned policy IDs (see §5–§6), not per-row prose.

`*_profile` values resolve through the **profile registry** to an exact model + effort + flags — e.g. `codex.astra.high`, `codex.astra.max`, `codex.sol.high`, `codex.sol.ultra`, `claude.opus5.xhigh`, `claude.fable.max`, `gemini.flash.high`, `codex.terra.default`. Claude also keeps `claude.opus.default` / `claude.sonnet.default` as **native in-lane fallback only** (`--fallback-model`), not standing lanes.

## 3. Lanes, models, and capability fit

Use the table for capability fit. Resolve each specialist's primary, backup, escalation, and review models
from its route columns and `shared/registries/profiles.tsv`; a lane can host several models and effort levels.
Current primary membership comes from the runtime map's `primary_profile` column.
Derive model membership and roster counts from the repository root instead of recording a static snapshot:

```sh
python3 -B - <<'PY'
import csv
from collections import Counter

with open("shared/registries/profiles.tsv", newline="") as handle:
    profiles = {row["profile_id"]: row for row in csv.DictReader(handle, delimiter="\t")}
with open("shared/specialist-runtime-map.tsv", newline="") as handle:
    specialists = list(csv.DictReader(handle, delimiter="\t"))
counts = Counter(
    (row["primary_lane"], profiles[row["primary_profile"]]["model_id"],
     profiles[row["primary_profile"]]["effort"])
    for row in specialists
)
print("lane\tprimary model\teffort\tspecialists")
for (lane, model, effort), count in sorted(counts.items()):
    print(f"{lane}\t{model}\t{effort}\t{count}")
print(f"Total specialists: {len(specialists)}")
PY
```

| lane | best-fit capability |
|------|---------------------|
| codex | implementation · tests · PoC · experimental probing · code review mechanics · graphics/runtime |
| claude | judgment · planning · safety/security reasoning · security defense · research/synthesis/long-context · developmental content · game/level/audio design |
| gemini | content/text · design · media/multimodal · large-context analysis · Google Search grounding; see `model-lanes/lane-capabilities.tsv` for grounding and authentication declarations |
| grok | advisory work and mapped research escalation; board launch constraints are described below. `read_file` ceiling ~25k tokens — use shell or paged ingest for large documents |

**Grok board launch constraints.** `scripts/python/dispatch_context_builder.py` builds Grok arguments with
`--disable-web-search` and requires `xai-api-key` authority; `model-lanes/lane-capabilities.tsv` declares
`xai-api-key-only`. That flag removes only general web search/fetch; native X search via
`x_keyword_search` and `x_thread_fetch` remain available under the launcher contract;
a current authorized probe must confirm their availability. The launch must not be described as
subscription-backed. Any separate search tool requires its own role-scoped availability and authorization check.

**Live-launcher model binding.** For every lane, the board attests the resolved
`shared/registries/profiles.tsv` row and passes that row's `model_id` as the native CLI's exact `--model`
argument. There is no lane-specific alternate model pin: selected profile, attested model, and launched model
remain aligned for Codex, Claude, Gemini, and Grok.

**Research default.** Decision-grade research fans the same question out in parallel to `research` across its three ranked routes (Gemini Argon primary, Codex backup, Grok escalate as an independent third family, launched with general web retrieval disabled and native X search available) → `synthesizer` merges and flags disagreements → `content-verifier` checks key claims against sources. Quick lookups use one worker. `arxiv_search` is available in any lane. This cross-lane fan-out replaces in-run Perplexity/xAI second opinions. Advisor membership and model families follow the runtime-map rows; blank-advisor behavior is defined in `shared/specialists/exodia.md`.

Bulk work uses the **gated throughput slot** only where the specialist's `throughput_*` fields permit it,
under the §5 downshift conjunction gate. Read `backup_*` separately for the operational backup;
a budget slot does not create a backup binding. Metered child calls require an external numeric budget ceiling.

**Gemini owns grounded bounty research.** `bounty-researcher` performs cited prior-audit, historical-exploit, incident, and taxonomy recon. Its outputs feed attack lanes but remain leads until heavy-hitter validation.

**Deep six-round research is a typed large-context handoff, not generic search grounding.** `research` and `bounty-researcher` keep grounded live search local, while substantive six-round investigations route to `large-context-analyst` on the lane selected from its runtime-map row. When that role runs on Claude, it may invoke `/ultra-research` only after a current slash-command discovery probe passes; a present-but-undiscoverable legacy plugin is `needs_tool`, never live availability.

**Claude and Codex are the heavy hitters and finding authorities.** Claude is judgment/security-reasoning primary; Codex is implementation, tracing, PoC, and test primary. They back up and review one another under anti-affinity. Agreement from any models is corroboration, not formal review.

The machine-enforced lane defaults, narrow primary exceptions, adapter templates, heightened-role set, and routing vocabulary live in `shared/lane-policy.tsv`. Markdown defines the policy; validators read that data and enforce it.

## 4. Tool-gated media axis

`tool_gated` is an **orthogonal axis, not a fifth capability class.** Media-production specialists (image/video/audio/voice/interactive-audio and similar) are gated by which lane's CLI is configured with the chrono-media-studio plugin (higgsfield/elevenlabs), so the model is secondary and routing pins to that plugin-host lane. The gate is the lane CLI's own MCP configuration, not a tmux pane: a board-spawned specialist gets a role-scoped MCP surface that `scripts/python/lane_capability_enforcement.py` narrows from the lane's configured servers down to the set its adapter declares, so a plugin the host lane does not carry is simply absent from the spawn. Where the required credential/provider is present on multiple lanes, prefer capability routing and validate tool compatibility; pin only when a required credential/provider is single-lane.

When a backup lane cannot invoke the required tools, it runs **specification-only (TBASF)**: it produces a blueprint (storyboard / SSML / EDL / event-map / code-spec), flags a clean `capability_gap` / `needs_tool`, and yields to Chrono to re-run the render on the real host. A TBASF blueprint terminates as `capability_gap`/`needs_tool`, **never `success`**.

## 5. Safety model

- **`safety_level`** (`low | medium | high`) is a **quality floor, not a change-level review detector.** `high` forces the strongest profile for the high-safety work; a budget slot never authorizes downshifting that work; packet review is derived separately from `review_triggers`. Complexity escalation is separate and signal-based.
- **`heightened_risk`** (boolean) marks defense-in-depth roles. The complete machine-readable role set lives in `shared/lane-policy.tsv`; it includes the security, exploit, incident, privacy, provenance, reconnaissance, supply-chain, and experimental-attacker roles that require the high-safety floor.
- **GLOBAL safety-refusal invariant.** A genuine safety refusal on **any** lane surfaces to the operator; the same request is **never cross-family re-dispatched in either direction** (Fable-refuses → do not shop to Sol; Sol-refuses → do not shop to Fable/Gemini). An operational block (overload/down/timeout) may inform a later, manually authored operator/Chrono board packet to the backup lane; no automatic redispatch exists. Refusals are classified by (1) structured provider/wrapper policy event, (2) typed terminal status, then (3) content heuristic **only to downgrade certainty** to `possible_refusal` + surface. A schema-valid 200-style response is terminal; short output is never treated as an operational failure.
- **`operator_gate`** — closed policy enum whose machine vocabulary lives in `shared/lane-policy.tsv`; Hard Rule 6 in `CLAUDE.md` states the corresponding policy set, and `scripts/python/tests/test_held_action_gate.py` requires both to equal the admission-time controller set. `production_mutation` (mutating a live production system that is not itself a public release) is **operator-ratified (2026-07-13)**. `requires_approval` in a brief is **harness tool names only** (`Write`, `Bash`, `WebFetch`, …) — domain gates live in `operator_gate`, never in `requires_approval`. Ordinary worker admission keeps all controller-held category tokens out of `action_scope`; this is not a per-tool-call approval mechanism. See `shared/protocol.md` § Held-category authority and logical scopes.
- **Downshift conjunction gate.** `throughput.downshift_gated.v1` permits a mapped cheap tier ONLY for sub-work whose task `safety_level == low` AND which contains no security/privacy/financial content AND carries the explicit per-task Chrono bulk flag `route_tier: throughput` (set via `ROUTE_TIER`). Slot availability is not per-task eligibility and never creates a per-specialist default. Preserve every row's safety level, safety tags, and heightened-risk metadata: sensitive or higher-safety work remains on its normal quality route. Chrono must isolate and classify eligible sub-work before downshifting; neither a filled slot nor the bulk flag lowers the parent work's safety floor. `throughput.never.v1` pairs with `none` lane/profile slots and forbids downshifting. Heightened-risk specialists retain no budget slot. Rows tagged `privacy`, `financial` or `dual_use` carry no budget slot unless `shared/lane-policy.tsv` names a `throughput_exception` for that tag and specialist; an excepted slot must stay on the row's primary lane. On `live_target` rows, budget sub-work is read-only.

Policy IDs (versioned): `failover.conservative.v1` · `escalation.signal.v1` · `escalation.safety_floor.v1` (mandatory for high/heightened) · `throughput.never.v1` · `throughput.downshift_gated.v1`.

Deterministic assignment and use rules:
```
if row.safety_level==high OR row.heightened_risk: escalation.safety_floor.v1
else:                                           escalation.signal.v1
if no budget slot:                              none / none / throughput.never.v1
else:                                           mapped lane / profile / throughput.downshift_gated.v1
if row.heightened_risk:                          no budget slot
use budget slot ONLY if task.safety_level==low AND no security/privacy/financial content AND Chrono bulk flag
failover_policy = failover.conservative.v1   (all rows)
```

## 6. Failover — signal and surface; redispatch is manual

`failover.conservative.v1` names the backup choice and conservative signal policy. The dormant automatic-failover subsystem is retired; there is no enable flag, sentinel, watcher, or alternate dispatch route.

- HARD signals (`dispatch_ack` failure, confirmed process exit, or a typed provider error) are evidence to surface, not a dispatch trigger. Ambiguous / slow / silent / missed-heartbeat / deadline observations also surface and never select or launch a backup.
- The ordinary board descriptor, receipt, and registry fences retain process and publication evidence; no parallel attempt ledger exists.
- After the native Claude fallback chain is observed terminal, the operator may direct Chrono to author a new ordinary board packet using the mapped backup. That packet passes the same dispatch, scope, gate, and review checks as any other task.
- **Opus** has distinct fallback and primary profiles. `claude.opus.default` (default effort, `native-fallback` flag) is the in-family overload/safety fallback and never a standing lane. Resolve standing primary membership from the runtime map's `primary_profile` column; the registry also contains `claude.opus5.high` / `.max` / `.xhigh`. Carve-out/heightened work exhausted on the in-family chain **surfaces** rather than laundering cross-family.

## 7. Dispatch contract

Every non-trivial task packet names:

- `to_model`: `gpt-codex | claude | gemini | grok`
- `specialist`: canonical specialist name
- `source_namespace`: `coding | security | content | sysmgmt | research | shared`
- `compatibility_namespace`: legacy label; the packet is always stored in the canonical `departments/coding` mailbox (see § 1)
- `write_scope`: exact writable paths, or `[]`
- `review_model`: read-only reviewer lane, or `none`
- `reviews`: `none` for ordinary work, or the canonical `TASK-YYYY-MM-DD-HHMM-<suffix>` ID this packet reviews (`shared/protocol.md` § Task Packet Frontmatter)
- `mandatory_review`: `true | false`
- `review_triggers`: explicit subset of `blast_radius | adversarial_claim | deciding_measurement | architecture` (the gate that defines these is `shared/protocol.md` § Mandatory Review Behavior — its one home)
- `parallel_safe`: `true | false`
- `direct_lane_work_allowed`: default `false`
- `operator_approved`: `true | false` (must be `true` for any `operator_gate` work)

### Dispatch is blocked when

- specialist is unknown or missing from the runtime map
- `to_model` or `review_model` is invalid
- `to_model` differs from the map without `model_override_reason`
- `mandatory_review` disagrees with the packet's validated `review_triggers`
- `mandatory_review: true` has `review_model: none`
- normalized `review_model` equals `to_model` for mandatory review, or otherwise violates distinct-family `anti_affinity`
- a deletion manifest is present without `operator_approved: true`; for other held categories, `operator_approved` records policy consent but does not grant ordinary worker action-time authority
- write scopes overlap in-flight work

Explicit operator approval is required by policy for every `operator_gate` action enumerated by Hard Rule 6. Ordinary worker admission denies declared held-category authority; deletion alone also has the file-exact integration gate described in `shared/protocol.md`.

## 8. Pointers

- Per-specialist rows: `shared/specialist-runtime-map.tsv` (machine source of truth).
- Mode workflows: `shared/modes/*.md`.

## 9. Dispatch shape

- **Single:** one specialist, one lane, one task and artifact.

Every board dispatch is a single packet with its own registry record, artifact, response, and verification
contract. Cross-lane comparison is controller-authored as multiple ordinary single packets. When those packets
must prove they answered the same frozen question, the controller may stamp the same general
`swarm_spec_sha256` provenance pin on each; the pin creates no parent record, child transport, or synthetic diff.

## 10. Selection discipline (which specialist, not just which lane)

After choosing the specialist, choose its execution lane by the ranked-availability rule owned by
`chrono/CLAUDE.md` § Dispatch.

The dispatcher enforces the map; the recurring failure is *selecting* the wrong specialist. These rules are canonical (the full task-shape table lives in `shared/specialists/triage.md`):

1. **Pick the most specific specialist for the task shape** — never a generalist by default. A generalist absorbing specific work starves the specific role and loads a weaker-fit prompt.
2. **Never route review / audit / verify work to an implementer.** Review belongs to `code-reviewer`, `skeptic`, `impact-validator`, `vibecoding-check`, or `content-verifier` (or the packet's `review_model`). An implementer reviewing lacks the reviewer's adversarial + `anti_affinity: author_family` discipline.
3. **`systems-engineer` is not the Codex-lane default.** Per its own brief it fires for genuine low-level / cross-arch / SIMD / runtime work only (~5% of coding work). Default general implementation to `backend-engineer`, infra/tool-wiring to `devops-engineer`, persistence to `database-engineer`, hot-paths to `performance-optimizer`, docs to `technical-writer`, review to `code-reviewer`/`skeptic`.
4. **Deliberately use the mapped lanes.** Select grounded research, large-context analysis, content/text, tool-gated media, summarization, and advisory specialists by task shape, then resolve their execution and review lanes from the runtime map. Apply primary eligibility from `shared/lane-policy.tsv` and the downshift gate to bulk work. Concentrating on a subset of lanes wastes the roster and the cross-family independence that review depends on.
