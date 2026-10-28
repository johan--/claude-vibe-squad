# Model Runtime Map

Status: canonical specialist → model runtime map
Owner: Chrono / harness-optimizer

Vibe Squad routes work as:

```text
Operator -> Chrono -> board dispatch -> one fresh specialist CLI per task
```

Chrono is the only controller. Each specialist runs as a freshly spawned,
capability-scoped CLI in its own git worktree, bound to the model its runtime-map
row selects — model binding is **per specialist**. `codex`, `claude`, and
`grok` name their executables; the `gemini` routing identifier resolves to
Antigravity's `agy` executable. These are task vehicles, not standing lanes. The
`departments/` tree is compatibility storage for canonical specialist markdown,
memory, and mailboxes until it can be safely renamed or retired.

## Canonical Data

The source of truth is `shared/specialist-runtime-map.tsv`. Derive the current row count and primary-lane
distribution from that file instead of copying a mutable snapshot here. Its canonical schema has 29
tab-separated fields, in this order:

1. `specialist` — unique canonical specialist name.
2. `source_namespace` — one of `coding | security | content | sysmgmt | research | shared`; the specialist markdown location. **Not** the mailbox — transport always goes through `CANONICAL_MAILBOX_ROOT` (`dispatch_context_builder.py:96`) — and never the model. Canonical statement: `CLAUDE.md` Hard Rule 3.
3. `capability_class` — the work-shape used for quality-fit routing.
4. `safety_level` — `low | medium | high`.
5. `safety_tags` — structured dual-use/privacy/financial and related tags.
6. `tool_profile` — named tool-host constraint, or `none`.
7. `primary_lane` — `codex | claude | gemini | grok`; primary eligibility is governed by `shared/lane-policy.tsv`.
8. `primary_profile` — model/effort/flags key in `shared/registries/profiles.tsv`.
9. `backup_lane` — genuine cross-family operational backup.
10. `backup_profile` — backup profile registry key.
11. `escalate_lane` — lane used by the escalation policy.
12. `escalate_profile` — stronger profile registry key.
13. `escalation_policy` — versioned policy registry key.
14. `review_lane` — independent reviewer lane, or `none` where permitted.
15. `review_profile` — reviewer profile registry key, or `none`.
16. `anti_affinity` — reviewer-separation constraint, or `none`.
17. `throughput_lane` — optional cheap-tier slot (Claude, Codex, or Gemini in the current map); availability does not establish per-task eligibility.
18. `throughput_profile` — optional bulk profile.
19. `throughput_policy` — versioned downshift policy; the low-safety, non-sensitive, explicit per-task flag conjunction is defined in `shared/routing.md` §5.
20. `failover_policy` — versioned operational-failover policy.
21. `operator_gate` — closed list of actions requiring explicit operator approval.
22. `heightened_risk` — `true | false` defense-in-depth marker.
23. `requires_approval` — harness tool approvals declared by the brief.
24. `required_tools` — required verified tool capabilities.
25. `preferred_tools` — optional verified tool capabilities.
26. `notes` — non-empty one-line role description.
27. `tags` — routing/catalog metadata.
28. `version` — row contract version.
29. `operator_model_consult` — `true | false`; whether operator-model consultation is part of the routed contract.

`bin/validate-specialists.sh` is a thin wrapper: the live engine is `scripts/python/validate_specialists.py`, followed by the capability-home gate in `scripts/python/validate_capability_homes.py`. It rejects a header that is neither the canonical 29-field header nor the 28-field legacy header (legacy rows are normalized by defaulting `operator_model_consult` to `false`), and rejects any row whose width does not match the declared header. It also validates lane/profile and policy foreign keys, source namespaces, capability and safety enums, cross-family primary/backup separation, primary eligibility (read from `shared/lane-policy.tsv`), review independence, high/heightened-risk escalation floors, throughput gates, operator gates, tool-profile compatibility, specialist briefs, and native adapters. Folder location must never be used to infer model choice.

## CLI Vehicles

Model binding is per specialist. These are the CLIs a specialist row can be bound to, and what each is typically chosen for:

| CLI | Provider | Best fit |
|---|---|---|
| Claude Code (`chrono`) | Anthropic | operator conversation, planning, dispatch, conflict prevention, synthesis — the coordinator, not a routable specialist |
| `codex` | OpenAI | implementation, repo edits, tests, refactors, PoC mechanics |
| `claude` | Anthropic | judgment, safety review, privacy/auth reasoning, memory hygiene, adversarial challenge |
| `gemini` (`agy` executable) | Google | multimodal analysis, media generation routes, visual/design review, grounded content |
| `grok` | xAI | one primary (`smokey`, the blank advisor); escalate route for `research` and `bounty-researcher`. Board launch requires API-key authority and disables general web retrieval while retaining native X search; `shared/routing.md` § Grok board launch constraints owns that policy. `read_file` hard-fails past ~25k tokens — large documents need shell or paged ingest |

## Dispatch Algorithm

1. Chrono identifies the operator-approved mode and desired artifact.
2. Chrono selects the canonical specialist.
3. Chrono looks up the `primary_lane`/profile, independent `review_lane`/profile, `source_namespace`, safety/tool constraints, and versioned policies in `shared/specialist-runtime-map.tsv`.
4. Chrono assigns one write owner for each path in `write_scope`.
5. Chrono adds read-only review when the packet's `review_triggers` list is non-empty (`mandatory_review: true`); see Mandatory Review below.
6. The board spawns a fresh CLI for that specialist in its own git worktree; it executes the specialist brief only and does not become an independent controller.
7. Chrono gathers results, resolves conflicts, and speaks to the operator.

## Mandatory Review

`mandatory_review` is a per-packet property, not a property of the specialist or
the content class. It is true exactly when the packet's `review_triggers` list is
non-empty; `safety_level` and content or severity categories never manufacture a
change-level review. The trigger vocabulary and the single distinct-family review
rule have one home — `shared/protocol.md` § Mandatory Review Behavior — and are
not restated here (root `CLAUDE.md` Hard Rule 10).

When a packet carries a non-empty trigger list, Chrono routes a separate reviewer
from a different provider family after the response lands. Reviewers are read-only
unless Chrono serializes a later write pass.

## Compatibility Policy

Generated agent config files are allowed only when a target CLI requires them for startup. They must point back to canonical markdown and be validated. Do not maintain duplicate specialist definitions by model family.

## Verification

Current required gates:

- `bash bin/validate-specialists.sh`
- `bash bin/product-hygiene.sh --public-export`
- `bash bin/memory-audit.sh`
- `bash bin/mcp-audit.sh`
- `bash bin/doctor.sh`
- `squad up` topology check for windows: `chrono` and `watchers/status` (specialists are per-task child processes, not windows)
