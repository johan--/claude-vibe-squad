#!/usr/bin/env bash
# Opt-in Claude hook. asyncRewake owns wait's exit status; never sends tmux keys.
set -uo pipefail

# Match the focus gate's exclusion BEFORE dependencies or stdin parsing.
physical_cwd="$(pwd -P 2>/dev/null || pwd)"
for guard_path in "${physical_cwd}" "${CLAUDE_PROJECT_DIR:-}"; do
    case "/${guard_path#/}/" in
        */_state/board-worktrees/*) exit 0 ;;
    esac
done
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || exit 0
repo_root="$(cd -- "${script_dir}/.." && pwd -P)" || exit 0
notice_root="${CHRONO_NOTIFICATION_ROOT:-${repo_root}}"
# No flag creation here or in settings. Missing flag is fast, silent and zero.
[[ -f "${notice_root}/_state/chrono/notification-hooks.enabled" ]] || exit 0

export VAULT_ROOT="${notice_root}"
pane_helper="${CHRONO_NOTIFICATION_PANE_HELPER:-${repo_root}/shared/chrono-pane.sh}"
# shellcheck source=../shared/chrono-pane.sh
source "${pane_helper}" >/dev/null 2>&1 || exit 0
declare -F chrono_pane_has_coordinator >/dev/null || exit 0
[[ -n "${TMUX_PANE:-}" ]] || exit 0
chrono_pane_has_coordinator "${TMUX_PANE}" || exit 0

# Read the same coordinator record the shared focus/pane gate just validated.
session_name="$("${TMUX_BIN:-tmux}" display-message -p -t "${TMUX_PANE}" '#{session_name}' 2>/dev/null)" || exit 0
[[ -n "$session_name" && "$session_name" != */* && "$session_name" != . && "$session_name" != .. ]] || exit 0
pidfile="${CHRONO_COORDINATOR_PIDFILE:-${notice_root}/_state/runtime/chrono-coordinator/${session_name}.pid}"
[[ -f "$pidfile" && ! -L "$pidfile" ]] || exit 0
coordinator_pid=""; coordinator_start=""
while read -r key value; do
    case "$key" in
        pid) coordinator_pid="$value" ;;
        start) coordinator_start="$value" ;;
    esac
done < "$pidfile"
[[ "$coordinator_pid" =~ ^[0-9]+$ && -n "$coordinator_start" ]] || exit 0
# Fast path for exec-based hooks. A forking helper may sit between this hook
# and Claude; inspect at most four ancestors from one snapshot in that case.
# Stop at the nearest Claude, so nested CLIs cannot inherit coordinator rights.
if [[ "$PPID" != "$coordinator_pid" ]]; then
    ps -eo pid=,ppid=,command= 2>/dev/null | awk -v current="$PPID" -v owner="$coordinator_pid" '
        { parent[$1] = $2; executable[$1] = $3 }
        END {
            for (hop = 0; hop < 4 && current > 1; hop++) {
                if (!(current in parent)) exit 1
                if (current == owner) exit 0
                if (executable[current] ~ /(^|\/)claude/) exit 1
                current = parent[current]
            }
            exit 1
        }
    ' || exit 0
fi

python_bin="${CHRONO_NOTIFICATION_PYTHON:-${repo_root}/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3)" || exit 0
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="${repo_root}/scripts/python${PYTHONPATH:+:${PYTHONPATH}}"
exec "$python_bin" -B -m chrono_state.notifications --vault "$notice_root" hook \
    --mode "${1:-context}" --owner-pid "$coordinator_pid" --owner-start "$coordinator_start"
