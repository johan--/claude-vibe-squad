---
name: disk-cleanup
description: Use when the Mac is low on disk or before a build/release/large-media job, to find where space went and reclaim it safely in tiers. Triggers on "disk is full", "free up space", "out of disk", low-free-space warnings, or at session start before heavy work. Measures first, then deletes only regenerable/settled pools; never touches keys, staged OS updates, or in-flight work.
audience: chrono
---

# Disk cleanup (measure first, then reclaim in tiers)

This skill measures reclaimable pools, then reclaims the safe ones in tiers.
One command does the survey and the apply; it is `dry-run` by default and deletes
nothing until you pass an apply flag.

## When to run

- **Session start**, and **before any build, release, or large-media job**.
- **Whenever free space is under 40 GB** (survey) — act before the 25 GB floor
  where board builds and media jobs start failing.
- After a burst of board lanes, which leave worktrees, `/tmp/vs` sandboxes, and
  caches behind.

## 1. Measure first — never delete blind

```bash
bin/disk-hygiene.sh --plan      # full survey + sized plan, deletes NOTHING
```

`--plan` prints: APFS container free, staged-update (Preboot) allocation, board
worktrees, the rescued-artifact archive, Chrome clones, the test-Postgres
cluster, the top temp allocations, vault snapshots, docker usage, and the
rescued regenerable-bulk plan. Read it before applying anything. For a raw look:

```bash
diskutil apfs list                         # container free vs ceiling
df -Pk /System/Volumes/Data | tail -n1     # data-volume Available (KiB)
du -sh /private/tmp/* 2>/dev/null | sort -h | tail        # tmp hogs
du -sh ~/vault-snapshots/* 2>/dev/null | sort -h          # snapshot pile
docker system df                                          # image/container space
```

## 2. Tiered reclaim

```bash
bin/disk-hygiene.sh --apply-safe        # Tier 1 only
bin/disk-hygiene.sh --apply-preserve    # Tier 1 + Tier 2
```

**Tier 1 — automatic & safe (`--apply-safe`).** Regenerable or settled only:
- Settled board worktrees (preserve-first via `prune-board-worktrees.sh`) and
  their `/tmp/vs` sandboxes + codex-homes — only when the owning attempt is
  terminal, not live-referenced, and `lsof`-idle.
- Chrome `code_sign_clone.*`, recognised `/tmp` lane caches/scratch older than
  12 h, idle per-run test databases older than 6 h, and `VACUUM FULL` of a
  bloated idle configured base test database. Direct children of
  `/private/tmp/chrono-scratch/` use a **48 h** idle TTL; the container stays.
- **Tool versions** (explicit Tier 1): Codex daemon releases keep the
  `~/.codex/packages/app-server-daemon/current` target plus the newest other
  release by mtime. Claude versions keep the `~/.local/bin/claude` target plus
  the newest other version. `ps` and `lsof` must prove every removed version
  idle, including mapped binaries. Missing/broken active links or failed
  censuses retain versions. These pools run only on explicit plan/apply paths.
- **Vault snapshots**: keep the newest 3, delete older tarballs in
  `~/vault-snapshots`.
- **Docker/colima**: prune dangling (untagged) images + stopped containers that
  exited over 24 h ago, then `colima` `fstrim`. **Never** `system prune -a`, and
  **never** a container prune while any container is running (a running
  container means board/release work may be in flight).

**Tier 2 — preserve, then delete (`--apply-preserve`).** Adds, on top of Tier 1:
- **Rescued-artifact bulk**: inside `_state/rescued-worker-artifacts`, delete
  only regenerable subtrees (`.cache`, `dist`, `.work`, `node_modules`,
  `target`, `venv`, …) under tasks that are terminal and unreferenced. The rescue
  **documents are retained** — they may be the only surviving work product.

**Tier 3 — operator only (never automated).** Surfaced by the survey, acted on
only by the operator: operator-managed storage and protected data, including
anything holding keys or signatures.

## 3. Registry hygiene (not disk, but it hides real blockers)

Stale `needs_human` / review-required tasks whose work already shipped inflate
the tmux `blocked:` badge and bury real blockers. Close them as superseded (with
reasons) via the normal reconciler — this is a Chrono action, not part of the
delete path.

## Hard protections (enforced in code, not just documented)

Never deleted or read, in any tier:
- In-flight attempt ids, process-referenced paths (`ps`/`lsof` idle required).
- `wallet.key` (exact), `*.keychain-db`, any name containing the configured protected-profile substring, and the
  built-in and locally configured pinned-tool globs are skipped by `protected_tmp_name`, and such a name inside an
  otherwise-regenerable subtree aborts that subtree — but only on a sweep whose caller passes
  `protect=True` (`scripts/python/disk_hygiene.py`; the default `protect=False` applies no name
  guard). Sensitive local configuration under `~/.config/` is never a sweep pool. `*.sig` and other arbitrary key/signing
  names are NOT name-protected — do not rely on the sweep to spare them.
- The tmux socket dir `/private/tmp/tmux-*`, operator/Chrono `claude-*` scratch.
- The rescued-artifact archive's documents, and any staged OS update.

Deletion is preserve-first and idempotent: a failed idle/ownership/reference
check retains the whole tree. If a pool needs a judgement call, `--plan` it and
decide; do not widen the delete.

## Private host configuration

`_state/disk-hygiene.local.json` holds private temporary-pool patterns, pinned-tool
exclusions, the protected-profile substring, the test PostgreSQL directory and
base database name, and `postgres.superuser`. This ignored host file is
provisioned separately from code by Chrono; never read sensitive local configs
or infer the PostgreSQL role from the OS login. Example PostgreSQL section:

```json
{"postgres": {"data_directory": "~/.local/share/example-test-pg", "database": "example_test", "superuser": "fixture_admin"}}
```

`superuser` is the existing cluster role (1–63 ASCII letters/digits/underscore/
hyphen, starting with a letter or underscore). No role is created. Missing or
invalid `superuser` skips connections, including survey queries. The socket,
port 55432, database-family and cluster-identity checks remain enforced.
`bin/rotate-logs.sh` uses the configured data directory for `launchd.stderr.log`,
with the daemon-log size cap and archive retention (defaults: 16 MiB, 3 archives).
It copies/compresses and **truncates in place**, preserving the open inode.

The shell entry point passes its resolved `VAULT_ROOT` to the child pruner as
both working directory and environment. A board/nightly caller targeting the
main squad must pass that root explicitly; there is no fallback to another
checkout's private configuration. `bin/doctor.sh` warns when that root's temp
protection policy is absent or invalid, even when free disk space is healthy.
Missing or invalid selection/protection settings skip the entire temporary-pool
and rescued-bulk cleanup classes with a notice; missing or invalid PostgreSQL
settings skip that database class before connecting. Unknown protection never
means an empty protection list.

## Where the logic lives

- `scripts/python/disk_hygiene.py` — pools, guards, and the `cleanup_plan`
  entry point. `bin/disk-hygiene.sh` — the CLI (`--check` is read-only and runs inside
  `bin/doctor.sh`, nightly via `run-nightly.sh`; `--periodic` runs every 15 min from
  `bin/outbox-watcher.sh`'s timer; `--plan`/`--apply-safe`/`--apply-preserve` are this skill).
- `bin/prune-board-worktrees.sh` — the preserve-before-delete worktree pruner.
- `bin/chrono-status-segment.sh` — the tmux `disk NNG` gauge (red under 25 GB).
