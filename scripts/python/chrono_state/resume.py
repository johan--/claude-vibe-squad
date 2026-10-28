"""Bounded, source-tagged Chrono resume capsule.

Regenerated from the decision-authority record + the live board registry (never a
summary-of-a-summary). Every projected item carries a source ID
([DEC-...] / [TASK-...] / [THREAD-...] / [OPEN-WORK-...]) so the
capsule is a cache pointing at authority, never authority itself. This becomes the new
content of `current.md`: a thin derived cursor, not a narrative log.

A derived cache is only worth having if it is regenerated; `write_capsule` is the
single writer, and `bin/chrono-resume-capsule.sh` is the entry point that calls it.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from chrono_state import registry as registry_state
from chrono_state import workboard as workboard_state
from chrono_state.decisions import active_decisions, decision_home
from chrono_state.registry import registry_view
from chrono_state.thread_charters import (
    load_archived_debt,
    CHARTERS_REL,
    ThreadCharter,
    clip,
    load_active_charters,
)

CAPSULE_PATH = (
    Path(os.environ.get("VAULT_ROOT", ".")) / "_state" / "chrono" / "resume.md"
)
QUEUE_PATH = Path(os.environ.get("VAULT_ROOT", ".")) / "_state" / "chrono-queue.md"
ARCHIVED_DEBT_ROOT = Path(os.environ.get("VAULT_ROOT", "."))
TASKS_HEADING = "## Live tasks (dispatched / in-flight / review-required)"
DEFERRED_HEADING = "## Deferred owed work (awaiting a Chrono/operator action)"
QUEUE_HEADING = "## Pending completions (specialist returns awaiting a decision)"
ARCHIVED_DEBT_HEADING = "## Archived with unfinished business"
OPEN_WORK_HEADING = "## Open work (raised and not yet done)"
OPEN_WORK_REL = workboard_state.WORKBOARD_REL

# Pending completion rows are parked work, but their raw lifecycle statuses do
# not all name the same action. Keep the four operator-facing causes stable.
# Registry statuses supply task-lifecycle signals; the queue also has one typed,
# queue-only coordination signal emitted by ``registry_reconciler``.
REVIEW_REQUIRED = "REVIEW-REQUIRED"
COORDINATION_REQUESTED = "COORDINATION-REQUESTED"
NEEDS_HUMAN = "NEEDS-HUMAN"
BLOCKED = "BLOCKED"
PARKED_CAUSES = (
    REVIEW_REQUIRED,
    COORDINATION_REQUESTED,
    NEEDS_HUMAN,
    BLOCKED,
)
QUEUE_ONLY_STATUSES = frozenset({COORDINATION_REQUESTED})
UNCLASSIFIED_CAUSE = "UNCLASSIFIED-CAUSE"
_LEGACY_NEEDS_REVIEW = "NEEDS-REVIEW"
_STATUS_TOKEN_RE = re.compile(r"[^A-Z0-9]+")

# A pending-completions row is only owed while its task is still open. These
# statuses mean the work has already been settled by the board (auto-closed on a
# landed response, hand-closed, cancelled, or completed), so counting them under
# "awaiting a decision" would misrepresent handled work as outstanding debt.
# Normalized through `_status_token`, so spelling/casing variants collapse.
HANDLED_PENDING_STATUSES = frozenset(
    {"AUTO-CLOSED", "TASK-CLOSED", "CANCELLED", "COMPLETE", "COMPLETED"}
)
# `## Active decisions` is authority, but the capsule only needs the authority
# that still bears on current work. Older confirmed decisions live in the
# journal; the capsule names how many there are and points at it rather than
# re-stating months-old statements every session (audit finding #5).
ACTIVE_DECISION_MAX_AGE_DAYS = 30

# Loud markers. A read that failed must never render as a read that found
# nothing owed -- root CLAUDE.md § Session Resume: "nothing owed is ever
# silently absent". Each of these is the failure's own line in the capsule.
ARCHIVED_DEBT_SCAN_FAILED = "- archived-charter debt scan failed"
QUEUE_UNREADABLE_LINE = (
    "- pending completion count unavailable (_state/chrono-queue.md exists but "
    "could not be read — owed work may be hidden here; read it directly)"
)


def _archived_debt_count(root=None, fallback=0) -> int:
    """True count of charters archived while still owed, or ``fallback`` on error.

    Kept separate from ``_archived_debt_rows`` on purpose: that function's
    ``(rows, failed)`` shape is pinned by callers and mocks outside this file's
    task scope, and its rows cap at 8 (+1 "… and more"). The collapsed one-line
    form needs the true number of owed charters, not the capped display-row
    count, so it recomputes the length here — only on the collapse path,
    which runs just when real rows are present.
    """
    try:
        owed = load_archived_debt(ARCHIVED_DEBT_ROOT if root is None else root)
    except Exception:  # noqa: BLE001 — the capsule must render regardless
        return fallback
    return len(owed)


def _archived_debt_rows(root=None) -> tuple[list[str], bool]:
    """Bounded, warn-only rows naming charters archived while still owed.

    Every reader of charter state stops at ``active/``; archiving is a bare
    ``mv``. So debt is visible until the move and invisible after it -- which is
    how six queued items and three unticked boxes went unread. This says what is
    owed and nothing else: it renders no verdict, gates nothing, and no caller
    may branch on it.

    Probation: if this stays empty across roughly ten sessions, the written
    discipline is holding and this code should be deleted. A check that never
    fires is not a safety net, it is a cost.
    """
    try:
        owed = load_archived_debt(ARCHIVED_DEBT_ROOT if root is None else root)
    except Exception as exc:  # noqa: BLE001 — the capsule must render regardless
        # A scan that blew up is not a scan that found nothing. Returning []
        # here rendered a broken archive rail exactly like a clean one, which
        # is the ambiguity this whole block exists to kill.
        # Return the failure as a FLAG, not only as prose. `_render` protects
        # this row from the token-bound cascade, and recovering that decision by
        # re-parsing the row's own wording meant a reword silently disabled the
        # protection -- collapsing the marker into a charter count nobody
        # measured.
        return (
            [
                f"{ARCHIVED_DEBT_SCAN_FAILED} ({exc}) — read "
                "_state/chrono/thread-charters/{complete,parked} directly"
            ],
            True,
        )
    rows = []
    for charter in owed[:8]:
        bits = []
        if not charter.done_when_met:
            bits.append("DONE-WHEN unticked")
        if charter.unresolved_queues:
            bits.append(f"{len(charter.unresolved_queues)} unresolved QUEUE")
        rows.append(f"- {charter.path.name}: {', '.join(bits)}")
    if len(owed) > 8:
        rows.append(f"- … and {len(owed) - 8} more")
    return rows, False


THREAD_HEADING = "## Active thread / Owed attention"
MAX_PROJECTED_CHARTERS = 8
MAX_PROJECTED_QUEUES = 4
MAX_PROJECTED_DONE_WHEN = 6
MAX_PROJECTED_OPEN_WORK = workboard_state.MAX_PROJECTED_ITEMS
DEPARTMENTS_ROOT = Path(os.environ.get("VAULT_ROOT", ".")) / "departments"
# A NEEDS HUMAN line carries the specialist's own one-line ask, read from its
# outbox response. Responses are unbounded prose and these lines are never
# dropped by the token cascade, so the read, the summary and the number of
# enriched lines are each capped; anything past the caps renders plain.
# 128, not 140: the ' untrusted: "…"' label costs 12 characters and the line budget is fixed.
NEEDS_HUMAN_SUMMARY_CHARS = 128
NEEDS_HUMAN_RESPONSE_MAX_BYTES = 256 * 1024
MAX_ENRICHED_NEEDS_HUMAN = 8
NEEDS_HUMAN_LANE_CHARS = 60
DECISION_SECTION_NOTE = "decision section present"
_SAFE_TASK_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_DECISION_HEADING_RE = re.compile(r"#{1,6}\s.*operator decision", re.IGNORECASE)
# Square brackets are the capsule's source-tag syntax and " | " introduces the
# decision note, so a summary must not be able to counterfeit either.
_SUMMARY_NOISE_RE = re.compile(r"[\[\]|]")
OPEN_WORK_CLIP = workboard_state.SUMMARY_CLIP
OPEN_WORK_NEXT_CLIP = workboard_state.ACTION_CLIP


def open_work_items(path=None):
    """Compatibility delegate to the one parser/projector in ``workboard``.

    The three-tuple shape remains for callers and tests written before the
    append-only event spine. No parsing or prominence decision lives here.
    """
    dest = Path(path) if path else CAPSULE_PATH.parent / OPEN_WORK_REL.name
    return workboard_state.resume_rows(dest)


def _open_work_lines(items, show_detail):
    """Compatibility delegate to the canonical workboard projection renderer."""
    return workboard_state.render_resume_rows(items, show_detail)


def _status_token(status) -> str:
    """Normalize one typed lifecycle status to the capsule's cause spelling."""
    if not isinstance(status, str):
        return ""
    return _STATUS_TOKEN_RE.sub("-", status.strip().upper()).strip("-")


def runtime_parked_status_vocabulary() -> frozenset[str]:
    """Return normalized task and pending-completion status literals.

    The registry vocabulary is read at call time so new task states stay loud.
    Coordination is a queue-only status, so it is owned explicitly here rather
    than pretending it is a registry task state.
    """
    registry_statuses = frozenset(
        token
        for status in getattr(registry_state, "KNOWN_STATUSES", ())
        if (token := _status_token(status))
    )
    return registry_statuses | QUEUE_ONLY_STATUSES


def parked_cause(status) -> str | None:
    """Map a status signal in the current vocabulary to an owed-work cause.

    Required cause tokens map by normalized identity. Legacy ``needs_review``
    remains review debt; typed coordination rows map to their own cause.
    Statuses absent from the runtime vocabulary are not guessed from summary
    words; callers surface them under UNCLASSIFIED-CAUSE instead.
    """
    token = _status_token(status)
    vocabulary = runtime_parked_status_vocabulary()
    if token not in vocabulary:
        return None
    if token in PARKED_CAUSES:
        return token
    if token == _LEGACY_NEEDS_REVIEW:
        return REVIEW_REQUIRED
    return None


def pending_completion_groups(pending):
    """Group counted ``((namespace, status), count)`` rows by typed cause.

    Unknown/non-parked queue statuses remain itemised under a loud residual
    group. The capsule therefore never trades flat visibility for silent cause
    loss while the producer vocabulary evolves.
    """
    buckets = {cause: [] for cause in PARKED_CAUSES}
    unclassified = []
    for (namespace, status), count in pending:
        cause = parked_cause(status)
        if cause is None:
            unclassified.append(((namespace, status), count))
        else:
            buckets[cause].append(((namespace, status), count))
    grouped = [(cause, buckets[cause]) for cause in PARKED_CAUSES if buckets[cause]]
    if unclassified:
        grouped.append((UNCLASSIFIED_CAUSE, unclassified))
    return grouped


def pending_completions(path=None):
    """Group `_state/chrono-queue.md` lines by `(namespace, status)`, counted.

    chrono/CLAUDE.md step 7 tells Chrono to read this file directly, but nothing
    ever did — 3,722 lines, zero handled (`shared/protocol.md:339`: "with no
    later reader it is durable storage, not a notification"). The capsule is the
    only path Chrono actually reads at session start, so this makes the queue
    reachable from there.

    Individual lines are never emitted here: the live queue can hold hundreds of
    open entries, which would blow the capsule's `max_tokens` budget outright.
    Grouped counts are the bounded projection; `_render` still subjects this
    section to the token-bound cascade, since even the grouped form can grow
    past budget on a long-uncleared queue.

    Each line is `timestamp | status | namespace/task-id | summary`; only the
    status and the namespace (text before the first `/`) are used. `#`-prefixed
    and blank lines are skipped. A malformed line (fewer than 3 `|`-fields) is
    skipped rather than raising — one bad line must not blank the whole section.
    Rows whose status is already-handled (``AUTO-CLOSED`` / ``TASK-CLOSED`` /
    ``cancelled`` / ``complete`` / ``completed``) are skipped too: the queue is
    append-only and keeps every settled receipt, so without this the "awaiting a
    decision" section would count closed tasks as owed work.
    Never raises: a corrupt queue must not break capsule generation any more
    than a missing one does (`read_text` raises `UnicodeDecodeError`, a
    `ValueError` subclass, on invalid bytes; fix round 2, controller ruling: the
    watcher that writes these summaries has separately produced verbatim error
    captures containing raw ANSI escapes, so invalid-UTF-8 input is a real risk,
    not a hypothetical one).

    The two failure modes are NOT the same answer. `[]` means the queue is
    absent or holds nothing — nothing is owed. `None` means the queue is there
    and could not be read — the count is *unknown*, and `_render` says so on its
    own line rather than dropping the section. Sorted by count descending, then
    by `(namespace, status)` for a stable order.
    """
    dest = Path(path) if path else QUEUE_PATH
    try:
        text = dest.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Genuinely absent: a queue nobody has written owes nothing.
        return []
    except (OSError, ValueError):
        # Present but unreadable/undecodable. Returning [] here made a corrupt
        # queue render exactly like an empty one, so hundreds of parked
        # completions could vanish behind one bad byte. None means unknown and
        # is rendered as a loud line. Still never raises: capsule
        # generation must not depend on the queue being well-formed.
        return None
    counts = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = line.split("|", 3)
        if len(fields) < 3:
            continue
        status = fields[1].strip()
        if _status_token(status) in HANDLED_PENDING_STATUSES:
            # Already settled by the board; not owed work. See the docstring.
            continue
        namespace = fields[2].strip().split("/", 1)[0]
        key = (namespace, status)
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def active_thread_charters(path=None, now: datetime | None = None):
    """Load open charters from the active-path status rail, fail-soft.

    Deriving the default from ``CAPSULE_PATH`` keeps throwaway/test capsules
    isolated from the host's real charters when callers rebind that path.
    """
    source = (
        Path(path)
        if path
        else CAPSULE_PATH.parent / CHARTERS_REL.relative_to("_state/chrono")
    )
    try:
        return load_active_charters(source, now=now)
    except Exception as exc:  # noqa: BLE001 — resume must never blank the capsule
        # A read failure must be loud, not indistinguishable from "no active
        # threads". Represent it as one invalid projected charter so the
        # capsule still renders and points Chrono at the failed source.
        return [
            ThreadCharter(
                path=source / "unreadable-charter-rail.md",
                ask="",
                open_loops=(),
                done_when=(),
                done_when_met=False,
                unresolved_queues=(),
                stale_claims=(),
                issues=(f"active charter rail unreadable ({exc})",),
            )
        ]


def _needs_human_response_path(task_id):
    """Locate departments/<namespace>/outbox/<ID>-response.md, or None."""
    if not isinstance(task_id, str) or not _SAFE_TASK_ID_RE.fullmatch(task_id):
        return None
    if not DEPARTMENTS_ROOT.is_dir():
        return None
    for namespace in sorted(DEPARTMENTS_ROOT.iterdir()):
        candidate = namespace / "outbox" / f"{task_id}-response.md"
        if candidate.is_file():
            return candidate
    return None


def _has_decision_heading(body):
    """True when a heading outside any fenced code block names an operator decision."""
    fenced = False
    for line in body:
        text = line.lstrip()
        if text.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced and _DECISION_HEADING_RE.match(text):
            return True
    return False


def needs_human_ask(task_id):
    """The specialist's one-line ask for a needs_human task, or None.

    Returns ``(summary, decision_section_present)``: the first non-empty line
    after the response's YAML frontmatter, clipped, plus whether any heading
    names an operator decision. None on any failure (missing, unreadable, no
    frontmatter, empty body, oversized) so the caller renders the plain line.
    """
    try:
        path = _needs_human_response_path(task_id)
        if path is None or path.stat().st_size > NEEDS_HUMAN_RESPONSE_MAX_BYTES:
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        close = next(
            (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"),
            None,
        )
        if close is None:
            return None
        body = lines[close + 1 :]
        first = next((line for line in body if line.strip()), "")
        summary = clip(_SUMMARY_NOISE_RE.sub("", first), NEEDS_HUMAN_SUMMARY_CHARS)
        if not summary:
            return None
        return summary, _has_decision_heading(body)
    except Exception:
        return None


def _needs_human_line(task, enrich):
    action = task.get("next_action") or "operator decision"
    plain = f"- NEEDS HUMAN: {action} [{task['id']}]"
    ask = needs_human_ask(task.get("id")) if enrich else None
    if ask is None:
        return plain
    summary, decision = ask
    # specialist and to_model arrive from the registry unbounded; clipping the
    # lane keeps the summary cap the only thing that sets this line's length.
    lane = clip(
        f"{task.get('specialist') or '?'}/{task.get('to_model') or '?'}",
        NEEDS_HUMAN_LANE_CHARS,
    )
    note = f" | {DECISION_SECTION_NOTE}" if decision else ""
    # The summary is worker-authored prose landing in the coordinator's resume
    # context. Quote it and label it so it reads as data, never as an instruction
    # (finding A1, 2026-09-19 opsec review). Straight quotes inside it are folded
    # so the boundary of the quoted span stays unambiguous.
    quoted = summary.replace('"', "'")
    task_id = task["id"]
    return f'- NEEDS HUMAN: {action} ({lane}) untrusted: "{quoted}"{note} [{task_id}]'


def _thread_lines(
    charters: list[ThreadCharter],
    mode: int,
    needs_human: list[dict] | None = None,
) -> list[str]:
    """Render the bounded charter and urgent operator-attention projection.

    ``mode`` 2 is the useful projection, 1 is one line per thread, and 0 is a
    loud count-only fallback.  The global capsule bound can therefore compress
    charter detail without making active or queued work look absent. A
    ``needs_human`` task is stronger than ordinary deferred work, so its ID is
    pinned to this rail at every mode and cannot be crowded out by queue depth.
    """
    urgent = list(needs_human or ())
    queue_total = sum(len(charter.unresolved_queues) for charter in charters)
    lines = [
        _needs_human_line(task, enrich=index < MAX_ENRICHED_NEEDS_HUMAN)
        for index, task in enumerate(urgent)
    ]
    if mode <= 0:
        if charters:
            lines.append(
                f"- active={len(charters)}; open_QUEUE={queue_total} "
                "[THREAD-RAIL:bound]"
            )
        return lines

    shown = charters[:MAX_PROJECTED_CHARTERS]
    if mode == 1:
        for charter in shown:
            checked = sum(line.lower().startswith("- [x]") for line in charter.done_when)
            flags = [f"DONE-WHEN {checked}/{len(charter.done_when)}"]
            if charter.unresolved_queues:
                flags.append(f"{len(charter.unresolved_queues)} unresolved QUEUE")
            if charter.stale_claims:
                flags.append(f"{len(charter.stale_claims)} stale current claim(s)")
            if charter.issues:
                flags.append("INVALID FORMAT")
            suffix = f"; {'; '.join(flags)}" if flags else ""
            lines.append(
                f"- THE ASK: {clip(charter.ask or '(unreadable)', 150)}{suffix} "
                f"[THREAD-{charter.thread_id}]"
            )
    else:
        for charter in shown:
            source = f"[THREAD-{charter.thread_id}]"
            lines.append(f"- THE ASK: {clip(charter.ask or '(unreadable)', 240)} {source}")
            if charter.done_when:
                for criterion in charter.done_when[:MAX_PROJECTED_DONE_WHEN]:
                    lines.append(f"  - DONE-WHEN: {clip(criterion, 240)} {source}")
                dropped_done = len(charter.done_when) - MAX_PROJECTED_DONE_WHEN
                if dropped_done > 0:
                    lines.append(
                        f"  - +{dropped_done} DONE-WHEN criterion/criteria omitted "
                        f"from this bounded block {source}"
                    )
            else:
                lines.append(f"  - DONE-WHEN: (missing) {source}")
            for loop in charter.unresolved_queues[:MAX_PROJECTED_QUEUES]:
                lines.append(f"  - {clip(loop.raw[2:], 500)} {source}")
            dropped_queues = len(charter.unresolved_queues) - MAX_PROJECTED_QUEUES
            if dropped_queues > 0:
                lines.append(
                    f"  - +{dropped_queues} unresolved QUEUE item(s) omitted from "
                    f"this bounded block {source}"
                )
            if charter.stale_claims:
                oldest = charter.stale_claims[0].observed_at
                lines.append(
                    f"  - WARNING: {len(charter.stale_claims)} current claim(s) are "
                    f"stale; oldest observed_at={oldest} (>24h) — refresh before "
                    f"presenting as current {source}"
                )
            if charter.issues:
                lines.append(
                    f"  - WARNING: invalid charter format — "
                    f"{clip('; '.join(charter.issues), 240)} {source}"
                )

    dropped_charters = len(charters) - len(shown)
    if dropped_charters:
        lines.append(
            f"- +{dropped_charters} active charter(s) omitted from this bounded "
            f"block — read {CHARTERS_REL}/"
        )
    return lines


def _parse_decision_timestamp(value) -> datetime | None:
    """Parse a decisions-journal ``%Y-%m-%dT%H:%M:%SZ`` stamp as UTC, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def partition_decisions_by_age(decisions, now):
    """Split decisions into (recent, older_count) by ``ACTIVE_DECISION_MAX_AGE_DAYS``.

    A missing or unparseable timestamp counts as older: ``decisions.record``
    always writes an ISO-8601 Z stamp, so only a hand-edited legacy row lacks
    one, and those are stale by construction. Order is preserved for the recent
    rows; the rest collapse to a count the caller points at the journal.
    """
    cutoff = now - timedelta(days=ACTIVE_DECISION_MAX_AGE_DAYS)
    recent, older = [], 0
    for decision in decisions:
        stamp = _parse_decision_timestamp(decision.get("timestamp"))
        if stamp is not None and stamp >= cutoff:
            recent.append(decision)
        else:
            older += 1
    return recent, older


def _render(view, max_tokens=3000, now=None):
    """Build a token-bounded capsule from an already-classified registry view.

    Under pressure the archived-debt and open-work blocks collapse to one-line
    declared omissions, then live task lines are trimmed, then the
    pending-completions section collapses to a
    one-line declared omission, then deferred lines are trimmed, and only then
    does the active-thread block compress from full to summary to a loud count.
    The charter rail therefore outlives every other droppable section.

    Open work collapses in the POINTER tier, alongside archived debt and ahead
    of live tasks, for the reason stated on that block: its one-line form keeps
    the whole signal (how much is owed, and the one file that holds it), while a
    dropped live or deferred line loses that task's next action outright. That
    is a placement argument about recoverability, not about importance —
    `_state/chrono/OPEN-WORK.md` is the canonical owed list and its collapsed
    line names it, so nothing is more than one read away. Live work re-surfaces
    through board sweeps, but pending-completions groups outrank it: nothing else
    in the capsule surfaces `_state/chrono-queue.md` at all, so a handful of
    live-task lines are traded away before the section collapses (controller
    ruling, fix round 1: measured on the real capsule, live tasks alone were
    ~75% of the 3000-token budget while the grouped pending section needed
    only ~116 tokens — dropping pending first made it permanently invisible in
    practice, which is worse than trimming a few recoverable live lines).
    Collapsing pending never goes silent (fix round 2): like live and
    deferred, it always declares what it dropped, because a missing heading
    is indistinguishable from "the queue is empty" — the exact ambiguity this
    section exists to kill. Deferred work still bites last: it surfaces
    nowhere but here. At the real budget nothing drops and every section shows.
    Active decisions, the omission/unclassified declarations, and the
    active-thread heading/count are never dropped.

    The capsule takes no operator turn. The ``## Latest operator instruction``
    section has no live producer, so rendering a stored turn would present
    stale instructions as current. The turn plumbing is removed; only the
    two public signatures (``render_capsule`` and
    ``write_capsule``) still accept an ignored ``latest_operator_turn``, because
    callers outside the removal's write scope pass it positionally or by
    keyword: ``tests/test_workboard_histories.py``,
    ``tests/test_lifecycle_observability.py`` and
    ``tests/test_fail_open_markers.py``. Delete the parameter from both once
    those call sites stop passing it.
    """
    now = now or datetime.now(timezone.utc)
    decs = active_decisions()
    harness_decs = [d for d in decs if decision_home(d) == "harness"]
    recent_harness_decs, older_harness_dec_count = partition_decisions_by_age(
        harness_decs, now
    )
    product_count = sum(decision_home(d) == "product" for d in decs)
    diagnostics = getattr(decs, "diagnostics", ())
    diagnostic_lines = []
    normalizations = {}
    for diagnostic in diagnostics:
        if diagnostic.kind == "legacy_normalization":
            key = diagnostic.decision_id or diagnostic.line
            normalizations.setdefault(key, []).append(diagnostic)
            continue
        source = f" [{diagnostic.decision_id}]" if diagnostic.decision_id else ""
        diagnostic_lines.append(
            f"- WARNING DECISION {diagnostic.kind.upper()}: "
            f"line {diagnostic.line} {diagnostic.field}: {diagnostic.message}{source}"
        )
    for group in normalizations.values():
        source = f" [{group[0].decision_id}]" if group[0].decision_id else ""
        fields = sorted({d.field for d in group if d.field != "status"})
        details = [f"{', '.join(fields)} converted to lists"] if fields else []
        if any(d.field == "status" for d in group):
            details.append("legacy active retained; confirmation not inferred")
        locations = ",".join(str(n) for n in sorted({d.line for d in group}))
        diagnostic_lines.append(
            f"- NOTICE DECISION LEGACY_NORMALIZATION: lines {locations}: "
            f"{'; '.join(details)}{source}"
        )
    # Retain loud ownership handling for callers supplying the legacy plain-list view.
    unknown_reported = {d.decision_id for d in diagnostics if d.kind == "unknown_home"}
    diagnostic_lines += [
        f"- WARNING DECISION UNKNOWN_HOME: ownership unknown [{d['decision_id']}]"
        for d in decs
        if decision_home(d) == "unknown" and d["decision_id"] not in unknown_reported
    ]
    tasks = view["live"]
    needs_human = [
        task for task in view["deferred"] if task.get("state") == "needs_human"
    ]
    deferred = [
        task for task in view["deferred"] if task.get("state") != "needs_human"
    ]
    pending = pending_completions()
    # None means the queue could not be read, which is not the same fact as an
    # empty queue and must not share its (silent) rendering.
    queue_unreadable = pending is None
    if queue_unreadable:
        pending = []
    pending_groups = pending_completion_groups(pending)
    pending_cause_summary = ", ".join(
        f"{cause}={sum(count for _, count in rows)}"
        for cause, rows in pending_groups
    )
    charters = active_thread_charters()
    # A failed scan is one line and must survive the token-bound cascade
    # intact: collapsing it into the "N still owed" count line would state a
    # charter count nobody measured. The flag comes back from the scan itself,
    # never from re-reading the row's wording.
    debt_rows, debt_scan_failed = _archived_debt_rows()
    open_items = open_work_items()

    def build(
        shown_live,
        shown_deferred,
        show_pending,
        show_debt,
        show_open_work,
        thread_mode,
    ):
        lines = ["# Chrono resume capsule", "", "## Active decisions"]
        if recent_harness_decs:
            lines += [
                f"- {d['statement']} [{d['decision_id']}]"
                for d in recent_harness_decs
            ]
        elif older_harness_dec_count:
            lines += [f"- (none in the last {ACTIVE_DECISION_MAX_AGE_DAYS} days)"]
        else:
            lines += ["- (none)"]
        if older_harness_dec_count:
            lines += [
                f"- (+{older_harness_dec_count} harness decision(s) older than "
                f"{ACTIVE_DECISION_MAX_AGE_DAYS} days, omitted; see "
                "_state/chrono/decisions.jsonl)"
            ]
        if product_count:
            lines += [
                f"- {product_count} active product decision(s) excluded from the harness list; "
                "see _state/chrono/decisions.jsonl (product home)."
            ]
        if diagnostic_lines:
            lines += ["", "## Decision diagnostics", *diagnostic_lines]
        if charters or needs_human:
            lines += ["", THREAD_HEADING]
            lines += _thread_lines(charters, thread_mode, needs_human)
        # Warn-only. Charters archived while still owed are invisible to every
        # other reader, because they all stop at active/ and archiving is a mv.
        # Collapses before live tasks are trimmed: this block is a POINTER, so
        # its one-line form keeps the whole signal (how many, where to look),
        # while a dropped live line loses that task's next action outright.
        # Never silent -- same rule as pending: an absent heading would read as
        # "nothing is owed", which is the ambiguity the block exists to kill.
        if debt_rows:
            lines += ["", ARCHIVED_DEBT_HEADING]
            if debt_scan_failed:
                lines += debt_rows
            elif show_debt:
                lines += [
                    "Closed while still carrying unfinished business. Resolve "
                    "each into `_state/chrono/OPEN-WORK.md` or finish it.",
                ] + debt_rows
            else:
                owed_count = _archived_debt_count(fallback=len(debt_rows))
                lines += [
                    f"- ({owed_count} archived charter(s) still owed, "
                    "omitted for the token bound — regenerate at a higher "
                    "budget or read _state/chrono/thread-charters/complete)"
                ]
        # The single list of everything raised and not yet done. Documented as
        # such and instructed to be read at session start, yet projected
        # nowhere until now — so owed work was absent from the one artifact a
        # session is guaranteed to regenerate and read.
        if open_items:
            lines += ["", OPEN_WORK_HEADING]
            lines += _open_work_lines(open_items, show_open_work)
        lines += ["", TASKS_HEADING]
        lines += [
            f"- {t['state']}: {t.get('next_action', '?')} [{t['id']}]"
            for t in shown_live
        ] or ["- (none)"]
        # An omission the capsule does not declare is indistinguishable from a task
        # that closed — the exact ambiguity that let this file rot unnoticed.
        dropped = len(tasks) - len(shown_live)
        if dropped:
            lines += [f"- (+{dropped} more live, omitted for the token bound)"]
        if deferred:
            # A count line is not a task line (0230 review): a resuming Chrono
            # must be able to act from the capsule alone, so deferred work is
            # itemised with its ID and next action.
            lines += ["", DEFERRED_HEADING]
            lines += [
                f"- {t['state']}: {t.get('next_action', '?')} [{t['id']}]"
                for t in shown_deferred
            ]
            dropped_deferred = len(deferred) - len(shown_deferred)
            if dropped_deferred:
                lines += [
                    f"- (+{dropped_deferred} more deferred, omitted for the token "
                    "bound — query _state/active-tasks.json by status)"
                ]
        if queue_unreadable:
            # The one case the section must NOT stay quiet about: the queue is
            # there and unread. An absent heading here would say "the queue is
            # empty" about a file nobody could open.
            lines += ["", QUEUE_HEADING, QUEUE_UNREADABLE_LINE]
        elif pending:
            # Silent when dropped is the exact ambiguity this section exists to
            # kill (fix round 2): a missing heading must never be mistaken for
            # "the queue is empty" the way an omitted live/deferred task must
            # never read as "closed". Cause grouping also survives collapse:
            # the compact line retains per-cause completion counts.
            lines += ["", QUEUE_HEADING]
            if show_pending:
                for cause, rows in pending_groups:
                    lines += [f"### {cause}"]
                    lines += [
                        f"- {count} x {namespace} | {status}"
                        for (namespace, status), count in rows
                    ]
            else:
                lines += [
                    f"- ({len(pending)} group(s) omitted for the token bound — "
                    f"{pending_cause_summary}; read "
                    "_state/chrono-queue.md)"
                ]
        for status, n in sorted(view["unclassified"].items()):
            lines += [
                f"- UNCLASSIFIED STATUS {status!r}: {n} task(s) invisible to this "
                "capsule — add it to chrono_state/registry.py KNOWN_STATUSES"
            ]
        # Omit `## Latest operator instruction`: without a live producer,
        # a stored turn could present stale instructions as current.
        return "\n".join(lines)

    shown_live, shown_deferred = list(tasks), list(deferred)
    show_pending = True
    show_debt = True
    show_open_work = True
    thread_mode = 2
    cap = build(
        shown_live,
        shown_deferred,
        show_pending,
        show_debt,
        show_open_work,
        thread_mode,
    )
    # hard token bound (~4 chars/token): collapse
    # the two pointer blocks (archived debt, then open work) to declared
    # omissions, then trim live task lines, then collapse the
    # pending-completions section to a declared omission, then trim deferred,
    # then compress active-thread detail; rebuild and re-check. Live is
    # trimmed BEFORE pending collapses — see the priority note above — so
    # pending only collapses once trimming live has failed to free enough
    # room on its own.
    while len(cap) // 4 > max_tokens and (
        show_debt
        or show_open_work
        or shown_live
        or show_pending
        or shown_deferred
        or (charters and thread_mode > 0)
    ):
        if show_debt:
            show_debt = False
        elif show_open_work:
            show_open_work = False
        elif shown_live:
            shown_live = shown_live[:-1]
        elif show_pending:
            show_pending = False
        elif shown_deferred:
            shown_deferred = shown_deferred[:-1]
        else:
            thread_mode -= 1
        cap = build(
            shown_live,
            shown_deferred,
            show_pending,
            show_debt,
            show_open_work,
            thread_mode,
        )
    return cap


def render_capsule(session_id, latest_operator_turn=None, max_tokens=3000, now=None):
    """Render a fresh capsule from the live registry (see `_render`).

    ``latest_operator_turn`` is ignored; see ``_render`` for why the slot remains.
    """
    return _render(
        registry_view(),
        max_tokens=max_tokens,
        now=now,
    )


def write_capsule(session_id, latest_operator_turn=None, max_tokens=3000, path=None, now=None):
    """Atomically regenerate the capsule on disk (temp + fsync + rename). Returns path.

    The capsule is derived purely from the journals and the registry; it carries
    no operator line, so there is nothing for a refresh to preserve
    (``latest_operator_turn`` is ignored; see ``_render`` for why the slot
    remains). An unclassified board status is loud twice: named in the capsule
    itself (via `_render`) and warned on stderr here, so the write path can never
    silently drop owed state again.
    """
    dest = Path(path) if path else CAPSULE_PATH
    view = registry_view()
    if view["unclassified"]:
        print(
            "WARNING: resume capsule: unclassified board statuses "
            f"{sorted(view['unclassified'])} — add them to "
            "chrono_state/registry.py KNOWN_STATUSES; affected tasks are counted "
            "in the capsule but cannot be listed",
            file=sys.stderr,
        )
    body = (
        _render(
            view,
            max_tokens=max_tokens,
            now=now,
        )
        + "\n"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("w", dir=dest.parent, delete=False)
    try:
        tmp.write(body)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, dest)
    except BaseException:
        # A failed rename must not leave the named temp beside the capsule — and
        # cleanup must never mask the failure that got us here: guard the close
        # (closing a buffered file can itself raise), always attempt the unlink,
        # and re-raise the ORIGINAL exception.
        try:
            tmp.close()
        except Exception:
            pass
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    return dest
