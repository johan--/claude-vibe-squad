# Vibe Squad documentation

New here? Start with [Getting started](getting-started.md). It covers the private
memory boundary, native model CLI authentication, the health check, and the first
launch.

- [Install](install/README.md) — the supported install path in order, with a
  **check** after every step. The guide itself marks which steps are required and
  which are optional.

## Understand the system

- [Architecture](architecture.md) — the coordinator, isolated workers, review,
  and settlement flow.
- [Model runtime map](model-runtime-map.md) — how a specialist is routed to
  Codex, Claude, Gemini, or Grok.
- [Project](../shared/modes/project.md) — the build and delivery workflow.
- [Bounty](../shared/modes/bounty.md) — the authorized security workflow.

Ordinary in-house work defaults to the `modeless` engagement state, with no mode
file or capability card. Project and Bounty are the two typed modes, chosen
deliberately with operator consent. Content and media workflows live under
Project; see the [engagement-state guide](../README.md#three-engagement-states-one-conversation).

## Operate and extend

- [Private configuration](private-config.md) — what must stay off Git.
- [Production readiness](production-readiness.md) — release checks and known
  limits.
- [Git hooks](git-hooks.md) — local leak and consistency checks.
- [Add a specialist](adding-a-specialist.md) — extend the Markdown roster.
- [Security tooling](tooling/security-arsenal-guide.md) — optional security
  CLIs and pilots, with starting commands.

## Where the truth lives

Vibe Squad is Markdown-first. Human-readable mode, capability, specialist, and
policy files define intent and judgment. Small scripts enforce the boundaries
that should not depend on judgment: process isolation, declared write scope,
independent review, private-data separation, and atomic settlement.

This index lists maintained public documentation. Working notes, private
operator data, migration records, and release evidence are intentionally not
part of the public export.
