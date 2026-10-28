"""Append-only Chrono decision-authority record.

Confirmed operator decisions are current AUTHORITY — kept deliberately separate from
chrono-vault, which holds quoted, potentially-stale learning. (Conflating the two would
demote an authoritative decision to untrusted-evidence status: a precedence bug.) The
active view is a fold of an immutable event log; a change appends a superseding event
rather than rewriting history. Only an explicit operator statement is `confirmed`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DECISIONS_FILE = (
    Path(os.environ.get("VAULT_ROOT", ".")) / "_state" / "chrono" / "decisions.jsonl"
)


# Historical ownership only: new events must carry home, never infer it from scope.
LEGACY_PRODUCT_DECISION_IDS = frozenset({
    "DEC-217af2dae9d9", "DEC-4ba93d297fac", "DEC-95eaeb0d7670",
    "DEC-79a9dc0fc097", "DEC-357f6dc2dafa", "DEC-93d386c0b738",
    "DEC-8f77871fa933",
})
LEGACY_HARNESS_DECISION_IDS = frozenset({
    "DEC-a5b221307538", "DEC-8ce85ea8304a", "DEC-21a60b7392ea",
    "DEC-60800610769e", "DEC-c067ae7a544c", "DEC-faae54e75561",
    "DEC-88c1dc934732", "DEC-57d16676bb86", "DEC-685acde8c0c4",
    "DEC-075565dbed4b", "DEC-b04ede009bec",
    "DEC-2026-08-18-public-history-acceptance", "DEC-969626e6da15",
    "DEC-6d136c7c8bf5", "DEC-45bc970d8d20", "DEC-826bc1b70eab",
})
Home = Literal["harness", "product", "unknown"]
DiagnosticKind = Literal[
    "unknown_home", "unrecognized_status", "malformed_field",
    "legacy_normalization", "malformed_event", "unreadable_journal",
]


@dataclass(frozen=True)
class DecisionDiagnostic:
    kind: DiagnosticKind
    line: int
    decision_id: str | None
    field: str
    message: str


class DecisionFold(list[dict]):
    """Active events plus diagnostics, retaining the existing list interface.

    Validation diagnostics cover all journal events, including retired rows.
    Ownership warnings describe only the active fold, before home projection.
    """

    def __init__(self, events=(), diagnostics=()):
        super().__init__(events)
        self.diagnostics: tuple[DecisionDiagnostic, ...] = tuple(diagnostics)


def decision_home(event) -> Home:
    if "home" in event:
        home = event["home"]
        return home if home in ("harness", "product") else "unknown"
    if event.get("decision_id") in LEGACY_PRODUCT_DECISION_IDS:
        return "product"
    if event.get("decision_id") in LEGACY_HARNESS_DECISION_IDS:
        return "harness"
    return "unknown"


def record(statement, status, source_turn_ids, scope=None, supersedes=None, *, home="unknown"):
    """Append an immutable decision event; return its decision_id.

    status is one of {"proposed", "confirmed", "superseded"}. supersedes is a list of
    prior decision_ids this event retires. Omitted ownership is explicitly unknown,
    so old callers cannot silently record a product decision as harness authority.
    """
    if home not in ("harness", "product", "unknown"):
        raise ValueError("home must be harness, product, or unknown")
    if status not in ("proposed", "confirmed", "superseded"):
        raise ValueError("status must be proposed, confirmed, or superseded")
    for field, value in (("scope", scope), ("supersedes", supersedes)):
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise ValueError(f"{field} must be a list of nonempty strings")
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    did = "DEC-" + hashlib.sha256(
        (statement + ts + os.urandom(6).hex()).encode()
    ).hexdigest()[:12]
    event = {
        "decision_id": did,
        "status": status,
        "home": home,
        "statement": statement,
        "scope": scope or [],
        "source_turn_ids": source_turn_ids,
        "supersedes": supersedes or [],
        "timestamp": ts,
    }
    with open(DECISIONS_FILE, "a") as fh:
        fh.write(json.dumps(event) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return did


def active_decisions() -> DecisionFold:
    """Fold globally before projecting ownership; return active rows and diagnostics.

    Legacy `active` remains active without inventing confirmation. Legacy scalar
    scope/supersedes become singleton lists, never sequences of characters. Invalid
    fields are diagnosed; usable supersession edges still retire their targets.
    """
    diagnostics = []
    try:
        lines = DECISIONS_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return DecisionFold()
    except (OSError, UnicodeError) as exc:
        return DecisionFold(diagnostics=[DecisionDiagnostic(
            "unreadable_journal", 0, None, "journal", str(exc),
        )])
    events = []
    latest_lines = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        did = None

        def diagnose(kind, field, message):
            diagnostics.append(DecisionDiagnostic(kind, number, did, field, message))

        try:
            event = json.loads(line)
        except ValueError as exc:
            diagnose("malformed_event", "journal", str(exc))
            continue
        if not isinstance(event, dict):
            diagnose("malformed_event", "journal", "expected an event object")
            continue
        raw_id = event.get("decision_id")
        if isinstance(raw_id, str) and raw_id.strip():
            did = raw_id
            latest_lines[did] = number
        else:
            diagnose("malformed_field", "decision_id", "expected a nonempty string")
        for field in ("scope", "supersedes"):
            value = event.get(field, [])
            if isinstance(value, str):
                diagnose("legacy_normalization", field, "string normalized to a singleton list")
                value = [value]
            if not isinstance(value, list):
                diagnose("malformed_field", field, "expected a list of nonempty strings")
                value = []
            valid = [item for item in value if isinstance(item, str) and item.strip()]
            if len(valid) != len(value):
                diagnose("malformed_field", field, "invalid list entries ignored; valid entries retained")
            event[field] = valid
        status = event.get("status")
        if status == "active":
            diagnose("legacy_normalization", "status", "legacy active retained; confirmation not inferred")
        elif status not in ("confirmed", "proposed", "superseded"):
            diagnose("unrecognized_status", "status", repr(status))
        # Only a valid ID is eligible for the historical ownership allowlist.
        home = decision_home(event) if did else "unknown"
        if "home" in event and event["home"] not in ("harness", "product", "unknown"):
            diagnose("malformed_field", "home", "expected harness, product, or unknown")
        event["home"] = home
        statement = event.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            diagnose("malformed_field", "statement", "expected a nonempty string")
            event["statement"] = "(malformed statement; inspect the journal event)"
        events.append(event)

    superseded = {sid for e in events for sid in e.get("supersedes", [])}
    latest = {}
    for e in events:
        did = e.get("decision_id")
        if isinstance(did, str) and did.strip():
            latest[did] = e
    active = [e for did, e in latest.items()
              if did not in superseded and e.get("status") in ("confirmed", "proposed", "active")]
    for e in active:
        if e["home"] == "unknown":
            diagnostics.append(DecisionDiagnostic(
                "unknown_home", latest_lines[e["decision_id"]], e["decision_id"],
                "home", "ownership unknown; inspect the journal event",
            ))
    return DecisionFold(active, diagnostics)
