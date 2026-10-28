#!/bin/bash
# Compatibility wrapper for Chrono's simple dispatch command.
#
# Usage:
#   REVIEW_TRIGGERS='[blast_radius]' bash scripts/send-task.sh \
#       <source-namespace> <body-file> <specialist> [to-model] \
#       --mode <project|bounty|modeless> [--dry-run]
#
#   --mode is required. The wrapper writes that exact operator-approved value
#     into packet frontmatter and never supplies a default.
#
#   WRITE_SCOPE="path/a, path/b"  — extra writable paths, appended to the response
#     artifact. Without this the wrapper could only ever author read-only packets,
#     so any packet asking for code changes silently could not apply them.
#   WRITE_SCOPE_BODY_ONLY_OK=1 — allow a body-only scope declaration without
#     WRITE_SCOPE when the declaration is intentionally just prose.
#   WORK_REPO=/absolute/main/checkout — optional external work repository.
#     Unset emits no field and retains squad-repository dispatch behavior.
#
#   ROUTE_TIER=primary|backup|escalate|review|throughput — pick this specialist's
#     effort tier for one dispatch. Unset emits no field, so lane selection keeps
#     its historical first-matching-tier behavior. An explicitly empty value is
#     invalid. The tier must name a row whose
#     *_lane is the lane the packet actually dispatches to.
#
#   REVIEW_MODEL=codex|gpt-codex|claude|gemini|grok — alternate reviewer for
#     non-empty REVIEW_TRIGGERS. Defaults to the mapped reviewer; if that reviewer
#     shares the author's family or is absent, explicitly choose an independent
#     reviewer. The wrapper refuses before staging when no valid choice is given.
#
#   MODEL_OVERRIDE_REASON="Primary lane is unavailable." — required off-primary.
#     The default 'none' remains explicit for primary-lane packets; both override
#     guards reject that sentinel when the author leaves the primary lane.
#
#   REVIEWS=none | REVIEWS=TASK-...  — required review-provenance declaration.
#     `none` deliberately marks ordinary work; a canonical task id marks a review
#     and is stamped into the response envelope for settlement.
#
#   AUTHORIZED_DELETE_PATHS='"a", "b"'  — JSON-list members of paths the worker may
#     DELETE. Required for any file MOVE; the board refuses worker deletions without it.
#
#   --supersedes TASK-ID  (env: SUPERSEDES=TASK-ID) — close the named task as
#     `superseded` via registry_reconciler.py, but ONLY after this dispatch is
#     admitted. A failed admission (set -e) or a --dry-run closes nothing. The
#     close reason is "superseded by <new id>". Settlement becomes a side effect
#     of the follow-up dispatch instead of a separate ceremony that gets skipped.
#
# This wrapper generates standard TASK frontmatter, then routes the packet
# through bin/send-task.sh so normal Chrono dispatches get the same safety path
# as prepared task files: write-scope checks, toolkit injection,
# active registry updates, dispatch logging, and board launch.

set -euo pipefail

# Default to the checkout this script lives in; a fixed home-relative name only worked on one machine.
VAULT_ROOT="${VAULT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
HARDENED_DISPATCH="${VAULT_ROOT}/bin/send-task.sh"
AUTHORING_PREFLIGHT="${VAULT_ROOT}/scripts/python/dispatch_preflight.py"
RUNTIME_MAP="${VAULT_ROOT}/shared/specialist-runtime-map.tsv"
source "${VAULT_ROOT}/shared/lead-windows.sh"

if [[ $# -lt 3 ]]; then
    echo "usage: $0 <source-namespace> <body-file> <specialist> [to-model] --mode <project|bounty|modeless> [--dry-run] [--supersedes TASK-ID]"
    echo "  source-namespace: ${COMPATIBILITY_NAMESPACES[*]}"
    echo "  body-file: path to markdown file containing task body"
    echo "  specialist: canonical specialist name, or none only when direct_lane_work_allowed is intentionally true"
    echo "  --mode: required operator-approved packet mode; no default is supplied"
    exit 1
fi

COMPAT_NAMESPACE="$1"
SOURCE_NAMESPACE="$1"
BODY_FILE="$2"
SPECIALIST="$3"
shift 3

TO_MODEL=""
MODE=""
DRY_RUN="false"
# Rec 5: settle a superseded task as a side effect of this dispatch. The env
# form exists for the wrapper callers that cannot add a CLI flag; an explicit
# --supersedes flag overrides it (parsed below).
SUPERSEDES_ID="${SUPERSEDES:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            [[ $# -ge 2 ]] || {
                echo "ERROR: --mode requires project, bounty, or modeless"
                exit 1
            }
            [[ -z "${MODE}" ]] || {
                echo "ERROR: --mode may be specified only once"
                exit 1
            }
            MODE="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN="true"
            shift
            ;;
        --supersedes)
            [[ $# -ge 2 ]] || {
                echo "ERROR: --supersedes requires a TASK-... id"
                exit 1
            }
            SUPERSEDES_ID="$2"
            shift 2
            ;;
        --*)
            echo "ERROR: unknown option: $1"
            exit 1
            ;;
        *)
            [[ -z "${TO_MODEL}" ]] || {
                echo "ERROR: unexpected argument: $1"
                exit 1
            }
            TO_MODEL="$1"
            shift
            ;;
    esac
done

if [[ -z "${MODE}" ]]; then
    echo "ERROR: missing required --mode <project|bounty|modeless>; the wrapper will not invent packet field 'mode'"
    exit 1
fi
case "${MODE}" in
    project|bounty|modeless) ;;
    *)
        echo "ERROR: invalid --mode '${MODE}'; expected project, bounty, or modeless"
        exit 1
        ;;
esac

# An optional per-dispatch effort tier. Validate against ROUTE_TIERS in the
# context builder rather than restating the vocabulary here: a second copy would
# age independently and admit a tier the dispatcher then rejects after staging.
# Membership in that tuple also implies single-line, round-trippable UTF-8, so
# this subsumes the interpolation boundary check applied to the raw values below.
# Resolve the builder from THIS script's checkout, not VAULT_ROOT: VAULT_ROOT is
# the dispatch target and may be a fixture root with no scripts/python at all.
# Failing to import is a refusal, never a silent skip of the check.
ROUTE_TIER_LINE=""
if [[ -n "${ROUTE_TIER+x}" ]]; then
    if ! ROUTE_TIER="${ROUTE_TIER}" python3 -X utf8 - \
        "$(cd "$(dirname "${BASH_SOURCE[0]}")/python" && pwd -P)" <<'PYEOF'
import os
import sys

sys.path.insert(0, sys.argv[1])
from dispatch_context_builder import ROUTE_TIERS

tier = os.environ["ROUTE_TIER"]
if tier not in ROUTE_TIERS:
    print(
        f"ERROR: invalid ROUTE_TIER {tier!r}; route_tier must be one of {', '.join(ROUTE_TIERS)}",
        file=sys.stderr,
    )
    raise SystemExit(1)
PYEOF
    then
        exit 1
    fi
    ROUTE_TIER_LINE="route_tier: ${ROUTE_TIER}"$'\n'
fi

# Review intent is an explicit admission-time union. An omitted variable used to
# be indistinguishable from deliberate ordinary work, and a missing review target
# cannot be reconstructed after launch. Validate before TASK_ID/UUID creation or
# packet staging so omission cannot leave any dispatch residue.
if [[ -z "${REVIEWS+x}" ]]; then
    echo "ERROR: missing required REVIEWS declaration; set REVIEWS=none for ordinary work or REVIEWS=TASK-YYYY-MM-DD-HHMM-<suffix> for a review"
    exit 1
fi
if [[ -z "${REVIEWS}" ]]; then
    echo "ERROR: empty REVIEWS declaration; set REVIEWS=none for ordinary work or REVIEWS=TASK-YYYY-MM-DD-HHMM-<suffix> for a review"
    exit 1
fi
if [[ "${REVIEWS}" != "none" && ! "${REVIEWS}" =~ ^TASK-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}-[A-Za-z0-9][A-Za-z0-9-]*$ ]]; then
    echo "ERROR: invalid REVIEWS declaration '${REVIEWS}'; expected exactly none or a canonical TASK-YYYY-MM-DD-HHMM-<suffix> id"
    exit 1
fi

# A --supersedes/SUPERSEDES id is interpolated into the reconciler close reason
# and command after admission; reject a malformed id before any dispatch so a
# typo cannot slip through and the close names a real canonical task.
if [[ -n "${SUPERSEDES_ID}" && ! "${SUPERSEDES_ID}" =~ ^TASK-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}-[A-Za-z0-9][A-Za-z0-9-]*$ ]]; then
    echo "ERROR: invalid --supersedes/SUPERSEDES id '${SUPERSEDES_ID}'; expected a canonical TASK-YYYY-MM-DD-HHMM-<suffix> id"
    exit 1
fi

# An explicit 4th-arg lane must survive the runtime-map lookup below; without
# this the documented [to-model] override was silently discarded (failover
# to a backup lane was impossible from this wrapper).
EXPLICIT_MODEL="${TO_MODEL}"

if [[ ! -f "${BODY_FILE}" ]]; then
    echo "ERROR: body file not found: ${BODY_FILE}"
    exit 1
fi

# A body declaration cannot grant writable paths; require the frontmatter input
# before staging unless the operator explicitly marks the declaration as prose.
if [[ -z "${WRITE_SCOPE:-}" && "${WRITE_SCOPE_BODY_ONLY_OK:-}" != "1" ]] &&
    grep -Eiq '^[[:space:]]*(-[[:space:]]*)?(\*\*[[:space:]]*)?write scope:' "${BODY_FILE}"; then
    echo 'ERROR: body declares a write scope but WRITE_SCOPE is not set; set WRITE_SCOPE="p1, p2"' >&2
    exit 1
fi

if ! is_compatibility_namespace "${COMPAT_NAMESPACE}"; then
    echo "ERROR: invalid compatibility namespace: ${COMPAT_NAMESPACE}"
    exit 1
fi

# Raw operator values interpolated below: SPECIALIST, TO_MODEL,
# MODEL_OVERRIDE_REASON, WRITE_SCOPE and AUTHORIZED_DELETE_PATHS (through
# DELETE_PATHS_LINE). REVIEW_TRIGGERS needs the same boundary check. Run before
# map lookup as well as staging so positional inputs cannot reach awk unchecked.
if ! python3 -X utf8 - \
    SPECIALIST "${SPECIALIST}" TO_MODEL "${TO_MODEL}" \
    MODEL_OVERRIDE_REASON "${MODEL_OVERRIDE_REASON-}" \
    WRITE_SCOPE "${WRITE_SCOPE-}" \
    WORK_REPO "${WORK_REPO-}" \
    AUTHORIZED_DELETE_PATHS "${AUTHORIZED_DELETE_PATHS-}" \
    REVIEW_TRIGGERS "${REVIEW_TRIGGERS-}" <<'PYEOF'
import sys

for name, value in zip(sys.argv[1::2], sys.argv[2::2]):
    # Match dispatch_context_builder._parse_task_text's splitlines semantics.
    # Compare content, not just line count: a trailing separator still yields
    # one line. Empty optional values are valid; never rewrite the input.
    if value and value.splitlines() != [value]:
        print(f"ERROR: {name} must be single-line (no parser line breaks)", file=sys.stderr)
        raise SystemExit(1)
    # Python preserves malformed argv/environment bytes with surrogateescape.
    # Refuse values that strict UTF-8 cannot preserve; do not normalize text.
    try:
        round_trip = value.encode("utf-8").decode("utf-8")
    except UnicodeError:
        round_trip = None
    if round_trip != value:
        print(f"ERROR: {name} must round-trip through UTF-8 without loss", file=sys.stderr)
        raise SystemExit(1)
PYEOF
then
    exit 1
fi

map_field() {
    local specialist="$1" field_index="$2"
    awk -F '\t' -v s="$specialist" -v idx="$field_index" '$1 == s {print $idx; exit}' "${RUNTIME_MAP}"
}

if [[ -z "${TO_MODEL}" ]]; then
    # A source namespace locates specialist markdown, never a model. The only valid
    # omitted-model fallback is the specialist's primary lane in the runtime map.
    if [[ "${SPECIALIST}" == "none" ]]; then
        echo "ERROR: omitted to-model requires a canonical specialist"
        exit 1
    fi
    if [[ ! -r "${RUNTIME_MAP}" ]]; then
        echo "ERROR: specialist runtime map is unavailable: ${RUNTIME_MAP}"
        exit 1
    fi
    TO_MODEL="$(map_field "${SPECIALIST}" 7)"
    if [[ -z "${TO_MODEL}" ]]; then
        echo "ERROR: specialist is absent from runtime map: ${SPECIALIST}"
        exit 1
    fi
fi
[[ "${TO_MODEL}" == "codex" ]] && TO_MODEL="gpt-codex"

REVIEW_MODEL="${REVIEW_MODEL-}"
MAPPED_REVIEW_MODEL="none"
REVIEW_TRIGGERS="${REVIEW_TRIGGERS:-[]}"
MANDATORY_REVIEW="false"
if [[ "${SPECIALIST}" != "none" && -f "${RUNTIME_MAP}" ]]; then
    # Canonical map fields used here: source_namespace=2 primary_lane=7 review_lane=14.
    # safety_level is a specialist quality floor, not a property of this change.
    mapped_model="$(map_field "${SPECIALIST}" 7)"
    mapped_review="$(map_field "${SPECIALIST}" 14)"
    mapped_namespace="$(map_field "${SPECIALIST}" 2)"
    [[ "${mapped_model}" == "codex" ]] && mapped_model="gpt-codex"
    [[ "${mapped_review}" == "codex" ]] && mapped_review="gpt-codex"
    if [[ -n "${mapped_model}" ]]; then
        [[ -z "${EXPLICIT_MODEL}" ]] && TO_MODEL="${mapped_model}"
        MAPPED_REVIEW_MODEL="${mapped_review:-none}"
        SOURCE_NAMESPACE="${mapped_namespace:-${SOURCE_NAMESPACE}}"
    fi
fi

# The lookup can replace both values. Check the resolved identifiers against
# the shared inventories; shared is a role source, not a compatibility mailbox.
if [[ "${SOURCE_NAMESPACE}" != "shared" ]] && ! is_compatibility_namespace "${SOURCE_NAMESPACE}"; then
    echo "ERROR: invalid SOURCE_NAMESPACE '${SOURCE_NAMESPACE}'"
    exit 1
fi
# The lane allowlist needs MODEL_LANES from the sourced helper. If that helper
# did not load, an unset array under `set -u` crashes with an opaque unbound-
# variable error rather than saying what is wrong -- which is how this surfaced
# as a dispatch failure in a fixture that simply lacked the helper.
if [[ -z "${MODEL_LANES+x}" || ${#MODEL_LANES[@]} -eq 0 ]]; then
    echo "ERROR: lane allowlist unavailable: MODEL_LANES is not defined." >&2
    echo "       shared/lead-windows.sh must be sourced before lane validation." >&2
    exit 1
fi
valid_model=false
for model_lane in "${MODEL_LANES[@]}"; do
    if [[ "${TO_MODEL}" == "${model_lane}" ]]; then
        valid_model=true
        break
    fi
done
if [[ "${valid_model}" != "true" ]]; then
    echo "ERROR: invalid TO_MODEL '${TO_MODEL}'; expected ${MODEL_LANES[*]}"
    exit 1
fi

# Review is a property of this packet. The hardened dispatcher validates the
# four-token enum and rejects a flag/list mismatch; this wrapper only derives
# the boolean from whether the explicit list is empty. An explicit reviewer wins
# over the primary-oriented map default; never invent an alternate reviewer.
review_triggers_compact="${REVIEW_TRIGGERS//[[:space:]]/}"
if [[ "$review_triggers_compact" != "[]" ]]; then
    MANDATORY_REVIEW="true"
    REVIEW_MODEL="${REVIEW_MODEL:-${MAPPED_REVIEW_MODEL}}"
    [[ "${REVIEW_MODEL}" == "codex" ]] && REVIEW_MODEL="gpt-codex"
    case "${REVIEW_MODEL}" in
        gpt-codex|claude|gemini|grok|none) ;;
        *)
            echo "ERROR: invalid REVIEW_MODEL '${REVIEW_MODEL}'; expected codex, gpt-codex, claude, gemini, grok, or none"
            exit 1
            ;;
    esac
    # Each supported lane names one provider family. Normalize the Codex alias
    # above before applying the same separation required by the hardened guard.
    if [[ "${REVIEW_MODEL}" == "none" || "${REVIEW_MODEL}" == "${TO_MODEL}" ]]; then
        echo "ERROR: no valid cross-family reviewer for author '${TO_MODEL}' (reviewer '${REVIEW_MODEL}'); set REVIEW_MODEL to an independent model lane"
        exit 1
    fi
else
    REVIEW_MODEL="none"
fi

if [[ ! -x "${HARDENED_DISPATCH}" ]]; then
    echo "ERROR: hardened dispatcher not executable: ${HARDENED_DISPATCH}"
    exit 1
fi

# The hardened dispatcher honours authorized_delete_paths, but this wrapper had no
# way to express it, so any packet needing a file MOVE (retire a skill, relocate a
# doc) was undispatchable from here and had to be hand-authored. Always emitted:
# the dispatcher json.loads it and treats [] as falsy, so an unset value is inert.
DELETE_PATHS_LINE="authorized_delete_paths: [${AUTHORIZED_DELETE_PATHS:-}]"
# Every generated packet carries the typed declaration. The hardened dispatcher
# independently validates prepared packets, then projects only a real task target.
REVIEWS_LINE="reviews: ${REVIEWS}"$'\n'
WORK_REPO_LINE=""
if [[ -n "${WORK_REPO:+x}" ]]; then
    WORK_REPO_LINE="work_repo: ${WORK_REPO}"$'\n'
fi

TIMESTAMP="$(date +%Y-%m-%d-%H%M)"
TASK_ID="TASK-${TIMESTAMP}-$(uuidgen | head -c 8 | tr '[:upper:]' '[:lower:]')"
STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/squad-task.XXXXXX")"
TASK_FILE="${STAGING_DIR}/${TASK_ID}.md"
trap 'rm -rf "${STAGING_DIR}"' EXIT

{
    cat <<EOF
---
id: ${TASK_ID}
run_id: none
from: chrono
mode: ${MODE}
phase: none
type: TASK
priority: normal
status: new
created: $(date -u +%FT%TZ)
deadline: none
write_scope: [departments/coding/outbox/${TASK_ID}-response.md${WRITE_SCOPE:+, ${WRITE_SCOPE}}]
${DELETE_PATHS_LINE}
${REVIEWS_LINE}${WORK_REPO_LINE}read_context: []
return_artifact: departments/coding/outbox/${TASK_ID}-response.md
compatibility_namespace: coding
specialist: ${SPECIALIST}
to_model: ${TO_MODEL}
${ROUTE_TIER_LINE}model_override_reason: ${MODEL_OVERRIDE_REASON:-none}
source_namespace: ${SOURCE_NAMESPACE}
review_model: ${REVIEW_MODEL}
mandatory_review: ${MANDATORY_REVIEW}
review_triggers: ${REVIEW_TRIGGERS}
success_criteria: []
out_of_scope: []
parallel_safe: false
direct_lane_work_allowed: false
operator_approved: true
parent_msg_id: none
---

EOF
    cat "${BODY_FILE}"
} > "${TASK_FILE}"

sync "${TASK_FILE}" 2>/dev/null || true

# The hardened live path runs the packet-bound preflight immediately before host
# admission. Its dry-run exits earlier through the context-builder validator, so
# emit the same authoring advisories here for generated dry-runs. This diagnostic
# path is intentionally fail-open: neither a warning nor a broken warning check
# may change what the hardened dispatcher admits.
if [[ "${DRY_RUN}" == "true" && -f "${AUTHORING_PREFLIGHT}" ]]; then
    python3 "${AUTHORING_PREFLIGHT}" \
        --repo-root "${VAULT_ROOT}" \
        --packet "${TASK_FILE}" \
        --authoring-warnings-only >/dev/null || true
fi

echo "  Packet mode: ${MODE}"
DISPATCH_ARGS=("${TASK_FILE}")
if [[ "${DRY_RUN}" == "true" ]]; then
    DISPATCH_ARGS+=(--dry-run)
fi
VAULT_ROOT="${VAULT_ROOT}" "${HARDENED_DISPATCH}" "${DISPATCH_ARGS[@]}"

# Close the superseded task as part of this dispatch so a missed manual close
# cannot leave the replaced task showing as outstanding work.
# Ordering is load-bearing: this runs only AFTER admission. `set -e` exits above
# if the hardened dispatch fails, so a failed admission closes nothing; and a
# dry-run admits nothing, so it must not close either. The close is best-effort:
# the dispatch is the irreversible action and already succeeded, so a close
# failure is loud on stderr but does not fail the dispatch — the operator closes
# it by hand from the printed command.
if [[ -n "${SUPERSEDES_ID}" && "${DRY_RUN}" != "true" ]]; then
    RECONCILER="${VAULT_ROOT}/scripts/python/registry_reconciler.py"
    if VAULT_ROOT="${VAULT_ROOT}" python3 "${RECONCILER}" \
        --close-task "${SUPERSEDES_ID}" \
        --close-status superseded \
        --close-reason "superseded by ${TASK_ID}"; then
        echo "  Superseded: ${SUPERSEDES_ID} (closed as superseded by ${TASK_ID})"
    else
        echo "WARNING: --supersedes: ${TASK_ID} was admitted, but closing ${SUPERSEDES_ID} as superseded FAILED." >&2
        echo "         Close it by hand: ${RECONCILER} --close-task ${SUPERSEDES_ID} --close-status superseded --close-reason 'superseded by ${TASK_ID}'" >&2
    fi
fi

echo "  File: ${VAULT_ROOT}/departments/coding/inbox/${TASK_ID}.md"
echo "  Reply expected at: ${VAULT_ROOT}/departments/coding/outbox/${TASK_ID}-response.md"
echo "  Model lane: ${TO_MODEL}"
