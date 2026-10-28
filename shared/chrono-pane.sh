#!/usr/bin/env bash
# One home for "is the coordinator actually listening in that tmux pane?"
#
# Why this exists: both notifiers asked the question and both got it wrong, in
# different directions.
#
#   * outbox-watcher.sh compared `#{pane_current_command}` to the literal
#     "claude". The real value is the VERSIONED executable name -- measured
#     2026-08-16 on a live pane: "2.1.233". So the guard skipped even while
#     Chrono was running, every time, printing "no notification lost" as it
#     dropped the nudge. That is why board notifications went unseen for a whole
#     session.
#   * squad-monitor.sh had no check at all and typed alerts into whatever held
#     the pane. With Chrono exited that is a shell, which EXECUTES them:
#     "zsh: command not found: git", "zsh: no matches found: (26418 bytes)".
#
# Fixing one and leaving the other is how they drifted apart in the first place,
# so the rule lives here and both source it.
#
# The check is deliberately not a name match. tmux reports the FOREGROUND
# process name, which changes with every Claude release, so any literal is a
# time bomb. Look at real command lines instead -- version-independent.
#
# It checks TWO positions, because `#{pane_pid}` is not always a shell:
#
#   * a CHILD of the pane pid -- the normal case, where launch-squad.sh starts a
#     shell in the pane and Claude runs under it.
#   * the PANE PID ITSELF -- when the pane was respawned with `exec claude`,
#     Claude REPLACES the shell, so it is the pane process and has no shell
#     parent to be a child of.
#
# Measured 2026-09-17: a hand respawn using `exec $SHELL -lc 'claude'` made the
# child-only check fail while Chrono was running normally. Every board nudge was
# skipped for the rest of the session and nothing surfaced the skip, so lane
# completions landed silently and were only noticed by accident. That is the same
# class of silent-skip failure as the versioned-literal bug above, reintroduced
# by an assumption about process shape rather than about a name.

# chrono_pane_resolve_id <tmux-target>
# Prints the pane id (%N) the target names, or nothing when no pane matches.
#
# Verify the target EXISTS before trusting a pid from it. `display-message`
# silently falls back to the ACTIVE pane for an unresolvable target and exits
# 0, so a typo'd or stale target returns the wrong pane's pid and the caller
# cannot tell. Measured 2026-09-17: `-t squad:99.9` returned the live Chrono
# pane's pid with exit 0. Enumerate real panes and require an exact match.
#
# The match must accept every spelling a real caller uses, not just one:
#   * `%N`                    -- the pane id. Hooks inherit TMUX_PANE=%N, and
#                                chrono-focus-gate.sh passes it straight through.
#   * `session:index.pane`    -- what launch-squad.sh and squad-stop.sh use.
#   * `session:name.pane`     -- outbox-watcher.sh's `squad:chrono.0`.
# Measured 2026-09-19: the 2026-09-17 version matched only the index spelling,
# so the focus gate returned "no coordinator" on every one of 293 operator
# prompts and the watcher dropped every board nudge for two days, both silently.
# Window names may contain spaces, so the fields are tab-separated.
#
# This function stays a pure spelling->pane map and never inspects what is
# running: a bare `session:window` names no single pane, so it resolves to
# nothing here. Callers that mean "wherever the coordinator is in that window"
# want chrono_pane_resolve_coordinator_id below.
chrono_pane_resolve_id() {
    local target="$1"
    local tmux_bin="${TMUX_BIN:-tmux}"
    [[ -n "$target" ]] || return 1
    "$tmux_bin" list-panes -a \
        -F $'#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_name}:#{window_name}.#{pane_index}' 2>/dev/null \
        | awk -F '\t' -v t="$target" '$1 == t || $2 == t || $3 == t { print $1; exit }'
}

# chrono_pane_pid_runs_coordinator <pane-pid> <coordinator-pid> <start>
# The startup record in vs-welcome.sh supplies identity. A Claude name alone
# proves nothing: another worker or a background child must never receive keys.
# '+' in ps stat denotes membership of the terminal's foreground process group
# on both Darwin and Linux. A stopped/zombie process cannot receive a notice.
chrono_pane_pid_runs_coordinator() {
    local pane_pid="$1" coordinator_pid="${2:-}" start="${3:-}"
    local pid="" parent="" state="" cmd="" executable="" observed_start=""
    [[ "$pane_pid" =~ ^[0-9]+$ && "$coordinator_pid" =~ ^[0-9]+$ && -n "$start" ]] || return 1
    observed_start="$(ps -o lstart= -p "$coordinator_pid" 2>/dev/null | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//')"
    [[ -n "$observed_start" && "$observed_start" == "$start" ]] || return 1
    while read -r pid parent state cmd; do
        [[ "$pid" == "$coordinator_pid" ]] || continue
        [[ "$pid" == "$pane_pid" || "$parent" == "$pane_pid" ]] || continue
        [[ "$state" == *+* && "$state" != *T* && "$state" != *Z* ]] || return 1
        executable="${cmd%%[[:space:]]*}"
        [[ "$executable" == */claude* || "$executable" == claude ]] && return 0
    done < <(ps -eo pid=,ppid=,stat=,command= 2>/dev/null)
    return 1
}

# Validate one exact pane against the coordinator's own startup record. Read the
# record as data (never source it), require a complete fingerprint, and bind its
# recorded window/pane to this candidate as well as its PID. Missing records or
# unavailable process observations fail closed; callers leave notices queued.
chrono_pane_validate_coordinator() {
    local pane_id="$1" pane_pid="" session="" pidfile="" root=""
    local key="" value="" pid="" shape="" target="" start="" recorded=""
    local tmux_bin="${TMUX_BIN:-tmux}"
    pane_pid="$("$tmux_bin" display-message -p -t "$pane_id" '#{pane_pid}' 2>/dev/null)" || return 1
    session="$("$tmux_bin" display-message -p -t "$pane_id" '#{session_name}' 2>/dev/null)" || return 1
    [[ -n "$session" && "$session" != */* && "$session" != . && "$session" != .. ]] || return 1
    root="${VAULT_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}"
    pidfile="${CHRONO_COORDINATOR_PIDFILE:-${root}/_state/runtime/chrono-coordinator/${session}.pid}"
    if [[ ! -f "$pidfile" || -L "$pidfile" ]]; then
        # Direct `claude --resume ...` launches bypass vs-welcome.sh, the
        # record's writer. Report that lifecycle gap before process matching;
        # widening the executable match cannot repair a missing identity.
        printf 'chrono pane identity unavailable: pane=%s record=%s; launch/resume must register the coordinator PID and start time\n' \
            "$pane_id" "$pidfile" >&2
        return 1
    fi
    while read -r key value; do
        case "$key" in
            pid) [[ -z "$pid" ]] || return 1; pid="$value" ;;
            shape) [[ -z "$shape" ]] || return 1; shape="$value" ;;
            target) [[ -z "$target" ]] || return 1; target="$value" ;;
            start) [[ -z "$start" ]] || return 1; start="$value" ;;
        esac
    done < "$pidfile"
    [[ "$shape" == pane && -n "$target" ]] || return 1
    recorded="$(chrono_pane_resolve_id "$target")"
    if [[ -n "$recorded" ]]; then
        [[ "$recorded" == "$pane_id" ]] || return 1
    else
        chrono_pane_window_panes "$target" | awk -F '\t' -v p="$pane_id" '$1 == p {found=1} END {exit !found}' || return 1
    fi
    chrono_pane_pid_runs_coordinator "$pane_pid" "$pid" "$start"
}

# chrono_pane_window_panes <tmux-target>
# Prints "pane_id<TAB>pane_pid" for every pane of the WINDOW the target names,
# by index (`squad:3`) or by name (`squad:chrono`). Prints nothing otherwise.
#
# The target is matched against whole window identities rather than parsed for a
# trailing ".pane", because a window NAME may itself contain a dot -- splitting
# on the last dot would silently rename `squad:my.win` into a pane request.
chrono_pane_window_panes() {
    local target="$1"
    local tmux_bin="${TMUX_BIN:-tmux}"
    [[ -n "$target" ]] || return 1
    "$tmux_bin" list-panes -a \
        -F $'#{pane_id}\t#{pane_pid}\t#{session_name}:#{window_index}\t#{session_name}:#{window_name}' 2>/dev/null \
        | awk -F '\t' -v t="$target" '$3 == t || $4 == t { print $1 "\t" $2 }'
}

# chrono_pane_resolve_coordinator_id <tmux-target>
# Prints a validated coordinator pane ID, for either a pane or a window target.
# Exact spelling is resolved by chrono_pane_resolve_id; this function additionally
# proves identity and foreground ownership and rejects ambiguous observations.
#
# Why the window fallback exists: two live callers name a window, not a pane.
# registry_reconciler.py defaults CHRONO_TMUX_TARGET to `squad:chrono` and
# squad-stop.sh asks about `${SESSION}:chrono`. chrono_pane_resolve_id matches
# exact pane spellings only, so both got "no coordinator" while Chrono was
# running: measured 2026-09-22, 138 of 260 dispatch-log nudges were recorded
# `chrono-nudge queued-only` and never reached the operator's chat.
#
# Selection is by what is RUNNING, never by `#{pane_active}`. The chrono window
# holds two panes -- .0 runs the coordinator, .1 runs the dashboard -- so
# "the active pane" is a coin flip that types board notices into the dashboard.
chrono_pane_resolve_coordinator_id() {
    local target="$1" pane_id="" pane_pid="" found=""

    pane_id="$(chrono_pane_resolve_id "$target")"
    if [[ -n "$pane_id" ]]; then
        chrono_pane_validate_coordinator "$pane_id" || return 1
        printf '%s\n' "$pane_id"
        return 0
    fi

    while IFS=$'\t' read -r pane_id pane_pid; do
        [[ -n "$pane_id" && -n "$pane_pid" ]] || continue
        if chrono_pane_validate_coordinator "$pane_id"; then
            [[ -z "$found" ]] || return 1
            found="$pane_id"
        fi
    done < <(chrono_pane_window_panes "$target")
    [[ -n "$found" ]] || return 1
    printf '%s\n' "$found"
}

# chrono_pane_has_coordinator <tmux-target>
# Returns 0 when the coordinator CLI is the live foreground process there.
chrono_pane_has_coordinator() {
    chrono_pane_resolve_coordinator_id "$1" >/dev/null
}

# chrono_pane_observed_command <tmux-target>
# What is actually there, for a diagnosable skip message.
chrono_pane_observed_command() {
    local target="$1" pane_id=""
    # Same resolution as the check itself, so the skip message can never
    # describe the active pane while claiming to describe the target, and a
    # bare-window target reports the pane the check actually looked at.
    pane_id="$(chrono_pane_resolve_id "$target")"
    [[ -n "$pane_id" ]] || pane_id="$(chrono_pane_resolve_coordinator_id "$target")"
    if [[ -z "$pane_id" ]]; then
        printf 'unavailable'
        return 0
    fi
    "${TMUX_BIN:-tmux}" display-message -p -t "$pane_id" '#{pane_current_command}' 2>/dev/null \
        || printf 'unavailable'
}
