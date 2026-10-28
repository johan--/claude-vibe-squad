# Git hooks

Vibe Squad installs its private-memory leak guard and a reviewed validator bundle
in Git's private hooks directory. The installed executable is outside the mutable
worktree, so checking out another branch cannot replace code that Git runs on
your next commit.

## Install it (one-time, per clone)

From the repository root:

```sh
bash docs/install/install-pre-commit-hook.sh
```

The installer copies [`scripts/hooks/pre-commit`](../scripts/hooks/pre-commit),
the release-version validator, and the declared validator/dependency bundle to
Git's private hooks directory, then creates a small managed entrypoint. These
are regular files, not symlinks; the entrypoint and guard are executable. Re-run
the installer after reviewing validator or guard updates so the snapshots advance.

Do not point `core.hooksPath` at a tracked directory, and do not link a file in
`.git/hooks/` back into the worktree. Both patterns let the checked-out branch
replace code that runs with the developer's privileges. The tracked
`.githooks/pre-commit` file is not an installation target.

## Verify it

```sh
bash bin/doctor.sh --check-pre-commit-hook
git config --show-origin --get-all core.hooksPath  # expect no output and exit 1
git hook run pre-commit                            # expect validation output and exit 0 when checks pass
```

The focused doctor check resolves Git's effective hooks directory rather than
looking only at local config. It fails if a global or system `core.hooksPath`
still overrides the private directory, or if the entrypoint or copied guard is
missing, non-executable, or a symlink. The installer reports the origin of an
effective override and refuses to claim success until it is removed.

## What the local guard does

The installed `vibe-squad-pre-commit` snapshot rejects staged private-memory
artifacts:

- restricted-sensitivity frontmatter;
- paths beneath `_state/bounty/`;
- a literal `${CHRONO_VAULT_ROOT}` phantom path; and
- legacy `kg.db*`, `.db-wal`, and `.db-shm` artifacts.

It reads staged index blobs, so unstaged worktree content cannot hide a staged
restricted version or create a false rejection. Any failure to enumerate or
read the staged objects blocks the commit rather than passing silently.

The installer refuses an unrelated existing pre-commit hook by default. After
reviewing its bytes, pass `--replace-unmanaged-sha256 <sha256>` to authorize
replacement of that exact hook with a retained regular-file backup. The installer
never executes or composes the old hook. `bin/doctor.sh` treats an unresolved
installation as an issue instead of reporting the clone healthy.

## Repository-wide checks

The installed hook checks release-version agreement, then runs specialist and
live capability-home validation. Capability/registry and skill changes trigger
their respective validators; shell and dispatch-toolkit changes receive format
warnings. Staged `moat/` changes trigger Tier-A scanning when Node and the scanner
are available; the hook reports a skip when either is absent. Install the moat
dependencies with `npm ci --prefix moat` before rerunning the reviewed installer
so its snapshot includes TypeScript.

The local hook executes the reviewed validator snapshots, reading checkout files
as data. It never imports validator code from the mutable worktree. Public CI
also runs repository-wide checks. The private exact-target Tier-B scanner belongs
in private pre-push/CI enforcement;
see [`moat/boundary/README.md`](../moat/boundary/README.md).

The retired Spec-1.5 auto-snapshot check is intentionally absent: current
dispatch leaves Git untouched, so there is no snapshot to require.
