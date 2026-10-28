#!/bin/bash
# Regenerate _state/chrono/resume.md from live state (decisions + board registry).
#
# Why an entry point rather than a push from the sweep: the capsule's only consumer is
# a session-start read, and a capsule regenerated AT read time cannot be stale by
# construction. A push-based writer is only as fresh as its last successful tick and
# goes quiet when the daemon is not running — which is exactly how the previous file
# reached ten days old without anyone noticing. Pull-at-read is the correctness
# property; wiring this into the reconcile sweep is an optimization on top of it, and
# safe to add because the write is atomic and idempotent.
#
# The capsule is derived purely from the journals and the registry. It carries no
# operator line: without a live producer, `## Latest operator instruction` could
# present stale instructions as current. The `--latest-turn` / `--session-id`
# compaction-snapshot recovery is removed so a stored turn cannot reintroduce
# that stale context.
#
# Pure stdlib — deliberately no `uv`, so session start never blocks on a toolchain.

set -uo pipefail

# shellcheck source-path=SCRIPTDIR source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"

usage() {
    cat <<'EOF'
Usage: bin/chrono-resume-capsule.sh [--stdout]

  --stdout   Render to stdout and write nothing.
EOF
}

MODE="write"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stdout) MODE="stdout"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

PYTHONPATH="${VAULT_ROOT}/scripts/python" VAULT_ROOT="${VAULT_ROOT}" \
    python3 - "$MODE" <<'PYEOF'
import sys

from chrono_state import resume

mode = sys.argv[1]

if mode == "stdout":
    sys.stdout.write(resume.render_capsule("current") + "\n")
else:
    dest = resume.write_capsule("current")
    print(f"resume capsule regenerated: {dest}")
PYEOF
