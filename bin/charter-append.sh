#!/usr/bin/env bash
# The one shell writer for an active thread charter's OPEN LOOPS.
#
#   bin/charter-append.sh <thread-id|path> <KIND> '<request> — why: <why>; resume: <exact return point>'
#
#   KIND  FOLD | DROP | 'QUEUE Q-007' | 'FOLD resolves Q-007' | 'DROP resolves Q-007'
#         DECISION  — writes a FOLD receipt AND one confirmed decisions.jsonl line
#                     (home product, scope [<thread-id>], statement = the request text)
#
# The UTC stamp is written by chrono_state.thread_charters.append_receipt;
# a timestamp or classification typed into the text is refused. Caller-supplied
# stamps can drift from the registry clock or put receipts out of order. A DROP
# receipt records an operator rejection so decisions and rejections can be
# found directly in the charter.
#
# Prints one JSON object (line, stamp, decision_id when DECISION); exit 3 on refusal,
# exit 4 when the receipt landed but the decisions.jsonl write failed.

set -uo pipefail

script_dir="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")" && pwd -P)" || exit 1
repo_root="$(cd -- "${script_dir}/.." && pwd -P)" || exit 1
# shellcheck source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"

if [[ $# -ne 3 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
fi

# VAULT_ROOT is exported by repo-root.sh (fixture roots override it), so the
# Python side resolves a bare thread id under that root, not this checkout.
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="${repo_root}/scripts/python${PYTHONPATH:+:${PYTHONPATH}}" \
    exec python3 -m chrono_state.thread_charters append --root "${VAULT_ROOT}" "$1" "$2" "$3"
