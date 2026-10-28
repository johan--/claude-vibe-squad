#!/bin/bash
# Chrono's open questions only; specialist notices stay in the lane-health badge.

set -uo pipefail

# shellcheck source-path=SCRIPTDIR source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"

questions="${VAULT_ROOT}/_state/chrono/awaiting-operator.md"
[[ -f "$questions" && -s "$questions" ]] || exit 0

# One local read per existing tmux refresh. Skip site initialization to keep
# startup small; only Python's standard library is needed, with no subprocesses.
exec python3 -S - "$questions" <<'PY'
from datetime import datetime, timezone
import re
import sys

# UTC ISO-8601, including fractional seconds and the equivalent +00:00 suffix.
entry = re.compile(
    r"-\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00))"
    r"\s*\|([^|]+)\|([^|]+)"
)
count = 0
oldest = None
try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as questions:
        for line in questions:
            match = entry.fullmatch(line.strip())
            if not match or not all(field.strip() for field in match.groups()[1:]):
                continue
            try:
                asked = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
            except ValueError:
                continue
            count += 1
            if oldest is None or asked < oldest:
                oldest = asked
except OSError:
    # A removal/replacement between the shell check and open is harmless.
    sys.exit(0)

if count:
    minutes = max(0, int((datetime.now(timezone.utc) - oldest).total_seconds() // 60))
    print(
        f"#[fg=colour213,bold]◆ needs you: {count} · oldest {minutes}m#[default] ",
        end="",
    )
PY
