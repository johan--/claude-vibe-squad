#!/usr/bin/env bash
# The one shell writer for _state/chrono/OPEN-WORK.md.
#
#   bin/workboard-append.sh queue    --alias ID --summary S --why W --resume R
#   bin/workboard-append.sh start    --alias ID --summary S --why W --next N
#   bin/workboard-append.sh restate  --alias ID|--work-id W-… --summary S --why W --next N
#   bin/workboard-append.sh advance  --alias ID|--work-id W-… --next N
#   bin/workboard-append.sh switch   --alias ID|--work-id W-… --next N
#   bin/workboard-append.sh block    --alias ID|--work-id W-… --resume R [--successor X --next N | --idle]
#   bin/workboard-append.sh complete --alias ID|--work-id W-… [--successor X --next N | --idle | --waiting X --resume R]
#   bin/workboard-append.sh archive  (same shape as complete)
#   bin/workboard-append.sh drop     --alias ID|--work-id W-… --summary S --why W [focus flags as complete]
#   bin/workboard-append.sh fold     --request-id ID --target ID|W-… --summary S --why W --next N
#   bin/workboard-append.sh idle
#
# It wraps chrono_state.workboard.append_event: work_id, event_id and the UTC
# stamp are generated, the required facts are checked before any file is
# opened, and the append is refused unless the simulated projection reflects
# it. Hand-written ledger rewrites can introduce malformed events and trigger
# fail-open gate warnings. Never build an event line by hand.
#
# Prints one JSON object (event_id, work_id, line) on success; exit 3 on refusal.

set -uo pipefail

script_dir="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")" && pwd -P)" || exit 1
repo_root="$(cd -- "${script_dir}/.." && pwd -P)" || exit 1
# shellcheck source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
fi

# VAULT_ROOT is exported by repo-root.sh; workboard.WORKBOARD_PATH reads it, so
# the default --path is <VAULT_ROOT>/_state/chrono/OPEN-WORK.md.
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="${repo_root}/scripts/python${PYTHONPATH:+:${PYTHONPATH}}" \
    exec python3 -m chrono_state.workboard append "$@"
