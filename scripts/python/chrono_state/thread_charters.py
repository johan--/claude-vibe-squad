"""Markdown-first active-thread charters for Chrono's resume rail.

An active charter is one regular ``*.md`` file under
``_state/chrono/thread-charters/active``.  The path supplies the active status;
the file therefore needs exactly three fields and no frontmatter:

``THE ASK`` / ``OPEN LOOPS`` / ``DONE-WHEN``.

The parser is deliberately small and fail-soft.  The resume capsule can surface a
malformed charter without crashing, while the skill-wiring validator reports the
same issue informationally.  OPEN LOOPS is an append-only operator/Chrono
ledger: ``append_receipt`` is its only writer, it stamps the UTC time itself,
and no earlier line is ever edited.  This module also owns the one state
transition a charter has -- ``archive_charter``, active/ to complete/ or parked/.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import os
import re
import sys
import tempfile


CHARTERS_REL = Path("_state/chrono/thread-charters/active")
FIELD_ORDER = ("THE ASK", "OPEN LOOPS", "DONE-WHEN")
STALE_AFTER = timedelta(hours=24)
EMPTY_LOOPS_PLACEHOLDER = "- (none)"
# Receipt kinds a writer may append. DECLINE is read-only (see below); DECISION
# is a CLI spelling that writes a FOLD receipt plus one decisions.jsonl line.
DECISION_KIND = "DECISION"

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^- \[(?P<mark>[ xX])\]\s+\S")
_QUEUE_ID = r"Q-[A-Za-z0-9][A-Za-z0-9._-]*"
_QUEUE_CLASS_RE = re.compile(rf"^QUEUE\s+(?P<id>{_QUEUE_ID})$")
# DECLINE is a deprecated read-only alias solely so archived charters keep
# parsing. New lines must use DROP (TASK-2099-01-01-0003-example-charter-update).
_RESOLUTION_CLASS_RE = re.compile(
    rf"^(?P<kind>FOLD|DROP|DECLINE)\s+resolves\s+(?P<id>{_QUEUE_ID})$"
)
_TERMINAL_CLASS_RE = re.compile(r"^(FOLD|DROP|DECLINE)$")
_LOOP_RE = re.compile(
    r"^- (?P<stamp>[^|]+?)\s*\|\s*(?P<classification>[^|]+?)\s*\|\s*"
    r"(?P<request>.+?)\s+—\s+why:\s*(?P<why>.+?);\s*resume:\s*(?P<resume>.+?)\s*$"
)
_OBSERVED_AT_RE = re.compile(
    r"\bobserved_at=(?P<stamp>\d{4}-\d{2}-\d{2}T"
    r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2}))"
)
_WRITABLE_KIND_RE = re.compile(
    rf"^(?:FOLD|DROP|QUEUE\s+{_QUEUE_ID}|(?:FOLD|DROP)\s+resolves\s+{_QUEUE_ID})$"
)
_LEADING_STAMP_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_LEADING_KIND_RE = re.compile(r"^\s*(?:FOLD|DROP|DECLINE|QUEUE|DECISION)\b[^|—]*\|")


class CharterAppendRefused(ValueError):
    """A receipt was not appended; the charter's bytes are unchanged."""


@dataclass(frozen=True)
class OpenLoop:
    """One physical OPEN LOOPS ledger line, with its parsed receipt fields."""

    raw: str
    classification: str
    queue_id: str | None = None
    resolves: str | None = None
    stamp: str = ""
    request: str = ""
    why: str = ""
    resume: str = ""


@dataclass(frozen=True)
class StaleClaim:
    """One ``observed_at`` stamp older than the capsule freshness window."""

    line_number: int
    observed_at: str


@dataclass(frozen=True)
class ThreadCharter:
    """Parsed projection of one active charter, including loud parse issues."""

    path: Path
    ask: str
    open_loops: tuple[OpenLoop, ...]
    done_when: tuple[str, ...]
    done_when_met: bool
    unresolved_queues: tuple[OpenLoop, ...]
    stale_claims: tuple[StaleClaim, ...]
    issues: tuple[str, ...]

    @property
    def thread_id(self) -> str:
        return self.path.stem


def _parse_iso8601(value: str) -> datetime | None:
    """Parse the bounded ISO-8601 spellings used by charter ledger lines."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _section(lines: list[str], heading: str) -> list[str]:
    """Return a heading's body up to the next level-two heading."""
    marker = f"## {heading}"
    try:
        start = lines.index(marker) + 1
    except ValueError:
        return []
    end = len(lines)
    for index in range(start, len(lines)):
        if _HEADING_RE.match(lines[index]):
            end = index
            break
    return lines[start:end]


def _one_line(lines: list[str]) -> str:
    """Collapse a prose field for the bounded capsule projection."""
    return " ".join(line.strip() for line in lines if line.strip())


def parse_charter(path: Path, now: datetime | None = None) -> ThreadCharter:
    """Parse one charter without raising for malformed or unreadable state."""
    issues: list[str] = []
    if path.is_symlink() or not path.is_file():
        return ThreadCharter(
            path=path,
            ask="",
            open_loops=(),
            done_when=(),
            done_when_met=False,
            unresolved_queues=(),
            stale_claims=(),
            issues=("charter must be a regular file, not a symlink or special entry",),
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return ThreadCharter(
            path=path,
            ask="",
            open_loops=(),
            done_when=(),
            done_when_met=False,
            unresolved_queues=(),
            stale_claims=(),
            issues=(f"charter is unreadable UTF-8 ({exc})",),
        )
    return _parse_charter_text(path, text, now=now)


def _parse_charter_text(path: Path, text: str, now: datetime | None = None) -> ThreadCharter:
    """Parse already-read charter text so a writer can validate before writing."""
    issues: list[str] = []
    lines = text.splitlines()
    headings = [m.group(1) for line in lines if (m := _HEADING_RE.match(line))]
    if tuple(headings) != FIELD_ORDER:
        issues.append(
            "level-two headings must be exactly, once each, and in order: "
            + " / ".join(FIELD_ORDER)
        )

    ask_lines = _section(lines, "THE ASK")
    loop_lines = _section(lines, "OPEN LOOPS")
    done_lines = [line.strip() for line in _section(lines, "DONE-WHEN") if line.strip()]
    ask = _one_line(ask_lines)
    if not ask:
        issues.append("THE ASK is empty")

    if not done_lines:
        issues.append("DONE-WHEN is empty; use one or more checkbox lines")
    checkbox_marks: list[bool] = []
    for line in done_lines:
        match = _CHECKBOX_RE.match(line)
        if not match:
            issues.append(f"DONE-WHEN entry is not a checkbox line: {line}")
            continue
        checkbox_marks.append(match.group("mark").lower() == "x")
    done_when_met = bool(done_lines) and len(checkbox_marks) == len(done_lines) and all(
        checkbox_marks
    )

    parsed_loops: list[OpenLoop] = []
    queue_by_id: dict[str, OpenLoop] = {}
    resolved: set[str] = set()
    nonblank_loops = [line.strip() for line in loop_lines if line.strip()]
    if nonblank_loops == ["- (none)"]:
        nonblank_loops = []
    for line in nonblank_loops:
        match = _LOOP_RE.match(line)
        if not match:
            issues.append(
                "OPEN LOOPS entry must be one line shaped "
                "'<ISO> | FOLD|QUEUE Q-id|DROP [resolves Q-id] | request "
                "— why: ...; resume: ...': "
                + line
            )
            continue
        stamp = match.group("stamp").strip()
        if _parse_iso8601(stamp) is None:
            issues.append(f"OPEN LOOPS entry has a non-ISO timestamp: {stamp}")
        classification = match.group("classification").strip()
        receipt = {
            "stamp": stamp,
            "request": match.group("request").strip(),
            "why": match.group("why").strip(),
            "resume": match.group("resume").strip(),
        }
        queue_match = _QUEUE_CLASS_RE.match(classification)
        resolution_match = _RESOLUTION_CLASS_RE.match(classification)
        if queue_match:
            queue_id = queue_match.group("id")
            loop = OpenLoop(line, "QUEUE", queue_id=queue_id, **receipt)
            if queue_id in queue_by_id:
                issues.append(f"OPEN LOOPS reuses queue id {queue_id}")
            else:
                queue_by_id[queue_id] = loop
        elif resolution_match:
            queue_id = resolution_match.group("id")
            loop = OpenLoop(
                line,
                resolution_match.group("kind"),
                resolves=queue_id,
                **receipt,
            )
            if queue_id not in queue_by_id:
                issues.append(
                    f"OPEN LOOPS resolves {queue_id} before that queue id is recorded"
                )
            resolved.add(queue_id)
        elif _TERMINAL_CLASS_RE.match(classification):
            loop = OpenLoop(line, classification, **receipt)
        else:
            issues.append(f"OPEN LOOPS has invalid classification: {classification}")
            continue
        parsed_loops.append(loop)

    unresolved = tuple(
        loop for queue_id, loop in queue_by_id.items() if queue_id not in resolved
    )

    reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stale: list[StaleClaim] = []
    for line_number, line in enumerate(lines, start=1):
        for match in _OBSERVED_AT_RE.finditer(line):
            value = match.group("stamp")
            observed = _parse_iso8601(value)
            if observed is None:
                issues.append(f"line {line_number} has invalid observed_at={value}")
            elif reference_now - observed > STALE_AFTER:
                stale.append(StaleClaim(line_number, value))
    stale.sort(
        key=lambda claim: _parse_iso8601(claim.observed_at) or reference_now
    )

    return ThreadCharter(
        path=path,
        ask=ask,
        open_loops=tuple(parsed_loops),
        done_when=tuple(done_lines),
        done_when_met=done_when_met,
        unresolved_queues=unresolved,
        stale_claims=tuple(stale),
        issues=tuple(issues),
    )


def load_active_charters(path: Path, now: datetime | None = None) -> list[ThreadCharter]:
    """Load active Markdown charters in stable name order.

    Absence means none: a rail that was never created owes nothing, so a
    missing directory is an empty list. A rail that exists but cannot be listed
    means *unknown*, and raises. Both callers already wrap this in a loud
    handler that names the failed source (``resume.active_thread_charters``
    projects a marker charter; ``validate_skill_wiring.thread_charter_reports``
    reports it) -- the swallow here made both of them dead code and turned an
    unreadable rail into a confident "no active threads".

    ``iterdir`` rather than ``glob`` on purpose: pathlib's globbing suppresses
    the underlying ``scandir`` error, so an unreadable rail read as empty even
    before the handler was reached. The name filter matches what ``*.md`` did,
    dotfiles included.
    """
    try:
        entries = sorted(entry for entry in path.iterdir() if entry.suffix == ".md")
    except FileNotFoundError:
        return []
    return [parse_charter(entry, now=now) for entry in entries]


COMPLETE_DIRNAME = "complete"
PARKED_DIRNAME = "parked"

ARCHIVED_RELS = (
    Path("_state/chrono/thread-charters") / COMPLETE_DIRNAME,
    Path("_state/chrono/thread-charters") / PARKED_DIRNAME,
)


class CharterArchiveRefused(RuntimeError):
    """A charter's own DONE-WHEN contradicts the completion claim being made."""

    def __init__(self, path: Path, reasons: tuple[str, ...]) -> None:
        self.path = path
        self.reasons = reasons
        super().__init__(
            f"{path.name} cannot be archived as {COMPLETE_DIRNAME}: "
            + "; ".join(reasons)
        )


def unfinished_business(charter: ThreadCharter) -> tuple[str, ...]:
    """Why this charter is not finished, in stable order; empty means it is.

    One home for that question.  The archive transition below and the archived-
    debt reporter must never disagree about whether a charter is done, so both
    read this instead of re-deriving it.
    """
    reasons: list[str] = []
    if not charter.done_when_met:
        reasons.append("DONE-WHEN is unticked, empty, or unparsable")
    reasons.extend(f"unresolved {loop.queue_id}" for loop in charter.unresolved_queues)
    return tuple(reasons)


def archive_charter(
    path: Path, destination: Path, now: datetime | None = None
) -> Path:
    """Move one charter out of ``active/``, refusing a false completion claim.

    This is the transition itself, not a check standing beside it.  Archiving
    used to be a bare ``mv``, so ``done_when_met`` was computed on every parse
    and consulted by nobody at the single moment it decides something -- a
    charter was fully visible right up until the move and invisible after it.

    An unfinished charter is not blocked from being archived: it belongs in
    ``parked/``, which is what that directory already means.  Only the
    *completion claim* is refused, so closing stays possible and stops being
    automatic.  An unrecognised destination fails closed.
    """
    path = Path(path)
    destination = Path(destination)
    if destination.name not in (COMPLETE_DIRNAME, PARKED_DIRNAME):
        raise ValueError(
            f"charter destination must be {COMPLETE_DIRNAME}/ or "
            f"{PARKED_DIRNAME}/, got {destination.name!r}"
        )
    if destination.name == COMPLETE_DIRNAME:
        reasons = unfinished_business(parse_charter(path, now=now))
        if reasons:
            raise CharterArchiveRefused(path, reasons)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / path.name
    os.replace(path, target)
    return target


def load_archived_debt(root: Path, now: datetime | None = None) -> list[ThreadCharter]:
    """Charters that were archived while still carrying unfinished business.

    The active scan above stops at ``active/``, and archiving is a bare ``mv``
    into ``complete/`` with no code path in between -- so an unresolved QUEUE or
    an unticked DONE-WHEN is fully visible right up until the move, and invisible
    to every reader immediately after. Measured 2026-08-23: six queued items sat
    unread inside ``complete/``, one of them archived the same evening it was
    filed, minutes after the operator was told it had been captured.

    Warn-only by construction: this returns what to say, never a verdict, and no
    caller may block on it.
    """
    found: list[ThreadCharter] = []
    for rel in ARCHIVED_RELS:
        directory = root / rel
        # `iterdir`, not `glob`, for the same reason as `load_active_charters`:
        # pathlib's globbing suppresses the underlying `scandir` error, so an
        # unreadable rail read as empty and the `except OSError` below it was
        # dead code. `_archived_debt_rows` only builds its marker when this
        # RAISES, so the swallow made the likeliest real failure invisible and
        # rendered an unreadable archive identically to a clean one.
        try:
            entries = sorted(e for e in directory.iterdir() if e.suffix == ".md")
        except FileNotFoundError:
            continue
        for entry in entries:
            charter = parse_charter(entry, now=now)
            if unfinished_business(charter):
                found.append(charter)
    return found


def clip(text: str, limit: int) -> str:
    """Bound one projected field without hiding that truncation occurred."""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 2)].rstrip() + " …"


def last_resume_point(charter: ThreadCharter) -> OpenLoop | None:
    """The newest parsed receipt, whose ``resume:`` clause is the thread's return point.

    The focus anchor reads this instead of a workboard ``next_action`` so the
    resume point has one home: the receipt Chrono wrote when it last touched
    the thread. ``None`` when the charter has no parseable receipt.
    """
    for loop in reversed(charter.open_loops):
        if loop.resume:
            return loop
    return None


def _utcnow() -> str:
    """UTC ``date -u`` as ISO-8601; the only clock receipts may carry."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_text(path: Path, text: str) -> None:
    """Publish text as same-directory temp + fsync + rename, keeping the mode."""
    mode = path.stat().st_mode & 0o777
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.tmp.", delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _added_issues(before: tuple[str, ...], after: tuple[str, ...]) -> tuple[str, ...]:
    remaining = Counter(before)
    added: list[str] = []
    for issue in after:
        if remaining[issue]:
            remaining[issue] -= 1
        else:
            added.append(issue)
    return tuple(added)


def append_receipt(charter: Path | str, kind: str, text: str) -> OpenLoop:
    """Append one OPEN LOOPS receipt, stamped by this clock, never by the caller.

    ``kind`` is ``FOLD``, ``DROP``, ``QUEUE Q-id``, ``FOLD resolves Q-id`` or
    ``DROP resolves Q-id``. ``text`` is the receipt body
    ``<request> — why: <why>; resume: <exact return point>`` and must not start
    with a timestamp or a classification: caller-supplied stamps can drift
    from the registry clock or misorder receipts. The line is validated by
    the same parser that reads it, and the write is refused if
    it would add any parse issue (a reused queue id, a resolution of an
    unrecorded id, a malformed body). Returns the parsed receipt.
    """
    path = Path(charter)
    if path.is_symlink() or not path.is_file():
        raise CharterAppendRefused(f"charter must be an existing regular file: {path}")
    kind = " ".join(kind.split())
    if kind.startswith("DECLINE"):
        raise CharterAppendRefused("DECLINE is deprecated and read-only; write DROP")
    if not _WRITABLE_KIND_RE.match(kind):
        raise CharterAppendRefused(
            "kind must be FOLD, DROP, 'QUEUE Q-id', 'FOLD resolves Q-id' or "
            f"'DROP resolves Q-id'; got {kind!r}"
        )
    if "\n" in text or "\r" in text:
        raise CharterAppendRefused("a receipt is exactly one line")
    body = text.strip()
    if _LEADING_STAMP_RE.match(body):
        raise CharterAppendRefused(
            "receipts are stamped by append_receipt; do not supply a timestamp"
        )
    if _LEADING_KIND_RE.match(body):
        raise CharterAppendRefused(
            "pass the classification as `kind`, not inside the receipt text"
        )
    stamp = _utcnow()
    line = f"- {stamp} | {kind} | {body}"
    if not _LOOP_RE.match(line):
        raise CharterAppendRefused(
            "receipt text must read '<request> — why: <why>; resume: <exact return point>'"
        )

    current = path.read_text(encoding="utf-8")
    before = _parse_charter_text(path, current)
    lines = current.splitlines(keepends=True)
    heading = "## OPEN LOOPS"
    start = next(
        (index for index, raw in enumerate(lines) if raw.rstrip("\r\n") == heading),
        None,
    )
    if start is None:
        raise CharterAppendRefused(f"charter has no '{heading}' section: {path}")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _HEADING_RE.match(lines[index].rstrip("\r\n")):
            end = index
            break
    section = range(start + 1, end)
    nonblank = [index for index in section if lines[index].strip()]
    if len(nonblank) == 1 and lines[nonblank[0]].strip() == EMPTY_LOOPS_PLACEHOLDER:
        lines[nonblank[0]] = line + "\n"
    else:
        insert_at = nonblank[-1] + 1 if nonblank else start + 1
        if nonblank and not lines[nonblank[-1]].endswith("\n"):
            lines[nonblank[-1]] += "\n"
        lines.insert(insert_at, line + "\n")
    candidate = "".join(lines)
    after = _parse_charter_text(path, candidate)
    added = _added_issues(before.issues, after.issues)
    if added:
        raise CharterAppendRefused("receipt refused; it would add: " + "; ".join(added))
    appended = next((loop for loop in after.open_loops if loop.raw == line), None)
    if appended is None:
        raise CharterAppendRefused("receipt did not round-trip through the charter parser")
    _atomic_write_text(path, candidate)
    return appended


def resolve_charter(reference: str, root: Path | str | None = None) -> Path:
    """Turn a thread id or path into the active charter file it names.

    A bare id resolves under ``_state/chrono/thread-charters/active`` only: a
    parked or completed charter is not a place new receipts belong.
    """
    candidate = Path(reference)
    if candidate.suffix == ".md" and (candidate.is_absolute() or len(candidate.parts) > 1):
        if not candidate.is_file():
            raise FileNotFoundError(f"charter path does not exist: {candidate}")
        return candidate
    base = Path(root) if root is not None else Path(os.environ.get("VAULT_ROOT", "."))
    resolved = base / CHARTERS_REL / f"{candidate.stem}.md"
    if not resolved.is_file():
        raise FileNotFoundError(
            f"no active charter named {candidate.stem!r} under {base / CHARTERS_REL}"
        )
    return resolved


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append a stamped receipt to an active thread charter"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    append = subcommands.add_parser("append", help="append one OPEN LOOPS receipt")
    append.add_argument("charter", help="thread id (active/) or a charter path")
    append.add_argument(
        "kind",
        help=f"FOLD | DROP | 'QUEUE Q-id' | 'FOLD resolves Q-id' | 'DROP resolves Q-id' | "
        f"{DECISION_KIND} (FOLD receipt + one decisions.jsonl line)",
    )
    append.add_argument("text", help="'<request> — why: <why>; resume: <return point>'")
    append.add_argument("--root", type=Path, default=None, help="vault root for a bare id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    try:
        path = resolve_charter(args.charter, args.root)
        kind = " ".join(args.kind.split())
        record_decision = kind == DECISION_KIND
        receipt = append_receipt(path, "FOLD" if record_decision else kind, args.text)
        report: dict[str, object] = {
            "action": "append",
            "charter": str(path),
            "kind": receipt.classification,
            "stamp": receipt.stamp,
            "line": receipt.raw,
        }
    except (CharterAppendRefused, FileNotFoundError, ValueError) as exc:
        print(f"charter append ERROR: {exc}", file=sys.stderr)
        return 3
    if record_decision:
        # Imported here so a charter append never depends on the decision
        # journal being importable; the receipt is the primary record and has
        # already landed, so a journal failure is reported, not hidden.
        try:
            from chrono_state import decisions

            report["decision_id"] = decisions.record(
                receipt.request,
                "confirmed",
                [f"charter:{path.stem}:{receipt.stamp}"],
                scope=[path.stem],
                home="product",
            )
            report["decisions_file"] = str(decisions.DECISIONS_FILE)
        except Exception as exc:  # noqa: BLE001 — the receipt must still be reported
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            print(
                f"charter append ERROR: receipt appended but decisions.jsonl was not: {exc}",
                file=sys.stderr,
            )
            return 4
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
