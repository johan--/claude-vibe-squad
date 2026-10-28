#!/bin/bash
# bin/canary.sh — live capability canary. Probes whether a capability WORKS,
# by performing or adjudicating a real action.
#
# WHY THIS EXISTS
#   Unit tests check CODE. bin/doctor.sh checks STATE. Neither checks
#   CAPABILITY. One build produced five capabilities that were green and dead
#   at the same time: board fan-out (1,779 unit tests passing, doctor 0 issues,
#   an anti-affinity APPROVE -- and every fan-out refused before host
#   admission); swarm (six test modules, 1,531 lines, no dispatch path at all);
#   the notification spine (doctor reported it, doctor could not measure it,
#   the reconciler exited 1); anti-affinity review (a 35-test suite that still
#   passed with three `must be cross-family` clauses replaced by `if False:`);
#   and advisory mode (a mode file, a protocol entry, a builder branch, and not
#   one successful dispatch in a month).
#
#   Every one of those is invisible to a test and to a state check, and visible
#   to a live action. That is the entire scope of this program.
#
# THREE OUTCOMES, NEVER TWO
#   PASS          the probe ran and the capability worked
#   FAIL          the probe ran and the capability is broken       (exit 1)
#   NOT MEASURED  the probe did not run, or its oracle is broken   (exit 2)
#
#   NOT MEASURED is never a pass. This mirrors doctor.sh's COULD NOT DETERMINE
#   and bin/test's BLOCKED, and it exists because a probe that returns "fine"
#   when the subsystem is absent is worse than no probe: it manufactures the
#   exact false confidence this program was written to destroy.
#
# EVERY PROBE HAS A POSITIVE CONTROL
#   Before any probe reports PASS or FAIL it first proves its own oracle can
#   see. The memory probe recalls its nonce BEFORE recording it and requires
#   zero hits. The skills probe requires its sentinel to still be present in
#   the skill file. The registry probes require a loadable, populated registry.
#   A control that cannot be established downgrades the probe to NOT MEASURED.
#
# EVERY PROBE HAS AN INVERTED CONTROL
#   `--self-test` breaks each capability against a fixture and asserts the
#   probe FAILS or reports NOT MEASURED. A canary that cannot fail is not a
#   gate; four green-but-broken cases above are what that costs.
#   scripts/python/tests/test_canary_suite.py pins the same inversions.
#
# WHAT A WORKER CANNOT DO
#   A board worker cannot launch a board dispatch, so probes 1-3 and 6 cannot be
#   EXECUTED from a lane. They are split instead: Chrono launches the two
#   role-specific canary packets, and this program ADJUDICATES the evidence
#   those tasks left in the registry, outbox and notify receipts. `--task` and
#   `--mcp-task` may adjudicate both results in one run.
#
# USAGE
#   bin/canary.sh                      probes that run here; 1-3, 6 NOT MEASURED
#   bin/canary.sh --task TASK-ID       adjudicate transport/skills evidence
#   bin/canary.sh --mcp-task TASK-ID   adjudicate Codex MCP evidence (may combine)
#   bin/canary.sh --emit-packet ID     print the transport/skills packet
#   bin/canary.sh --emit-mcp-packet ID print the Codex MCP-surface packet
#   bin/canary.sh --emit-mcp-expectation-example print placeholder local JSON
#   bin/canary.sh --self-test          inverted controls (no live writes)
#   bin/canary.sh --no-memory-write    skip probe 4's one vault note
#
# MCP EXPECTATION (operator-local; never an observed host answer in this file)
#   CANARY_MCP_EXPECTED_JSON takes precedence, even when empty or invalid.
#   Otherwise read CANARY_MCP_EXPECTED_FILE, defaulting to the gitignored
#   <root-under-test>/_state/canary-mcp-expected.json. An external path is allowed.
#   Supply a sorted, unique, non-empty JSON array of runtime namespace prefixes.
#   Generate yours: map the selected role/lane projection to runtime prefixes,
#   sort/deduplicate, and save that JSON locally before measuring the worker.
#   Never derive the expectation from the observed report being adjudicated.
#
# EXIT CODES
#   0  every probe PASS
#   1  at least one FAIL
#   2  no FAIL, but at least one NOT MEASURED   (the default invocation)
#   64 usage error (EX_USAGE, matching doctor.sh)

set -uo pipefail
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:${PATH}"

# shellcheck source-path=SCRIPTDIR source=../shared/repo-root.sh disable=SC1091
source "$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}" 2>/dev/null || printf '%s' "${BASH_SOURCE[0]}")")/.." && pwd -P)/shared/repo-root.sh"

# The tree whose live state is under test. Defaults to this checkout. Run from
# a linked worktree, `_state/` is gitignored and therefore absent, so the
# registry probes report NOT MEASURED rather than inventing a clean answer --
# the same trap bin/send-task.sh documents, where a worktree's one-entry stub
# registry made a conflict check report "no conflicts" because it could no
# longer see any.
CANARY_ROOT="${CANARY_ROOT_UNDER_TEST:-${VAULT_ROOT}}"

# Prefer the vault venv for plugin dependencies; evidence parsing and fixtures
# need only the standard library. An explicit interpreter override is honored.
if [[ -z "${CHRONO_PY+x}" ]]; then
    CHRONO_PY="${VAULT_ROOT}/.venv/bin/python"
    [[ -x "${CHRONO_PY}" ]] || CHRONO_PY="$(command -v python3 || true)"
fi

# The oracle for probe 3. It is deliberately a phrase the lane can only produce
# by READING the dispatched lane's probe-canary/SKILL.md, and it is deliberately
# absent from the packet this program emits -- a packet that quoted it would
# let a lane echo it back without ever firing the skill, which is precisely the
# projected-versus-fired distinction the probe exists to draw.
SKILL_SENTINEL='project-scoped skill loading works'
# These are intentionally different load-path canaries, not identity mirrors.
# model-lanes/SKILL-HOMES.md owns the lane -> skill-home decision.
AGENTS_SKILL_SENTINEL='You reached this file.'

# Expectations are loaded by the evidence parser from operator-local input.
# They are deliberately absent from emitted packets: the worker must enumerate
# its live surface independently. Fixture expectations live only in self-test.
MCP_SURFACE_MARKER='MCP_SURFACE_JSON:'

TASK_ID=""
MCP_TASK_ID=""
SELF_TEST=0
MEMORY_WRITE=1
EMIT_PACKET_ID=""
EMIT_MCP_PACKET_ID=""
EMIT_MCP_EXPECTATION_EXAMPLE=0

usage() {
    cat <<'USAGE_EOF'
usage: canary.sh [--task TASK-ID] [--mcp-task TASK-ID] [--emit-packet ID | --emit-mcp-packet ID | --emit-mcp-expectation-example] [--self-test] [--no-memory-write]

MCP expectation: CANARY_MCP_EXPECTED_JSON, otherwise CANARY_MCP_EXPECTED_FILE
(default: <root-under-test>/_state/canary-mcp-expected.json, gitignored).
Example: ["example_alpha","example_beta"] (placeholders, not a measured surface).
Generate yours: map your selected role/lane projection to runtime prefixes, sort/deduplicate, and save the JSON locally before running --mcp-task.
Use --emit-mcp-expectation-example to print an editable example; missing or
invalid configuration reports NOT MEASURED. Keep actual expectations private.
USAGE_EOF
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --task)
            TASK_ID="${2:-}"
            [[ -z "${TASK_ID}" ]] && { printf 'canary.sh: --task needs a TASK-ID\n' >&2; exit 64; }
            shift 2
            ;;
        --mcp-task)
            MCP_TASK_ID="${2:-}"
            [[ -z "${MCP_TASK_ID}" ]] && { printf 'canary.sh: --mcp-task needs a TASK-ID\n' >&2; exit 64; }
            shift 2
            ;;
        --emit-packet)
            EMIT_PACKET_ID="${2:-}"
            [[ -z "${EMIT_PACKET_ID}" ]] && { printf 'canary.sh: --emit-packet needs an id\n' >&2; exit 64; }
            shift 2
            ;;
        --emit-mcp-packet)
            EMIT_MCP_PACKET_ID="${2:-}"
            [[ -z "${EMIT_MCP_PACKET_ID}" ]] && { printf 'canary.sh: --emit-mcp-packet needs an id\n' >&2; exit 64; }
            shift 2
            ;;
        --self-test)     SELF_TEST=1; shift ;;
        --emit-mcp-expectation-example) EMIT_MCP_EXPECTATION_EXAMPLE=1; shift ;;
        --no-memory-write) MEMORY_WRITE=0; shift ;;
        --help|-h)       usage; exit 0 ;;
        *)
            # Refused, never ignored: silently accepting `--tsk` would run the
            # default path while the caller believed a task was adjudicated.
            printf 'canary.sh: unknown argument: %s\n' "$1" >&2
            usage >&2
            exit 64
            ;;
    esac
done

if [[ -n "${EMIT_PACKET_ID}" && -n "${EMIT_MCP_PACKET_ID}" ]] ||
   { (( EMIT_MCP_EXPECTATION_EXAMPLE )) && [[ -n "${EMIT_PACKET_ID}${EMIT_MCP_PACKET_ID}" ]]; }; then
    printf 'canary.sh: choose only one packet emitter\n' >&2
    usage >&2
    exit 64
fi

# --- Result vocabulary ------------------------------------------------------
PASSES=(); FAILURES=(); UNMEASURED=()
note_pass() { PASSES+=("$1"); printf '[PASS]         %-12s %s\n' "$1" "$2"; }
note_fail() { FAILURES+=("$1"); printf '[FAIL]         %-12s %s\n' "$1" "$2"; }
note_unmeasured() { UNMEASURED+=("$1"); printf '[NOT MEASURED] %-12s %s\n' "$1" "$2"; }

route() {  # route <probe> <STATUS> <detail>
    case "$2" in
        PASS) note_pass "$1" "$3" ;;
        FAIL) note_fail "$1" "$3" ;;
        *)    note_unmeasured "$1" "$3" ;;
    esac
}

# --- The packet Chrono dispatches -------------------------------------------
# Printed, never written: this program's write scope does not include an inbox,
# and Chrono owns dispatch. Redirect it into departments/coding/inbox/<id>.md.
packet_model() {
    "${CHRONO_PY}" -B - "${VAULT_ROOT}/shared/specialist-runtime-map.tsv" "$1" <<'PY'
import csv
import sys

with open(sys.argv[1], encoding="utf-8", newline="") as handle:
    rows = [row for row in csv.DictReader(handle, delimiter="\t")
            if row["specialist"] == sys.argv[2]]
if len(rows) != 1 or rows[0]["primary_lane"] not in {
    "codex", "claude", "gemini", "grok"
}:
    raise SystemExit("canary.sh: specialist has no unique supported primary lane")
lane = rows[0]["primary_lane"]
print("gpt-codex" if lane == "codex" else lane)
PY
}

emit_packet() {
    local id="$1" model
    model="$(packet_model backend-engineer)" || return 2
    cat <<PACKET_EOF
---
id: ${id}
run_id: ${id}
to_model: ${model}
specialist: backend-engineer
source_namespace: coding
mode: project
memory_aperture: default
parallel_safe: true
direct_lane_work_allowed: true
review_triggers: []
reviews: none
return_artifact: departments/coding/outbox/${id}-response.md
write_scope: ["departments/coding/outbox/${id}-response.md"]
---

# Live capability canary

Do exactly three things and nothing else. This packet is deliberately trivial:
it measures the transport, not the work.

1. Run \`git rev-parse --short HEAD\` and paste the literal output.
2. Invoke the project skill named \`probe-canary\` through this worker's runtime
   skill mechanism. Report the literal invocation, the resolved base directory,
   and whether its name resolved bare or required a prefix. Quote, **verbatim**,
   the first prose paragraph after its heading (excluding frontmatter).
   Do not paraphrase it or reconstruct it from memory -- the exact wording is
   the measurement. Use the copy this runtime resolves.
3. Write your response envelope to the return_artifact path above.

If the skill does not resolve, say so and paste the literal error. An absent
skill is a real result; an invented quotation is not.
PACKET_EOF
}

emit_mcp_packet() {
    local id="$1" model
    model="$(packet_model systems-engineer)" || return 2
    if [[ "${model}" != gpt-codex ]]; then
        printf 'canary.sh: systems-engineer no longer maps to the Codex MCP oracle\n' >&2
        return 2
    fi
    cat <<PACKET_EOF
---
id: ${id}
run_id: ${id}
to_model: gpt-codex
specialist: systems-engineer
source_namespace: coding
mode: project
memory_aperture: default
parallel_safe: true
direct_lane_work_allowed: true
review_triggers: []
reviews: none
return_artifact: departments/coding/outbox/${id}-response.md
write_scope: ["departments/coding/outbox/${id}-response.md"]
---

# Live Codex MCP-surface canary

Do exactly two things and nothing else. This packet measures the MCP surface of
the systems-engineer@gpt-codex board worker.

1. Enumerate the MCP server namespaces exposed by THIS worker's live tool
   inventory. Do not read an adapter or config file, and do not use a child
   \`codex mcp list\`: that starts a different process and reports configuration,
   not this worker's callable surface. If the runtime provides \`ALL_TOOLS\`,
   enumerate names beginning \`mcp__\`, extract the component between the first
   two \`__\` separators, deduplicate, and sort. Otherwise use the runtime's
   equivalent live tool-manifest operation. Also enumerate every complete MCP
   tool name in \`tool_names\`; each namespace must have at least one tool and
   every tool's namespace must be in \`server_prefixes\`. Make one authorized,
   bounded read-only call to every namespace found. Paste
   the literal inventory command/expression and literal output, then emit exactly
   one single-line record with sorted unique arrays (a prefix belongs in
   \`successful_probes\` only after a non-error call):

   \`MCP_SURFACE_JSON: {"inventory_command":"<literal command or expression>","server_prefixes":["<runtime prefix>"],"successful_probes":["<runtime prefix>"],"tool_names":["mcp__<runtime prefix>__<tool>"]}\`

2. Write your response envelope to the return_artifact path above.
PACKET_EOF
}

if [[ -n "${EMIT_PACKET_ID}" ]]; then
    emit_packet "${EMIT_PACKET_ID}"
    exit $?
fi
if [[ -n "${EMIT_MCP_PACKET_ID}" ]]; then
    emit_mcp_packet "${EMIT_MCP_PACKET_ID}"
    exit $?
fi
if (( EMIT_MCP_EXPECTATION_EXAMPLE )); then
    printf '%s\n' '["example_alpha","example_beta"]'
    exit 0
fi

# --- Probes 1, 2, 3, 5, 6: adjudicated from live board evidence ---------------
# One python pass over the registry, the outbox and the notify receipts. The
# registry is multi-megabyte, so it is loaded once and every probe reads that
# one parse.
run_evidence_probes() {
    if [[ ! -x "${CHRONO_PY}" ]]; then
        local probe
        for probe in dispatch round_trip skills labelling mcp_surface; do
            printf '%s|NOT_MEASURED|no canary interpreter at %s\n' "${probe}" "${CHRONO_PY}"
        done
        return
    fi
    CANARY_ROOT="${CANARY_ROOT}" \
    CANARY_TASK="${TASK_ID}" \
    CANARY_MCP_TASK="${MCP_TASK_ID}" \
    CANARY_SENTINEL="${SKILL_SENTINEL}" \
    CANARY_AGENTS_SENTINEL="${AGENTS_SKILL_SENTINEL}" \
    CANARY_MCP_MARKER="${MCP_SURFACE_MARKER}" \
    "${CHRONO_PY}" -B - <<'PY'
import json
import os
import re
import sys
from pathlib import Path

root = Path(os.environ["CANARY_ROOT"])
task_id = os.environ.get("CANARY_TASK", "").strip()
mcp_task_id = os.environ.get("CANARY_MCP_TASK", "").strip() or task_id
sentinel = os.environ["CANARY_SENTINEL"]

def emit(probe, status, detail):
    print(f"{probe}|{status}|{detail}")

registry_path = root / "_state" / "active-tasks.json"

# POSITIVE CONTROL for every registry-derived probe. An unreadable or empty
# registry is the case where a silent no-op would otherwise read as clean: no
# entries means no mismatches means "all good". It is NOT MEASURED instead.
registry = None
registry_problem = None
if not registry_path.exists():
    registry_problem = (
        f"no registry at {registry_path} -- `_state/` is gitignored, so a linked "
        "worktree never carries it; run this from the main checkout"
    )
else:
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        registry_problem = f"registry unreadable: {exc}"
    else:
        if not isinstance(registry, dict) or not registry:
            registry_problem = "registry parsed but holds no entries"
            registry = None

def entry_for(tid):
    return registry.get(tid) if isinstance(registry, dict) else None

def artifact_present(entry):
    """Is the entry's DECLARED return_artifact actually on disk, non-empty?"""
    declared = str((entry or {}).get("return_artifact") or "").strip()
    if not declared:
        return None, ""
    candidate = root / declared
    if candidate.is_file() and candidate.stat().st_size > 0:
        return True, declared
    return False, declared

def persisted_task_context(entry, tid):
    """Load the task/attempt-bound assembled brief that survives dispatch."""
    if not isinstance(entry, dict):
        return None, "registry entry is absent"
    attempt_id = str(entry.get("delivery_attempt_id") or "").strip()
    generation = entry.get("delivery_generation")
    safe_component = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}")
    if not safe_component.fullmatch(tid) or not safe_component.fullmatch(attempt_id):
        return None, "registry task or delivery attempt id is unsafe or absent"
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        return None, "registry delivery generation is absent or invalid"
    context_path = (
        root / "_state" / "board-dispatch" / f"{tid}.{attempt_id}.context.json"
    )
    if context_path.is_symlink() or not context_path.is_file():
        return None, f"persisted assembled brief is absent: {context_path.name}"
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"persisted assembled brief is unreadable: {exc}"
    authority = context.get("authority") if isinstance(context, dict) else None
    prompt = context.get("task_prompt") if isinstance(context, dict) else None
    if (
        not isinstance(context, dict)
        or context.get("schema") != "go-live-trusted-context/v1"
        or not isinstance(authority, dict)
        or authority.get("task_id") != tid
        or authority.get("attempt_id") != attempt_id
        or authority.get("generation") != generation
        or not isinstance(prompt, str)
        or not prompt.strip()
    ):
        return None, "persisted assembled brief failed task/attempt binding"
    return context, None

def persisted_task_prompt(entry, tid):
    context, problem = persisted_task_context(entry, tid)
    return (context["task_prompt"], None) if context else (None, problem)

def skill_oracle(entry, context):
    """Resolve the dispatch's home, never default an unknown lane to Claude."""
    authority = context["authority"]
    normalize = lambda lane: "codex" if lane == "gpt-codex" else lane
    delivered = normalize(entry.get("delivery_lane"))
    lane = normalize(authority.get("lane")) or delivered
    if delivered and lane != delivered:
        return None, None, "dispatch context and registry disagree on the lane"
    homes = {
        "claude": (".claude/skills", sentinel),
        "codex": (".agents/skills", os.environ["CANARY_AGENTS_SENTINEL"]),
        "gemini": ("model-lanes/gemini/.agents/skills", os.environ["CANARY_AGENTS_SENTINEL"]),
    }
    if not isinstance(lane, str) or lane not in homes:
        return None, None, f"skill discovery home is unmeasured for lane {lane!r}"
    home, expected = homes[lane]
    # Completion prunes the attempt directory and ref. The promoted quotation
    # and bound lane survive, so compare against that lane's discovery home in
    # the working tree under test (including Gemini's cwd bridge). This measures
    # the surviving quotation against the current copy, not a historical snapshot.
    skill_file = root / home / "probe-canary/SKILL.md"
    try:
        source = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, None, f"oracle absent or unreadable: {skill_file}: {exc}"
    if expected not in source:
        return None, None, f"oracle broken: {skill_file} no longer contains its sentinel"
    return skill_file, expected, None

# --- Probe 1: dispatch ------------------------------------------------------
# "A trivial task reaches a lane and returns." The evidence is delivery_history:
# `queued` alone means the packet was accepted, not that any lane ever saw it --
# which is exactly the shape the refused-before-host-admission fan-out left
# behind while every unit test stayed green.
if registry is None:
    emit("dispatch", "NOT_MEASURED", registry_problem)
elif not task_id:
    emit("dispatch", "NOT_MEASURED",
         "no --task given; Chrono runs the dispatch, this adjudicates it")
else:
    entry = entry_for(task_id)
    if entry is None:
        emit("dispatch", "NOT_MEASURED",
             f"{task_id} is not in the registry -- nothing to adjudicate")
    else:
        events = [
            str(h.get("event"))
            for h in entry.get("delivery_history") or []
            if isinstance(h, dict)
        ]
        claimed = "board-claimed" in events
        returned = "terminal" in events
        if claimed and returned:
            reason = ""
            for h in reversed(entry.get("delivery_history") or []):
                if isinstance(h, dict) and h.get("event") == "terminal":
                    reason = str(h.get("reason") or "")
                    break
            emit("dispatch", "PASS",
                 f"{task_id} reached lane {entry.get('delivery_lane')} and returned "
                 f"({reason or 'terminal, no reason recorded'})")
        else:
            emit("dispatch", "FAIL",
                 f"{task_id} delivery_history is {events or ['<empty>']}: "
                 f"claimed={claimed} returned={returned}")

# --- Probe 2: round trip ----------------------------------------------------
# The full orchestrator<->specialist path, which is where the fan-out defect
# actually lived. Three independent facts, all required: the envelope was
# written, the artifact was promoted to its declared path, and the notification
# spine emitted a receipt for this task.
receipts_dir = root / "_state" / "chrono-notify-receipts"
if registry is None:
    emit("round_trip", "NOT_MEASURED", registry_problem)
elif not task_id:
    emit("round_trip", "NOT_MEASURED",
         "no --task given; the round trip needs a real dispatch to adjudicate")
else:
    entry = entry_for(task_id)
    if entry is None:
        emit("round_trip", "NOT_MEASURED", f"{task_id} is not in the registry")
    else:
        # Source namespace locates the role; board transport always uses coding.
        mailbox = root / "departments" / "coding"
        envelope = mailbox / "outbox" / f"{task_id}-response.md"
        archived = mailbox / "archive" / f"{task_id}.md"
        have_envelope = envelope.is_file() or archived.is_file()

        promoted, declared = artifact_present(entry)

        # POSITIVE CONTROL for the spine leg. An absent receipts directory means
        # the spine was never observable here, which is unmeasured; a present
        # directory with no receipt for a terminal task is a real failure. Doctor
        # already reports the spine and by its own admission could not measure
        # it -- that is the distinction this control draws.
        spine = None
        if receipts_dir.is_dir():
            spine = any(
                task_id in p.read_text(encoding="utf-8", errors="replace")
                for p in receipts_dir.glob("*.sent")
            )

        legs = [
            f"envelope={'yes' if have_envelope else 'NO'}",
            f"artifact={'yes' if promoted else ('NO:' + declared if declared else 'UNDECLARED')}",
            f"notified={'yes' if spine else ('NO' if spine is False else 'unmeasurable')}",
        ]
        if spine is None or promoted is None:
            emit("round_trip", "NOT_MEASURED",
                 "; ".join(legs) + " -- a leg had no observable input")
        elif have_envelope and promoted and spine:
            emit("round_trip", "PASS", "; ".join(legs))
        else:
            emit("round_trip", "FAIL", "; ".join(legs))

# --- Probe 3: skills FIRE (not merely project) ------------------------------
# The oracle is a phrase the lane can only produce by reading the skill file.
# scripts/python/validate_skill_wiring.py already proves the file is wired and
# well-formed; wiring is projection. This asks the different question -- did a
# runtime actually load and execute it.
if registry is None:
    emit("skills", "NOT_MEASURED", registry_problem)
elif not task_id:
    emit("skills", "NOT_MEASURED",
         "no --task given; only a lane's own artifact can show a skill fired")
else:
    entry = entry_for(task_id)
    promoted, declared = artifact_present(entry) if entry else (False, "")
    # A task that was never ASKED to fire the skill cannot answer the question.
    # Without this the probe reports FAIL for every ordinary board task, which
    # is a fabricated finding -- and a probe that cries wolf gets ignored
    # exactly like doctor's permanently-yellow warnings did.
    context, request_problem = persisted_task_context(entry, task_id)
    request_prompt = context["task_prompt"] if context else None
    asked = request_prompt is not None and "probe-canary" in request_prompt
    if not entry or not promoted:
        emit("skills", "NOT_MEASURED",
             f"{task_id} promoted no artifact to read; the skills oracle needs one")
    elif request_problem:
        emit("skills", "NOT_MEASURED",
             f"{task_id} ask evidence is unavailable: {request_problem}")
    elif not asked:
        emit("skills", "NOT_MEASURED",
             f"{task_id} was never asked to invoke probe-canary "
             "(no such instruction in its task/attempt-bound assembled brief); "
             "dispatch the packet from --emit-packet to measure this")
    else:
        skill_file, expected, oracle_problem = skill_oracle(entry, context)
        if oracle_problem:
            emit("skills", "NOT_MEASURED", oracle_problem)
        else:
            text = (root / declared).read_text(encoding="utf-8", errors="replace")
            if expected in text:
                emit("skills", "PASS",
                     f"{task_id} quoted the probe-canary sentinel from {skill_file}")
            else:
                emit("skills", "FAIL",
                     f"{task_id} produced an artifact but never quoted the sentinel "
                     f"from {skill_file} -- projected, not fired")

# --- Probe 5: labelling / organisation --------------------------------------
# Do artifacts land where the contract said they would? Sampled over the most
# recent terminal tasks so the answer is about current behaviour, not history.
SAMPLE = 12
if registry is None:
    emit("labelling", "NOT_MEASURED", registry_problem)
else:
    terminal = [
        (tid, e) for tid, e in registry.items()
        if isinstance(e, dict)
        and str(e.get("status") or "") in {"complete", "completed", "needs_review"}
        and str(e.get("return_artifact") or "").strip()
    ]
    terminal.sort(key=lambda kv: str(kv[1].get("dispatched_at") or ""))
    sample = terminal[-SAMPLE:]
    if not sample:
        # POSITIVE CONTROL: an empty sample proves nothing. Zero mismatches out
        # of zero tasks is the silent no-op this vocabulary exists to catch.
        emit("labelling", "NOT_MEASURED",
             "no terminal task in the registry declares a return_artifact")
    else:
        missing = [tid for tid, e in sample if not artifact_present(e)[0]]
        if missing:
            emit("labelling", "FAIL",
                 f"{len(missing)}/{len(sample)} recent tasks: declared return_artifact "
                 f"absent on disk ({', '.join(missing[:4])})")
        else:
            emit("labelling", "PASS",
                 f"{len(sample)}/{len(sample)} recent tasks: artifact present at its "
                 "declared path")

# --- Probe 6: board-spawned MCP surface -------------------------------------
# Configuration is not evidence. The emitted packet asks the worker to derive
# server namespaces from its own live tool manifest and to make one bounded
# read-only call through every namespace. The expected list is NOT in the
# packet, so an artifact can only match it by measuring (or fabricating) the
# runtime result; the literal command/output requirement makes fabrication
# reviewable in the same way as the skill sentinel above.
mcp_marker = os.environ["CANARY_MCP_MARKER"]
prefix_pattern = r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*"
try:
    if "CANARY_MCP_EXPECTED_JSON" in os.environ:
        expected_mcp_json = os.environ["CANARY_MCP_EXPECTED_JSON"]
    else:
        expectation_path = Path(os.environ.get(
            "CANARY_MCP_EXPECTED_FILE", str(root / "_state/canary-mcp-expected.json")
        ))
        expected_mcp_json = expectation_path.read_text(encoding="utf-8")
    expected_mcp = json.loads(expected_mcp_json)
except (OSError, UnicodeError, ValueError):
    emit("mcp_surface", "NOT_MEASURED",
         "canary expectation unavailable or invalid; set CANARY_MCP_EXPECTED_JSON "
         "or CANARY_MCP_EXPECTED_FILE (see --help)")
else:
    if (
        not isinstance(expected_mcp, list)
        or not expected_mcp
        or any(not isinstance(item, str) or not re.fullmatch(prefix_pattern, item)
               for item in expected_mcp)
        or expected_mcp != sorted(set(expected_mcp))
    ):
        emit("mcp_surface", "NOT_MEASURED",
             "canary expectation is not a sorted unique non-empty string list")
    elif registry is None:
        emit("mcp_surface", "NOT_MEASURED", registry_problem)
    elif not mcp_task_id:
        emit("mcp_surface", "NOT_MEASURED",
             "no --mcp-task or --task given; only a board worker can expose its live tool manifest")
    else:
        entry = entry_for(mcp_task_id)
        promoted, declared = artifact_present(entry) if entry else (False, "")
        request_prompt, request_problem = persisted_task_prompt(entry, mcp_task_id)
        asked = request_prompt is not None and mcp_marker in request_prompt
        if not entry or not promoted:
            emit("mcp_surface", "NOT_MEASURED",
                 f"{mcp_task_id} promoted no artifact containing a live MCP report")
        elif request_problem:
            emit("mcp_surface", "NOT_MEASURED",
                 f"{mcp_task_id} ask evidence is unavailable: {request_problem}")
        elif not asked:
            emit("mcp_surface", "NOT_MEASURED",
                 f"{mcp_task_id} was never asked for {mcp_marker.rstrip(':')} evidence "
                 "in its task/attempt-bound assembled brief")
        else:
            artifact_text = (root / declared).read_text(
                encoding="utf-8", errors="replace"
            )
            reports = [
                line[len(mcp_marker):].strip()
                for line in artifact_text.splitlines()
                if line.startswith(mcp_marker)
            ]
            if len(reports) != 1:
                emit("mcp_surface", "NOT_MEASURED",
                     f"artifact contains {len(reports)} {mcp_marker.rstrip(':')} records; expected one")
            else:
                try:
                    report = json.loads(reports[0])
                except json.JSONDecodeError as exc:
                    emit("mcp_surface", "NOT_MEASURED",
                         f"artifact MCP report is invalid JSON: {exc}")
                else:
                    expected_keys = {
                        "tool_names", "inventory_command", "server_prefixes",
                        "successful_probes"
                    }
                    visible = report.get("server_prefixes") if isinstance(report, dict) else None
                    successful = report.get("successful_probes") if isinstance(report, dict) else None
                    tool_names = report.get("tool_names") if isinstance(report, dict) else None
                    command = report.get("inventory_command") if isinstance(report, dict) else None
                    lists_are_valid = all(
                        isinstance(values, list)
                        and all(isinstance(item, str) and re.fullmatch(prefix_pattern, item)
                                for item in values)
                        and values == sorted(set(values))
                        for values in (visible, successful)
                    )
                    tool_names_are_valid = (
                        isinstance(tool_names, list)
                        and all(
                            isinstance(item, str)
                            and re.fullmatch(rf"mcp__{prefix_pattern}__[A-Za-z0-9_]+", item)
                            for item in tool_names
                        )
                        and tool_names == sorted(set(tool_names))
                    )
                    if (
                        not isinstance(report, dict)
                        or set(report) != expected_keys
                        or not isinstance(command, str)
                        or not command.strip()
                        or "\n" in command
                        or not lists_are_valid
                        or not tool_names_are_valid
                    ):
                        emit("mcp_surface", "NOT_MEASURED",
                             "artifact MCP report has the wrong schema or unsorted values")
                    elif sorted({name.split("__", 2)[1] for name in tool_names}) != visible:
                        emit("mcp_surface", "NOT_MEASURED",
                             "artifact MCP tool inventory does not match its visible namespaces")
                    elif visible == expected_mcp and successful == expected_mcp:
                        emit("mcp_surface", "PASS",
                             f"live prefixes and bounded calls match {expected_mcp}; "
                             f"enumerated tools={len(tool_names)}")
                    else:
                        missing = sorted(set(expected_mcp) - set(visible))
                        unexpected = sorted(set(visible) - set(expected_mcp))
                        unprobed = sorted(set(visible) - set(successful))
                        emit("mcp_surface", "FAIL",
                             f"expected={expected_mcp}; visible={visible}; "
                             f"missing={missing}; unexpected={unexpected}; "
                             f"no successful bounded call={unprobed}")
PY
}

# --- Probe 4: memory record -> recall round trip -----------------------------
# A note COUNT cannot answer this. doctor.sh already warns that auto-capture
# wrote no note six times in seven days and still cannot say whether recall
# works; those are different subsystems and only a round trip separates them.
#
# The pre-recall of the nonce is the positive control: it must return ZERO
# hits. Without it a recall that matched everything, or one served from a stale
# index, would look identical to a working one.
run_memory_probe() {
    if [[ "${MEMORY_WRITE}" == 0 ]]; then
        route memory NOT_MEASURED "--no-memory-write given; a read-only check cannot prove record->recall"
        return
    fi
    if [[ ! -x "${CHRONO_PY}" ]]; then
        route memory NOT_MEASURED "no vault interpreter at ${CHRONO_PY}"
        return
    fi
    if [[ -z "${CHRONO_VAULT_ROOT:-}" ]]; then
        # Known board-spawn gotcha: the vault fails closed with the root unset,
        # and a failed-closed vault must not read as a working one.
        route memory NOT_MEASURED "CHRONO_VAULT_ROOT is unset; the vault fails closed"
        return
    fi
    local out
    out="$(PYTHONPATH="${VAULT_ROOT}/plugins/chrono-vault" "${CHRONO_PY}" -B - <<'PY' 2>&1
import uuid
try:
    import notes
    import recall as recall_mod
except Exception as exc:  # noqa: BLE001 - any import failure is unmeasured
    print(f"NOT_MEASURED|vault modules did not import: {exc}")
    raise SystemExit(0)

nonce = "canaryprobe" + uuid.uuid4().hex[:12]
try:
    pre = recall_mod.recall(query=nonce, limit=3)
except Exception as exc:  # noqa: BLE001
    print(f"NOT_MEASURED|pre-recall control could not run: {exc}")
    raise SystemExit(0)

if pre.get("results"):
    # The control failed, so nothing after it can be trusted.
    print(f"NOT_MEASURED|pre-recall control returned {len(pre['results'])} hits "
          f"for an unused nonce; the oracle is not discriminating")
    raise SystemExit(0)

try:
    written = notes.record("learning", {
        "title": f"canary memory round-trip probe {nonce}",
        "body": (f"bin/canary.sh live record->recall probe, token {nonce}. "
                 "Disposable telemetry, not a finding."),
        "status": "candidate",
    })
except Exception as exc:  # noqa: BLE001
    print(f"FAIL|record raised {type(exc).__name__}: {exc}")
    raise SystemExit(0)

note_id = written.get("id", "")
try:
    post = recall_mod.recall(query=nonce, limit=3)
except Exception as exc:  # noqa: BLE001
    print(f"FAIL|recorded {note_id} but recall raised {type(exc).__name__}: {exc}")
    raise SystemExit(0)

ids = [r.get("id") for r in post.get("results", [])]
if note_id and note_id in ids:
    print(f"PASS|recorded {note_id} and recalled it (pre-recall control: 0 hits, "
          f"index_dirty={written.get('index_dirty')})")
else:
    print(f"FAIL|recorded {note_id} but recall for its own nonce returned {ids or '[]'}")
PY
)"
    local status="${out%%|*}"
    local detail="${out#*|}"
    case "${status}" in
        PASS|FAIL|NOT_MEASURED) route memory "${status}" "${detail}" ;;
        *) route memory NOT_MEASURED "probe produced no verdict: ${out}" ;;
    esac
}

# --- Inverted controls ------------------------------------------------------
# Break each capability against a fixture and require the probe NOT to pass. If
# any inversion still reports PASS, this program is decoration and says so.
# Script-scoped, not `local`: the EXIT trap fires after the function has
# returned, so a function-local name is already out of scope by then and the
# fixture leaks (with `set -u`, loudly).
CANARY_FIXTURE=""
cleanup_fixture() { [[ -n "${CANARY_FIXTURE}" ]] && rm -rf "${CANARY_FIXTURE}"; }

fixture_verdict() {
    local fixture="$1" probe="$2" expected="$3" label="$4" out
    out="$(CANARY_ROOT_UNDER_TEST="${fixture}" bash "${BASH_SOURCE[0]}" \
        --task TASK-2099-01-01-0003-good --no-memory-write 2>&1)"
    if grep -F "[${expected}]" <<<"${out}" | grep -q " ${probe} "; then
        printf '  inversion holds    %-28s %s\n' "${label}" "${expected}"
    else
        printf '  INVERSION FAILED   %-28s expected %s\n%s\n' "${label}" "${expected}" "${out}"
        return 1
    fi
}

run_self_test() {
    local bad=0
    # Synthetic projection, isolated from any operator-local configuration.
    local CANARY_MCP_EXPECTED_JSON='["fixture_alpha","fixture_beta"]'
    export CANARY_MCP_EXPECTED_JSON
    CANARY_FIXTURE="$(mktemp -d "${TMPDIR:-/tmp}/canary-selftest.XXXXXX")" || exit 2
    trap cleanup_fixture EXIT
    local fixture="${CANARY_FIXTURE}"

    printf 'inverted controls (fixtures only, no live state touched)\n'

    # 1. Absent registry: everything registry-derived must be NOT MEASURED.
    local empty_root="${fixture}/empty"
    mkdir -p "${empty_root}"
    local out
    out="$(CANARY_ROOT_UNDER_TEST="${empty_root}" bash "${BASH_SOURCE[0]}" \
        --task TASK-X --no-memory-write 2>&1)"
    for probe in dispatch round_trip skills labelling mcp_surface; do
        if grep -q "^\[NOT MEASURED\].* ${probe} " <<<"${out}"; then
            printf '  inversion holds    %-28s NOT MEASURED\n' "absent registry / ${probe}"
        else
            printf '  INVERSION FAILED  %-28s expected NOT MEASURED\n' "absent registry / ${probe}"
            bad=1
        fi
    done

    # 2. Broken capabilities against a populated fixture tree.
    local broken="${fixture}/broken"
    mkdir -p "${broken}/_state/chrono-notify-receipts" \
             "${broken}/departments/coding/outbox" \
             "${broken}/.claude/skills/probe-canary"
    printf 'this file deliberately omits the sentinel\n' \
        > "${broken}/.claude/skills/probe-canary/SKILL.md"
    cat > "${broken}/_state/active-tasks.json" <<'JSON_EOF'
{
  "TASK-2099-01-01-0001-brk": {
    "source_namespace": "coding",
    "status": "complete",
    "dispatched_at": "2099-01-01T00:00:00+00:00",
    "delivery_lane": "claude",
    "return_artifact": "departments/coding/outbox/TASK-2099-01-01-0001-brk-response.md",
    "delivery_history": [{"event": "queued", "at": "2099-01-01T00:00:00+00:00"}]
  }
}
JSON_EOF
    out="$(CANARY_ROOT_UNDER_TEST="${broken}" bash "${BASH_SOURCE[0]}" \
        --task TASK-2099-01-01-0001-brk --no-memory-write 2>&1)"
    # dispatch: queued but never claimed -> the refused-fan-out shape.
    grep -q '^\[FAIL\].* dispatch ' <<<"${out}" \
        && printf '  inversion holds    %-28s FAIL\n' "queued-but-never-claimed" \
        || { printf '  INVERSION FAILED  %-28s expected FAIL\n' "queued-but-never-claimed"; bad=1; }
    # round trip: no envelope, no artifact, no receipt.
    grep -q '^\[FAIL\].* round_trip ' <<<"${out}" \
        && printf '  inversion holds    %-28s not a pass\n' "severed round trip" \
        || { printf '  INVERSION FAILED  %-28s expected FAIL\n' "severed round trip"; bad=1; }
    # skills: without a promoted artifact there is no answer to adjudicate.
    grep -q '^\[NOT MEASURED\].* skills ' <<<"${out}" \
        && printf '  inversion holds    %-28s NOT MEASURED\n' "skill artifact missing" \
        || { printf '  INVERSION FAILED  %-28s expected NOT MEASURED\n' "skill artifact missing"; bad=1; }
    # labelling: the declared artifact was never promoted.
    grep -q '^\[FAIL\].* labelling ' <<<"${out}" \
        && printf '  inversion holds    %-28s FAIL\n' "artifact missing at declared path" \
        || { printf '  INVERSION FAILED  %-28s expected FAIL\n' "artifact missing at declared path"; bad=1; }

    # 3. Skills projected-but-not-fired: the sentinel is intact, the packet DID
    #    ask for the skill, and the artifact still never quotes it. This is the
    #    case the whole probe exists for, and it must be FAIL, not unmeasured.
    local mute="${fixture}/mute"
    mkdir -p "${mute}/_state/board-dispatch" \
             "${mute}/departments/coding/outbox" "${mute}/.claude/skills/probe-canary" \
             "${mute}/.agents/skills/probe-canary"
    printf 'If you are reading this, **%s** -- the runtime found this file.\n' \
        "${SKILL_SENTINEL}" > "${mute}/.claude/skills/probe-canary/SKILL.md"
    printf '%s Report back, verbatim:\n' "${AGENTS_SKILL_SENTINEL}" \
        > "${mute}/.agents/skills/probe-canary/SKILL.md"
    cat > "${mute}/_state/board-dispatch/TASK-2099-01-01-0002-mute.d-mute.context.json" <<'JSON_EOF'
{
  "schema": "go-live-trusted-context/v1",
  "authority": {
    "task_id": "TASK-2099-01-01-0002-mute",
    "attempt_id": "d-mute",
    "lane": "codex",
    "generation": 1
  },
  "task_prompt": "Invoke the project skill named probe-canary and quote it. Return MCP_SURFACE_JSON: evidence."
}
JSON_EOF
    printf '%s\n%s %s\n' \
        'I ran the task. I did not invoke any skill.' \
        "${MCP_SURFACE_MARKER}" \
        '{"tool_names":["mcp__fixture_alpha__probe"],"inventory_command":"fixture inventory","server_prefixes":["fixture_alpha"],"successful_probes":["fixture_alpha"]}' \
        > "${mute}/departments/coding/outbox/TASK-2099-01-01-0002-mute-response.md"
    cat > "${mute}/_state/active-tasks.json" <<'JSON_EOF'
{
  "TASK-2099-01-01-0002-mute": {
    "source_namespace": "coding",
    "status": "complete",
    "dispatched_at": "2099-01-01T00:00:00+00:00",
    "delivery_attempt_id": "d-mute",
    "delivery_generation": 1,
    "return_artifact": "departments/coding/outbox/TASK-2099-01-01-0002-mute-response.md",
    "delivery_history": [
      {"event": "queued"}, {"event": "board-claimed"}, {"event": "terminal"}
    ]
  }
}
JSON_EOF
    out="$(CANARY_ROOT_UNDER_TEST="${mute}" bash "${BASH_SOURCE[0]}" \
        --task TASK-2099-01-01-0002-mute --no-memory-write 2>&1)"
    grep -q '^\[FAIL\].* skills ' <<<"${out}" \
        && printf '  inversion holds    %-28s FAIL\n' "skill asked for, never fired" \
        || { printf '  INVERSION FAILED  %-28s expected FAIL\n' "skill asked for, never fired"; bad=1; }
    # MCP surface: the worker returned a well-formed live report, but one
    # expected namespace is absent. This must be FAIL, not NOT MEASURED.
    grep -q '^\[FAIL\].* mcp_surface ' <<<"${out}" \
        && printf '  inversion holds    %-28s FAIL\n' "MCP namespace missing" \
        || { printf '  INVERSION FAILED  %-28s expected FAIL\n' "MCP namespace missing"; bad=1; }

    # 4. POSITIVE CONTROL for the adjudicator itself. A probe stuck at FAIL is
    #    as useless as one stuck at PASS: it would satisfy every inversion above
    #    while measuring nothing. On a fixture where all five capabilities work,
    #    all five must report PASS.
    local good="${fixture}/good"
    mkdir -p "${good}/_state/chrono-notify-receipts" \
             "${good}/_state/board-dispatch" "${good}/departments/coding/outbox" \
             "${good}/.claude/skills/probe-canary" "${good}/.agents/skills/probe-canary"
    printf 'If you are reading this, **%s** -- the runtime found this file.\n' \
        "${SKILL_SENTINEL}" > "${good}/.claude/skills/probe-canary/SKILL.md"
    printf '%s Report back, verbatim:\n' "${AGENTS_SKILL_SENTINEL}" \
        > "${good}/.agents/skills/probe-canary/SKILL.md"
    cat > "${good}/_state/board-dispatch/TASK-2099-01-01-0003-good.d-good.context.json" <<'JSON_EOF'
{
  "schema": "go-live-trusted-context/v1",
  "authority": {
    "task_id": "TASK-2099-01-01-0003-good",
    "attempt_id": "d-good",
    "lane": "codex",
    "generation": 1
  },
  "task_prompt": "Invoke the project skill named probe-canary and quote it. Return MCP_SURFACE_JSON: evidence."
}
JSON_EOF
    printf 'HEAD abc1234. The skill says: %s.\n%s {"tool_names":["mcp__fixture_alpha__probe","mcp__fixture_beta__probe"],"inventory_command":"fixture inventory","server_prefixes":%s,"successful_probes":%s}\n' \
        "${AGENTS_SKILL_SENTINEL}" "${MCP_SURFACE_MARKER}" \
        "${CANARY_MCP_EXPECTED_JSON}" "${CANARY_MCP_EXPECTED_JSON}" \
        > "${good}/departments/coding/outbox/TASK-2099-01-01-0003-good-response.md"
    printf '{"event_key":"25|TASK-2099-01-01-0003-good|complete"}\n' \
        > "${good}/_state/chrono-notify-receipts/good.sent"
    cat > "${good}/_state/active-tasks.json" <<'JSON_EOF'
{
  "TASK-2099-01-01-0003-good": {
    "source_namespace": "security",
    "status": "complete",
    "dispatched_at": "2099-01-01T00:00:00+00:00",
    "delivery_lane": "codex",
    "delivery_attempt_id": "d-good",
    "delivery_generation": 1,
    "return_artifact": "departments/coding/outbox/TASK-2099-01-01-0003-good-response.md",
    "delivery_history": [
      {"event": "queued"},
      {"event": "board-claimed"},
      {"event": "terminal", "reason": "board-receipt:complete"}
    ]
  }
}
JSON_EOF
    out="$(CANARY_ROOT_UNDER_TEST="${good}" bash "${BASH_SOURCE[0]}" \
        --task TASK-2099-01-01-0003-good --no-memory-write 2>&1)"
    for probe in dispatch round_trip skills labelling mcp_surface; do
        if grep -q "^\[PASS\].* ${probe} " <<<"${out}"; then
            printf '  control holds      %-28s PASS\n' "working fixture / ${probe}"
        else
            printf '  CONTROL FAILED     %-28s probe never passes; it measures nothing\n' \
                "working fixture / ${probe}"
            bad=1
        fi
    done

    # The two homes MUST differ. A Codex answer quoting the controller copy
    # must fail even while both files are present and their oracles are intact.
    local wrong="${fixture}/wrong-home"
    cp -R "${good}" "${wrong}"
    printf '%s\n' "${SKILL_SENTINEL}" \
        > "${wrong}/departments/coding/outbox/TASK-2099-01-01-0003-good-response.md"
    fixture_verdict "${wrong}" skills FAIL 'controller quote on Codex' || bad=1
    printf 'the worker oracle drifted\n' > "${wrong}/.agents/skills/probe-canary/SKILL.md"
    fixture_verdict "${wrong}" skills 'NOT MEASURED' 'worker sentinel removed' || bad=1

    # Exercise every proven lane home after completion prunes the attempt.
    # The preserved quotation must pass without any worker directory or Git
    # repository; missing source, a wrong quotation, or unbound lane must not.
    CANARY_TEST_SCRIPT="${BASH_SOURCE[0]}" CANARY_GOOD="${good}" \
    CANARY_FIXTURES="${fixture}" \
    "${CHRONO_PY}" -B - <<'PY' || bad=1
import json
import os
from pathlib import Path
import shutil
import subprocess

script = str(Path(os.environ["CANARY_TEST_SCRIPT"]).resolve())
fixtures = Path(os.environ["CANARY_FIXTURES"])
tid = "TASK-2099-01-01-0003-good"
for lane, home in (("claude", ".claude/skills"), ("gpt-codex", ".agents/skills"),
                   ("gemini", "model-lanes/gemini/.agents/skills")):
    root = fixtures / lane
    shutil.copytree(os.environ["CANARY_GOOD"], root)
    context_path = root / "_state/board-dispatch" / f"{tid}.d-good.context.json"
    context = json.loads(context_path.read_text())
    context["authority"].update(lane=lane, pool_root=str(root / "_state/board-worktrees"))
    context_path.write_text(json.dumps(context))
    registry_path = root / "_state/active-tasks.json"
    registry = json.loads(registry_path.read_text())
    registry[tid]["delivery_lane"] = lane
    registry_path.write_text(json.dumps(registry))
    source = root / (".claude" if lane == "claude" else ".agents") / "skills/probe-canary/SKILL.md"
    skill_source = source.read_text()
    skill_file = root / home / "probe-canary/SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(skill_source)
    artifact = root / registry[tid]["return_artifact"]
    artifact.write_text(skill_source)
    assert not (root / "_state/board-worktrees").exists()
    assert not (root / ".git").exists()

    def expect(status, label):
        result = subprocess.run(
            ["bash", script, "--task", tid, "--no-memory-write"],
            env={**os.environ, "CANARY_ROOT_UNDER_TEST": str(root)},
            text=True, capture_output=True, timeout=30,
        )
        assert any(line.startswith(f"[{status}]") and line.split("]", 1)[1].split()[0] == "skills"
                   for line in result.stdout.splitlines()), (label, result.stdout, result.stderr)
        print(f"  {'control' if status == 'PASS' else 'inversion'} holds    {label}: {status}")

    expect("PASS", f"{lane} released attempt / working-tree skill")
    artifact.write_text("The task ran without invoking the skill.\n")
    expect("FAIL", f"{lane} skill never fired")
    artifact.write_text(skill_source)
    skill_file.write_text("The resolved skill no longer contains its sentinel.\n")
    expect("NOT MEASURED", f"{lane} resolved sentinel removed")
    skill_file.write_text(skill_source)
    # A directory is observable but cannot supply a readable skill body.
    skill_file.rename(skill_file.with_suffix(".saved"))
    expect("NOT MEASURED", f"{lane} resolved skill absent")
    skill_file.mkdir()
    expect("NOT MEASURED", f"{lane} resolved skill unreadable")
    skill_file.rmdir()
    skill_file.with_suffix(".saved").rename(skill_file)
    context["authority"]["lane"] = "grok"
    registry[tid]["delivery_lane"] = "grok"
    context_path.write_text(json.dumps(context))
    registry_path.write_text(json.dumps(registry))
    expect("NOT MEASURED", "unknown lane does not default to Claude")
    context["authority"].update(lane=lane, attempt_id="d-other")
    registry[tid]["delivery_lane"] = lane
    context_path.write_text(json.dumps(context))
    registry_path.write_text(json.dumps(registry))
    expect("NOT MEASURED", "another attempt cannot supply bound evidence")
PY

    # Packet regressions are checked against the recipient's runtime map, not
    # a second hardcoded lane. Neither distinct skill answer may leak into it.
    local packet model specialist
    for specialist in backend-engineer systems-engineer; do
        if [[ "${specialist}" == backend-engineer ]]; then
            packet="$(emit_packet TASK-2099-01-01-0004-packet)" || { bad=1; continue; }
        else
            packet="$(emit_mcp_packet TASK-2099-01-01-0005-mcp)" || { bad=1; continue; }
        fi
        model="$(awk -F '\t' -v role="${specialist}" '$1 == role {print $7}' \
            "${VAULT_ROOT}/shared/specialist-runtime-map.tsv")"
        [[ "${model}" == codex ]] && model=gpt-codex
        if grep -qx 'reviews: none' <<<"${packet}" \
            && grep -qx "to_model: ${model}" <<<"${packet}" \
            && ! grep -Fq "${SKILL_SENTINEL}" <<<"${packet}" \
            && ! grep -Fq "${AGENTS_SKILL_SENTINEL}" <<<"${packet}"; then
            printf '  control holds      %-28s PASS\n' "${specialist} packet contract"
        else
            printf '  CONTROL FAILED     %-28s invalid reviews, route, or leaked oracle\n' "${specialist} packet"
            bad=1
        fi
    done

    # Memory uses fake in-process modules in a disposable vault fixture. This
    # proves the success and failure verdicts without touching the real vault.
    local memory="${fixture}/memory" variant expected
    mkdir -p "${memory}/plugins/chrono-vault"
    cat > "${memory}/plugins/chrono-vault/notes.py" <<'PY'
import os

def record(note_type, fields):
    if os.environ["CANARY_MEMORY_CONTROL"] == "record-error":
        raise RuntimeError("fixture record is broken")
    return {"id": "mem-canary-fixture", "index_dirty": False}
PY
    cat > "${memory}/plugins/chrono-vault/recall.py" <<'PY'
import os

calls = 0

def recall(query, limit):
    global calls
    calls += 1
    variant = os.environ["CANARY_MEMORY_CONTROL"]
    if calls == 1:
        return {"results": [{"id": "unrelated"}] if variant == "dirty-pre" else []}
    if variant == "recall-error":
        raise RuntimeError("fixture recall is broken")
    return {"results": [] if variant == "missing-note" else [{"id": "mem-canary-fixture"}]}
PY
    for variant in good record-error recall-error missing-note dirty-pre; do
        expected=FAIL
        [[ "${variant}" == good ]] && expected=PASS
        [[ "${variant}" == dirty-pre ]] && expected='NOT MEASURED'
        out="$(VAULT_ROOT="${memory}" CANARY_ROOT_UNDER_TEST="${good}" \
            CHRONO_PY="${CHRONO_PY}" CHRONO_VAULT_ROOT="${memory}" \
            CANARY_MEMORY_CONTROL="${variant}" \
            bash "${BASH_SOURCE[0]}" --task TASK-2099-01-01-0003-good 2>&1)"
        if grep -F "[${expected}]" <<<"${out}" | grep -q ' memory '; then
            printf '  control holds      %-28s %s\n' "memory / ${variant}" "${expected}"
        else
            printf '  CONTROL FAILED     %-28s expected %s\n%s\n' "memory / ${variant}" "${expected}" "${out}"
            bad=1
        fi
    done

    # 5. Memory: a vault that fails closed must never read as a working one.
    # No live write happens: the unset root is refused before record is reached.
    out="$(env -u CHRONO_VAULT_ROOT bash "${BASH_SOURCE[0]}" 2>&1)"
    grep -q '^\[NOT MEASURED\].* memory ' <<<"${out}" \
        && printf '  inversion holds    %-28s NOT MEASURED\n' "vault root unset" \
        || { printf '  INVERSION FAILED  %-28s expected NOT MEASURED\n' "vault root unset"; bad=1; }

    if (( bad )); then
        printf '\nself-test FAILED: a probe cannot fail, so it is not a gate.\n'
        return 1
    fi
    printf '\nself-test passed: every probe demonstrably fails when its capability is broken.\n'
    return 0
}

if (( SELF_TEST )); then
    run_self_test
    exit $?
fi

# --- Run --------------------------------------------------------------------
printf '=== live capability canary ===\n'
printf 'root: %s\n' "${CANARY_ROOT}"
[[ -n "${TASK_ID}" ]] && printf 'adjudicating: %s\n' "${TASK_ID}"
[[ -n "${MCP_TASK_ID}" ]] && printf 'adjudicating MCP surface: %s\n' "${MCP_TASK_ID}"
printf '\n'

while IFS='|' read -r probe status detail; do
    [[ -z "${probe}" ]] && continue
    route "${probe}" "${status}" "${detail}"
done < <(run_evidence_probes)

run_memory_probe

printf '\nsummary: %d pass, %d fail, %d not measured\n' \
    "${#PASSES[@]}" "${#FAILURES[@]}" "${#UNMEASURED[@]}"

if (( ${#UNMEASURED[@]} )); then
    printf '\nNOT MEASURED is not a pass. Chrono runs the two role-specific packets:\n'
    printf '  CORE_ID=TASK-$(date -u +%%Y-%%m-%%d-%%H%%M)-canary\n'
    printf '  MCP_ID="${CORE_ID}-mcp"\n'
    printf '  bin/canary.sh --emit-packet "$CORE_ID" > departments/coding/inbox/"$CORE_ID".md\n'
    printf '  bin/canary.sh --emit-mcp-packet "$MCP_ID" > departments/coding/inbox/"$MCP_ID".md\n'
    printf '  bin/send-task.sh departments/coding/inbox/"$CORE_ID".md\n'
    printf '  bin/send-task.sh departments/coding/inbox/"$MCP_ID".md\n'
    printf '  # wait for both envelopes, then:\n'
    printf '  bin/canary.sh --task "$CORE_ID" --mcp-task "$MCP_ID"\n'
fi

if (( ${#FAILURES[@]} )); then
    exit 1
fi
if (( ${#UNMEASURED[@]} )); then
    exit 2
fi
exit 0
