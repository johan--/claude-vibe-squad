#!/bin/bash
# Install the Vibe Squad launchd routines (LaunchAgents) from the templates in launchd/.
#
# The optional launchd daemon adds the status-bar daemon segment and HTTP tool bridge.
# `bin/squad up` runs without it; an installed but broken/foreign daemon still blocks launch.
#
# Usage:
#   bash bin/install-routines.sh                # install all repo-managed routines, then load
#   bash bin/install-routines.sh --daemon-only  # install and load only the optional daemon
#   bash bin/install-routines.sh --dry-run      # render + validate, change nothing
#   bash bin/install-routines.sh --status       # report installed/loaded state
#   bash bin/install-routines.sh --force        # replace a differing installed plist
#   bash bin/install-routines.sh --uninstall    # unload + remove the managed routines
#   bash bin/install-routines.sh --uninstall --dry-run  # preview removal
#
# --daemon-only also limits uninstall. The monitor is an optional managed agent.
# Chrome belongs to the operator: --status reports it; lifecycle commands refuse it.

set -euo pipefail

# shellcheck source-path=SCRIPTDIR source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"

# Daemon selected by --daemon-only. Keep this list in sync with DAEMON_LABEL in bin/launch-squad.sh.
REQUIRED_AGENTS=("com.vibesquad.daemon")
# Installed by default for parity with previous behavior, but not required to launch.
OPTIONAL_AGENTS=("com.claudevibesquad.nightly" "com.vibesquad.dream" "com.chrono.squad-monitor")
# Canonical reporting-only set. Keep it separate from every lifecycle selection,
# including the test override below: Chrome holds the operator's authenticated
# sessions (bin/squad-stop.sh's persistent-CDP-Chrome protection).
OPERATOR_AGENTS=("com.vibesquad.chrome")

# Test seams. Both default to real behavior; they exist so the install path can be
# exercised without mutating the operator's live launchd session.
LAUNCHAGENTS_DIR="${SQUAD_LAUNCHAGENTS_DIR:-${HOME}/Library/LaunchAgents}"
NO_BOOTSTRAP="${SQUAD_INSTALL_NO_BOOTSTRAP:-0}"

DRY_RUN=0
STATUS_ONLY=0
FORCE=0
DAEMON_ONLY=0
UNINSTALL=0

for arg in "$@"; do
    case "${arg}" in
        --dry-run)     DRY_RUN=1 ;;
        --status)      STATUS_ONLY=1 ;;
        --force)       FORCE=1 ;;
        --daemon-only) DAEMON_ONLY=1 ;;
        --uninstall)   UNINSTALL=1 ;;
        --help|-h)     sed -n '2,17p' "$0"; exit 0 ;;
        *)
            echo "ERROR: unknown option: ${arg}" >&2
            echo "Run 'bash bin/install-routines.sh --help' for usage." >&2
            exit 2
            ;;
    esac
done

if [[ ${STATUS_ONLY} -eq 1 && ${UNINSTALL} -eq 1 ]]; then
    echo "ERROR: --status and --uninstall are mutually exclusive" >&2
    exit 2
fi

AGENTS=("${REQUIRED_AGENTS[@]}")
if [[ ${DAEMON_ONLY} -eq 0 ]]; then
    AGENTS+=("${OPTIONAL_AGENTS[@]}")
fi

# Test seam: install an explicit label list instead of the built-in sets, so the
# render/install/bootstrap/verify path can be exercised end to end against a
# throwaway label rather than against the live com.vibesquad.daemon.
if [[ -n "${SQUAD_INSTALL_AGENTS:-}" ]]; then
    read -r -a AGENTS <<<"${SQUAD_INSTALL_AGENTS}"
    REQUIRED_AGENTS=("${AGENTS[@]}")
    OPTIONAL_AGENTS=()
fi

# Validate the entire selection BEFORE any install/uninstall side effect. The
# test seam may select disposable labels, but may never grant Chrome lifecycle
# authority or smuggle a different plist path through a label.
for label in "${AGENTS[@]}"; do
    if [[ ! "${label}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
        echo "ERROR: invalid agent label: ${label}" >&2
        exit 2
    fi
    for operator_label in "${OPERATOR_AGENTS[@]}"; do
        if [[ "${label}" == "${operator_label}" ]]; then
            echo "ERROR: ${label} is operator-managed; use --status without the agent override for reporting only" >&2
            exit 2
        fi
    done
done

LAUNCHD_DOMAIN="gui/$(id -u)"
FAILURES=0
DEGRADED=0

# --- rendering -------------------------------------------------------------

# Render one template to a destination path, then prove the result is installable.
#
# Both __VAULT_ROOT__ and __HOME__ must be substituted even when a template only
# *documents* one of them: the daemon template's own instruction comment contains
# the literal string __HOME__, and bin/launch-squad.sh:136 rejects any installed
# plist matching __[A-Z][A-Z0-9_]*__ anywhere in the file, comments included.
# Substituting only __VAULT_ROOT__ therefore produces a plist the launcher refuses.
render_plist() {
    local src="$1" dest="$2"

    sed -e "s|__VAULT_ROOT__|${VAULT_ROOT}|g" \
        -e "s|__HOME__|${HOME}|g" \
        "${src}" > "${dest}"

    local residual
    if residual=$(LC_ALL=C grep -oE '__[A-Z][A-Z0-9_]*__' "${dest}" | sort -u | tr '\n' ' ') \
       && [[ -n "${residual}" ]]; then
        echo "  ✗ unresolved template token(s) after rendering: ${residual}" >&2
        echo "    bin/launch-squad.sh would refuse to bootstrap this plist." >&2
        return 1
    fi

    if ! plutil -lint "${dest}" >/dev/null 2>&1; then
        echo "  ✗ rendered plist failed plutil validation" >&2
        plutil -lint "${dest}" >&2 || true
        return 1
    fi

    return 0
}

# --- installation ----------------------------------------------------------

install_agent() {
    local label="$1"
    local src="${VAULT_ROOT}/launchd/${label}.plist"
    local target="${LAUNCHAGENTS_DIR}/${label}.plist"

    echo ""
    echo "${label}:"

    if [[ ! -f "${src}" ]]; then
        echo "  ✗ template not found: ${src}" >&2
        FAILURES=$((FAILURES + 1))
        return 1
    fi

    mkdir -p "${LAUNCHAGENTS_DIR}"

    # Render into the destination directory so the final move is a same-filesystem
    # rename, which is atomic: a reader never sees a half-written plist.
    local staged
    staged="$(mktemp "${LAUNCHAGENTS_DIR}/.${label}.plist.XXXXXX")"

    if ! render_plist "${src}" "${staged}"; then
        rm -f "${staged}"
        FAILURES=$((FAILURES + 1))
        return 1
    fi

    # launchd uses the plist's Label, not its filename. A mislabeled template
    # must not turn an allowed filename into authority over an operator job.
    local rendered_label
    rendered_label="$(plutil -extract Label raw "${staged}" 2>/dev/null)" || rendered_label=""
    if [[ "${rendered_label}" != "${label}" ]]; then
        echo "  ✗ rendered Label does not match ${label}; refusing installation" >&2
        rm -f "${staged}"
        FAILURES=$((FAILURES + 1))
        return 1
    fi

    if [[ -f "${target}" ]] && cmp -s "${staged}" "${target}"; then
        echo "  ✓ already installed and identical: ${target}"
        rm -f "${staged}"
    elif [[ -f "${target}" ]] && [[ ${FORCE} -eq 0 ]]; then
        # An installed plist that differs may carry operator edits. Refuse rather
        # than silently overwrite; --force is the explicit opt-in.
        echo "  ✗ installed plist differs from the rendered template: ${target}" >&2
        echo "    Re-run with --force to replace it, or diff it first:" >&2
        echo "      diff ${target} <(sed -e 's|__VAULT_ROOT__|${VAULT_ROOT}|g' -e 's|__HOME__|${HOME}|g' ${src})" >&2
        rm -f "${staged}"
        FAILURES=$((FAILURES + 1))
        return 1
    elif [[ ${DRY_RUN} -eq 1 ]]; then
        echo "  [dry-run] would install: ${target}"
        rm -f "${staged}"
    else
        chmod 644 "${staged}"
        mv -f "${staged}" "${target}"
        echo "  ✓ installed: ${target}"
    fi

    if [[ ${DRY_RUN} -eq 1 ]]; then
        echo "  [dry-run] would load: ${LAUNCHD_DOMAIN}/${label}"
        return 0
    fi

    # launchd opens the monitor's output files before the script can create its
    # state directory. Read the paths from the template we installed so its
    # logging configuration remains the single source of truth.
    if [[ "${label}" == "com.chrono.squad-monitor" ]]; then
        local log_key log_path
        for log_key in StandardOutPath StandardErrorPath; do
            if ! log_path="$(plutil -extract "${log_key}" raw "${target}" 2>/dev/null)" \
               || [[ -z "${log_path}" ]] || ! mkdir -p "$(dirname -- "${log_path}")"; then
                echo "  ✗ could not prepare monitor ${log_key}; refusing bootstrap" >&2
                FAILURES=$((FAILURES + 1))
                return 1
            fi
        done
    fi

    bootstrap_agent "${label}" "${target}"
}

# --- loading ---------------------------------------------------------------

# Resolve symlinks in a path's directory so two spellings of the same file compare
# equal. launchd reports the physical path (/private/tmp/...), while the path this
# script constructs from $HOME may go through a symlink (/tmp/...); comparing the
# raw strings would report a correctly-installed agent as foreign.
resolve_path() {
    local p="$1" dir base
    dir="$(dirname -- "${p}")"
    base="$(basename -- "${p}")"
    ( cd -- "${dir}" 2>/dev/null && printf '%s/%s\n' "$(pwd -P)" "${base}" ) || printf '%s\n' "${p}"
}

# Echo the plist path launchd actually loaded a label from. Return 113 only for
# an absent service; any other error (including a missing path) is unknown.
#
# The bare exit code of `launchctl print` is NOT a usable liveness signal here.
# launchd is addressed by label over IPC, not through the filesystem, so the query
# answers for whichever session owns this uid regardless of which HOME or
# LaunchAgents directory this script was pointed at. Asking only "is the label
# known?" therefore reports another installation's state as if it were ours. Bind
# the answer to identity instead: compare the path launchd reports against the
# plist we intend to manage.
#
# Callers explicitly distinguish absence from an unavailable observation. An
# IPC/permission error must never authorize bootstrap or destructive removal.
loaded_plist_path() {
    local service="$1" out path rc=0
    out="$(launchctl print "${service}" 2>/dev/null)" || rc=$?
    [[ ${rc} -eq 0 ]] || return "${rc}"
    path="$(printf '%s\n' "${out}" | sed -n 's/^[[:space:]]*path = //p' | head -1)"
    [[ -n "${path}" ]] || return 2
    printf '%s\n' "${path}"
}

bootstrap_agent() {
    local label="$1" target="$2"
    local service="${LAUNCHD_DOMAIN}/${label}"

    if [[ "${NO_BOOTSTRAP}" == "1" ]]; then
        echo "  — not loaded: SQUAD_INSTALL_NO_BOOTSTRAP=1 (plist is installed and valid)"
        return 0
    fi

    if ! command -v launchctl >/dev/null 2>&1; then
        echo "  ⚠ launchctl unavailable — plist installed but NOT loaded" >&2
        DEGRADED=$((DEGRADED + 1))
        return 0
    fi

    local live query_rc=0
    live="$(loaded_plist_path "${service}")" || query_rc=$?
    if [[ ${query_rc} -ne 0 && ${query_rc} -ne 113 ]]; then
        echo "  ✗ loaded state unknown (query rc=${query_rc}); refusing bootstrap" >&2
        FAILURES=$((FAILURES + 1))
        return 1
    fi
    if [[ -n "${live}" ]]; then
        if [[ "$(resolve_path "${live}")" == "$(resolve_path "${target}")" ]]; then
            echo "  ✓ already loaded: ${service}"
            return 0
        fi
        # Same label, different file. Booting ours in would silently displace an
        # installation this script does not own, so stop and say so.
        echo "  ✗ ${label} is already loaded from a DIFFERENT plist" >&2
        echo "      loaded: ${live}" >&2
        echo "      wanted: ${target}" >&2
        echo "    Not bootstrapping over it. Unload the other one first:" >&2
        echo "      launchctl bootout ${service}" >&2
        FAILURES=$((FAILURES + 1))
        return 1
    fi

    local rc=0
    launchctl bootstrap "${LAUNCHD_DOMAIN}" "${target}" || rc=$?

    # Verify by observation rather than by the bootstrap exit code: bootstrap
    # returns non-zero for "already loaded" races, and a zero exit is not by
    # itself proof the job is registered from the plist we just installed.
    live="$(loaded_plist_path "${service}")" || live=""
    if [[ -n "${live}" ]] && [[ "$(resolve_path "${live}")" == "$(resolve_path "${target}")" ]]; then
        if [[ ${rc} -ne 0 ]]; then
            echo "  ✓ loaded: ${service} (bootstrap returned ${rc}, job is registered)"
        else
            echo "  ✓ loaded: ${service}"
        fi
        return 0
    fi

    echo "  ✗ bootstrap failed (rc=${rc}) and ${service} is not registered" >&2
    echo "    Inspect with: launchctl print ${service}" >&2
    FAILURES=$((FAILURES + 1))
    return 1
}

# --- removal ---------------------------------------------------------------

uninstall_agent() {
    local label="$1" live query_rc=0 bootout_rc=0
    local target="${LAUNCHAGENTS_DIR}/${label}.plist"
    local service="${LAUNCHD_DOMAIN}/${label}"
    printf '\n%s:\n' "${label}"

    if ! command -v launchctl >/dev/null 2>&1; then
        echo "  ✗ launchctl unavailable; loaded state unknown, retaining plist" >&2
        FAILURES=$((FAILURES + 1))
        return 1
    fi
    live="$(loaded_plist_path "${service}")" || query_rc=$?
    if [[ ${query_rc} -ne 0 && ${query_rc} -ne 113 ]]; then
        echo "  ✗ loaded state unknown (query rc=${query_rc}); retaining plist" >&2
        FAILURES=$((FAILURES + 1))
        return 1
    fi
    if [[ -n "${live}" && "$(resolve_path "${live}")" != "$(resolve_path "${target}")" ]]; then
        echo "  ✗ loaded from a DIFFERENT plist: ${live}; refusing removal" >&2
        FAILURES=$((FAILURES + 1))
        return 1
    fi
    if [[ ${DRY_RUN} -eq 1 ]]; then
        [[ -z "${live}" ]] || echo "  [dry-run] would unload: ${service}"
        echo "  [dry-run] would remove if present: ${target}"
        return 0
    fi
    if [[ -n "${live}" ]]; then
        launchctl bootout "${service}" || bootout_rc=$?
        query_rc=0
        live="$(loaded_plist_path "${service}")" || query_rc=$?
        # A successful command alone is not proof that the job stopped. Keep
        # the plist unless launchd positively reports this service absent.
        if [[ ${query_rc} -ne 113 ]]; then
            echo "  ✗ unload not verified (bootout rc=${bootout_rc}, query rc=${query_rc}); retaining plist" >&2
            FAILURES=$((FAILURES + 1))
            return 1
        fi
        echo "  ✓ unloaded: ${service} (bootout rc=${bootout_rc})"
    fi
    if ! rm -f "${target}"; then
        echo "  ✗ could not remove: ${target}" >&2
        FAILURES=$((FAILURES + 1))
        return 1
    fi
    echo "  ✓ removed (or already absent): ${target}"
}

# --- status ----------------------------------------------------------------

report_status() {
    local label owner="$1" required="$2"
    shift 2
    for label in "$@"; do
        local target="${LAUNCHAGENTS_DIR}/${label}.plist"
        local installed="missing" loaded="not loaded" live=""
        [[ -f "${target}" ]] && installed="installed"
        if ! command -v launchctl >/dev/null 2>&1; then
            loaded="unknown"
        else
            local query_rc=0
            live="$(loaded_plist_path "${LAUNCHD_DOMAIN}/${label}")" || query_rc=$?
            if [[ ${query_rc} -eq 113 ]]; then
                loaded="not loaded"
            elif [[ ${query_rc} -ne 0 ]]; then
                loaded="unknown"
            elif [[ "$(resolve_path "${live}")" == "$(resolve_path "${target}")" ]]; then
                loaded="loaded"
            else
                # Registered under this label, but from a plist we do not manage.
                # Reporting that as "loaded" would credit us with another
                # installation's state.
                loaded="foreign"
                FOREIGN_NOTES+=("${label} is loaded from ${live}, not ${target}")
            fi
        fi
        printf '  %-38s %-10s %-10s %-16s %s\n' "${label}" "${installed}" "${loaded}" "${owner}" "${required}"
    done
}

if [[ ${STATUS_ONLY} -eq 1 ]]; then
    FOREIGN_NOTES=()
    echo "LaunchAgents dir: ${LAUNCHAGENTS_DIR}"
    echo "launchd domain:   ${LAUNCHD_DOMAIN}"
    echo ""
    printf '  %-38s %-10s %-10s %-16s %s\n' "LABEL" "PLIST" "LAUNCHD" "OWNER" "ROLE"
    report_status "repo-managed" "optional daemon" "${REQUIRED_AGENTS[@]}"
    # Guard the expansion: macOS ships bash 3.2, where "${empty[@]}" under
    # `set -u` is an unbound-variable error rather than zero arguments.
    if [[ ${#OPTIONAL_AGENTS[@]} -gt 0 ]]; then
        report_status "repo-managed" "optional" "${OPTIONAL_AGENTS[@]}"
    fi
    report_status "operator-managed" "reporting only; lifecycle reserved to operator" "${OPERATOR_AGENTS[@]}"
    if [[ ${#FOREIGN_NOTES[@]} -gt 0 ]]; then
        echo ""
        echo "  'foreign' means launchd knows the label, but from a plist outside"
        echo "  ${LAUNCHAGENTS_DIR}. That is another installation's state, not this one's:"
        printf '    - %s\n' "${FOREIGN_NOTES[@]}"
    fi
    exit 0
fi

# --- main ------------------------------------------------------------------

if [[ ${UNINSTALL} -eq 1 ]]; then
    echo "Removing Vibe Squad managed launchd routines"
    for label in "${AGENTS[@]}"; do
        uninstall_agent "${label}" || true
    done
    if [[ ${FAILURES} -gt 0 ]]; then
        echo "✗ ${FAILURES} agent(s) could not be removed" >&2
        exit 1
    fi
    echo "✓ Managed routine removal complete (dry-run=${DRY_RUN})"
    exit 0
fi

echo "Installing Vibe Squad launchd routines"
echo "  vault root:       ${VAULT_ROOT}"
echo "  LaunchAgents dir: ${LAUNCHAGENTS_DIR}"
echo "  launchd domain:   ${LAUNCHD_DOMAIN}"
[[ ${DRY_RUN} -eq 1 ]] && echo "  mode:             dry run (nothing will be changed)"

if [[ ${DRY_RUN} -eq 0 ]]; then
    # These files are tracked as non-executable, so this leaves the working tree
    # dirty with mode-only changes (measured: 34 files, zero content changes).
    # Say so rather than letting a later `git status` look like unexplained drift.
    echo "  note:             making bin/ and scripts/ executable (mode-only git changes)"
    chmod +x "${VAULT_ROOT}/bin/"*.sh 2>/dev/null || true
    chmod +x "${VAULT_ROOT}/scripts/python/"*.py 2>/dev/null || true
    chmod +x "${VAULT_ROOT}/scripts/"*.sh 2>/dev/null || true
fi

# Pre-warm the uv ephemeral env cache for vibecoding-check.
# Without this, the first run from a sandboxed CLI (Codex's workspace-write,
# launchd's restricted env) hangs on DNS while uv tries to fetch deps.
# Optional: an absent or offline uv degrades to a slower first run, nothing else.
if [[ ${DRY_RUN} -eq 0 ]]; then
    echo ""
    if ! command -v uv >/dev/null 2>&1; then
        echo "⚠ uv not found — skipping cache pre-warm; the first nightly run will fetch its deps."
        DEGRADED=$((DEGRADED + 1))
    else
        echo "Pre-warming uv cache..."
        if UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}" uv run --quiet --no-project \
            --with pyyaml --with httpx \
            python -c "import yaml, httpx" 2>/dev/null; then
            echo "  ✓ pyyaml, httpx cached"
        else
            echo "  ⚠ uv cache pre-warm failed (offline?) — first script run will fetch"
            DEGRADED=$((DEGRADED + 1))
        fi
    fi
fi

for label in "${AGENTS[@]}"; do
    install_agent "${label}" || true
done

echo ""
if [[ ${FAILURES} -gt 0 ]]; then
    echo "✗ ${FAILURES} agent(s) failed to install. See the errors above."
    echo "  Inspect installed/loaded state with: bash bin/install-routines.sh --status"
    exit 1
fi

if [[ ${DRY_RUN} -eq 1 ]]; then
    echo "✓ Dry run complete: every template rendered, resolved, and passed plutil."
    exit 0
fi

echo "✓ Vibe Squad launchd routines installed"
[[ ${DEGRADED} -gt 0 ]] && echo "  (${DEGRADED} optional step(s) degraded — see warnings above)"
echo ""
echo "Verify:  bash bin/install-routines.sh --status"
echo "Launch:  bin/squad doctor && bin/squad up"
exit 0
