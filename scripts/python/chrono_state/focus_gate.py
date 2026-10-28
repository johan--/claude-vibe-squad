"""Turn-boundary focus decisions for the Chrono coordinator hooks.

The workboard remains the only authority.  This module deliberately separates
the pure decision from Claude Code's JSON hook protocol so invalid-focus
behaviour can be tested without installing a live hook.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any, Mapping

from registry_reconciler import CHRONO_NOTICE_PREFIX

from . import thread_charters, workboard


USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
STOP = "Stop"
SUPPORTED_EVENTS = frozenset({USER_PROMPT_SUBMIT, PRE_TOOL_USE, STOP})

READ_ONLY = "read-only"
REPAIR = "workboard-repair"
MUTATING = "mutation-or-dispatch"

_BOARD_WORKTREE_PARTS = ("_state", "board-worktrees")
_FOCUS_ISSUE_PREFIXES = (
    "structured workboard must project exactly one active item",
    "structured workboard must project exactly one literal next_action",
    "projected next_action must not be blank",
    "workboard cannot declare both idle and waiting",
    "declared waiting focus must identify exactly one open work_id",
)
_REPAIR_PROGRAM = (
    "from chrono_state import workboard; workboard.append_event('idle')"
)
_OUTPUT_STANDARD_REL = "docs/standards/operator-facing-output-standard.md"
_OUTPUT_STANDARD_PATH = Path(__file__).parents[3] / _OUTPUT_STANDARD_REL

_READ_ONLY_TOOLS = frozenset(
    {
        "Glob",
        "Grep",
        "LSP",
        "ListMcpResources",
        "Read",
        "ReadMcpResource",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TodoRead",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
    }
)
_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
_READ_ONLY_SHELL_COMMANDS = frozenset(
    {
        "basename",
        "cat",
        "cksum",
        "cut",
        "diff",
        "dirname",
        "du",
        "file",
        "grep",
        "head",
        "id",
        "ls",
        "md5",
        "pwd",
        "readlink",
        "rg",
        "sha256sum",
        "shasum",
        "sort",
        "stat",
        "tail",
        "test",
        "true",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    }
)
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "cat-file",
        "diff",
        "diff-tree",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "rev-list",
        "rev-parse",
        "show",
        "status",
    }
)
_READ_ONLY_MCP_PREFIXES = (
    "check",
    "describe",
    "fetch",
    "find",
    "get",
    "health",
    "inspect",
    "list",
    "lookup",
    "open",
    "query",
    "read",
    "recall",
    "resolve",
    "search",
    "show",
    "status",
    "validate",
    "view",
)
_MUTATING_MCP_WORDS = frozenset(
    {
        "add",
        "append",
        "apply",
        "archive",
        "cancel",
        "close",
        "commit",
        "create",
        "delete",
        "deploy",
        "dispatch",
        "edit",
        "execute",
        "install",
        "launch",
        "merge",
        "patch",
        "post",
        "publish",
        "put",
        "record",
        "remove",
        "run",
        "send",
        "set",
        "spawn",
        "stop",
        "update",
        "upload",
        "write",
    }
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)


def is_harness_notice(prompt: object) -> bool:
    """Recognize one entire emitted notice; unknown or mixed text keeps the anchor.

    These are the wire formats from registry_reconciler's compact_notice /
    notification_digest and squad-monitor.sh's send_alert callers. A prefix
    match would also swallow operator instructions appended to a notice.
    """
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    if not text or "\n" in text or "\r" in text:
        return False
    text = text.removeprefix(f"{CHRONO_NOTICE_PREFIX} ")
    task = r"TASK-[A-Za-z0-9][A-Za-z0-9._-]*"
    state = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
    namespace = r"[a-z][a-z0-9_-]{0,63}"
    details = r"; details: _state/chrono-queue\.md\."
    count = rf"{state}=[0-9]+"
    action = rf"(?:review-required|needs-review|needs-human|blocked|coordination-requested): {task}(?:, {task})*"
    lane = r"(?:codex|gpt-codex|claude|gemini|grok) lane \([^()\r\n]+\)"
    age = r"(?:[0-9]+|\?)"
    patterns = (
        rf"{state}: {task}{details}(?: Coordination requested; see queue\.)?",
        rf"BOARD DIGEST: {count}(?:, {count})*(?:; action: {action}(?:; {action})*)?"
        rf"{details}(?: Coordination requested in [0-9]+ notice\(s\); see queue\.)?",
        rf"MONITOR: task {task} → {lane} idle [0-9]+m, no response yet "
        rf"\(dispatched {age}m ago\); pending in {namespace}/inbox",
        rf"MONITOR: observed task {task} → {lane}: live board spawn attempt log idle [0-9]+s, "
        rf"no receipt \(threshold [0-9]+s; dispatched {age}m ago\); a long model turn or tool call may still be healthy",
        rf"MONITOR: [A-Za-z_-]+: {namespace}/{task}\.md parked [0-9]+m \([A-Za-z_-]+\); coordinator action overdue",
        rf"MONITOR: {namespace} namespace has stale active task \({task}\.md, [0-9]+m old\)",
        rf"MONITOR: {namespace} namespace received same task [0-9]+x in [0-9]+m - real thrash "
        rf"\((?:repeated task id {task}|duplicate body)\)",
        rf"MONITOR: NOT_MEASURED thrash {namespace}: skipped_task_ids=[0-9]+ "
        rf"measured_task_ids=[0-9]+ skipped_log_rows=[0-9]+",
    )
    return any(re.fullmatch(pattern, text) for pattern in patterns)


@dataclass(frozen=True)
class GateDecision:
    """One hook-independent focus decision."""

    allow: bool
    message: str = ""
    state: str = "valid"

    @property
    def fail_open(self) -> bool:
        return self.state == "fail-open"


def is_board_worktree_path(path: str | os.PathLike[str] | None) -> bool:
    """Return whether *path* is inside the board-worker worktree namespace."""

    if not path:
        return False
    normalized = str(path).replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part and part != ".")
    width = len(_BOARD_WORKTREE_PARTS)
    return any(
        parts[index : index + width] == _BOARD_WORKTREE_PARTS
        for index in range(len(parts) - width + 1)
    )


def _fail_open(message: str) -> GateDecision:
    return GateDecision(
        True,
        "FOCUS GATE WARNING — fail open: " + message,
        "fail-open",
    )


def _focus_issue(issue: str) -> bool:
    return issue.startswith(_FOCUS_ISSUE_PREFIXES)


def _validated_projection(
    ledger_path: Path | str | None,
    projection: workboard.WorkboardProjection | None,
) -> tuple[workboard.WorkboardProjection | None, tuple[str, ...], GateDecision | None]:
    """Load once and distinguish a proven focus violation from parser failure."""

    try:
        if projection is None:
            path = Path(ledger_path) if ledger_path is not None else workboard.WORKBOARD_PATH
            if path.is_symlink() or not path.is_file():
                return None, (), _fail_open(
                    f"workboard is not a readable regular file: {path}"
                )
            projection = workboard.load_workboard(path=path)

        # A malformed or semantically unprojectable document is not affirmative
        # evidence of invalid focus.  The hook is an attention rail, not a
        # security boundary, so uncertainty must not brick the coordinator.
        if projection.document.issues or projection.transition_issues:
            details = tuple(
                dict.fromkeys(
                    (*projection.document.issues, *projection.transition_issues)
                )
            )
            return projection, (), _fail_open(
                "workboard could not be trusted: " + "; ".join(details)
            )

        issues = workboard.validate_workboard(projection.document, projection)
        non_focus = tuple(issue for issue in issues if not _focus_issue(issue))
        if non_focus:
            return projection, issues, _fail_open(
                "canonical validator returned a non-focus error: "
                + "; ".join(non_focus)
            )
        return projection, issues, None
    except Exception as exc:  # fail-open contract is intentionally broad
        return None, (), _fail_open(
            f"{type(exc).__name__} while reading the workboard: {exc}"
        )


def _active_item(
    projection: workboard.WorkboardProjection,
) -> workboard.WorkItem | None:
    if projection.active_work_id is None:
        return None
    return next(
        (
            item
            for item in projection.items
            if item.work_id == projection.active_work_id and item.state == "active"
        ),
        None,
    )


def _operator_output_reminder() -> str:
    """Read the canonical excerpt at prompt time; keep no policy copy here."""

    standard = _OUTPUT_STANDARD_PATH.read_text(encoding="utf-8")
    start = "<!-- focus-gate-reminder:start -->"
    end = "<!-- focus-gate-reminder:end -->"
    if standard.count(start) != 1 or standard.count(end) != 1:
        raise ValueError("operator-output standard must contain one reminder excerpt")
    _, _, following = standard.partition(start)
    excerpt, closing, _ = following.partition(end)
    if not closing or not excerpt.strip():
        raise ValueError("operator-output reminder excerpt is empty or out of order")
    return f"OPERATOR OUTPUT ({_OUTPUT_STANDARD_REL}): " + " ".join(excerpt.split())


ANCHOR_CLASSIFY = (
    "Before any dispatch or mutation, classify the incoming message "
    "as FOLD / QUEUE / DROP relative to this active thread."
)
ANCHOR_MECHANISM = (
    "Before any packet or plan edit that adds a mechanism (a cap, key, "
    "approval, claim step, schedule, or safety layer), quote the operator "
    "sentence that asked for it in your message; if none exists, do not add it."
)


def _charters_dir(ledger_path: Path) -> Path:
    """The active-charter rail beside the ledger: ``_state/chrono/thread-charters/active``."""
    return ledger_path.parent / thread_charters.CHARTERS_REL.relative_to("_state/chrono")


def _charter_resume_point(ledger_path: Path) -> tuple[str, str]:
    """Return (NEXT ACTION text, its source) from the single active charter.

    The charter's newest receipt ``resume:`` clause is the one home for where
    the thread resumes; the workboard ``next_action`` is a pointer at the
    charter and is only shown when no single charter can answer. A copied
    workboard action can become stale as the charter's receipts advance, so
    the anchor reads the return point directly from the charter.
    """
    try:
        charters = thread_charters.load_active_charters(_charters_dir(ledger_path))
    except Exception as exc:  # noqa: BLE001 — the anchor degrades, never fails
        return "", f"workboard (active charter rail unreadable: {exc})"
    if len(charters) != 1:
        return "", f"workboard (active charters: {len(charters)}, need exactly 1)"
    charter = charters[0]
    receipt = thread_charters.last_resume_point(charter)
    if receipt is None:
        return "", f"workboard (charter {charter.thread_id} has no parseable receipt)"
    return receipt.resume, f"charter {charter.thread_id}, receipt {receipt.stamp}"


def _active_context(projection: workboard.WorkboardProjection) -> str:
    item = _active_item(projection)
    if item is None or projection.next_action is None:
        raise ValueError("valid active projection has no matching item or next_action")
    why = item.why or "<not recorded in the workboard why field>"
    resume, source = _charter_resume_point(Path(projection.document.path))
    next_action = resume or projection.next_action
    return "\n".join(
        (
            "TURN-BOUNDARY FOCUS ANCHOR",
            f"active_work_id: {projection.active_work_id}",
            f"active_item_id: {projection.active_item_id}",
            f"ACTIVE ITEM: {item.summary}",
            f"WHY: {why}",
            f"NEXT ACTION: {next_action}",
            f"NEXT ACTION SOURCE: {source}",
            ANCHOR_CLASSIFY,
            ANCHOR_MECHANISM,
        )
    )


def _pause_context(projection: workboard.WorkboardProjection) -> str:
    if projection.idle:
        return "TURN-BOUNDARY FOCUS ANCHOR\nfocus: declared idle (no commitment)"
    if projection.waiting_work_id is not None:
        item = next(
            (
                candidate
                for candidate in projection.items
                if candidate.work_id == projection.waiting_work_id
            ),
            None,
        )
        alias = item.alias if item is not None else projection.waiting_work_id
        action = item.resume_action if item is not None else None
        return "\n".join(
            (
                "TURN-BOUNDARY FOCUS ANCHOR",
                f"focus: declared waiting on {alias}",
                f"resume_action (literal): {action or '<not projected>'}",
            )
        )
    return ""


def _workboard_repair_command(
    projection: workboard.WorkboardProjection,
) -> str | None:
    """Render the narrow repair escape for the canonical ledger's repository."""

    ledger_path = projection.document.path.resolve()
    relative_parts = workboard.WORKBOARD_REL.parts
    if tuple(ledger_path.parts[-len(relative_parts) :]) != relative_parts:
        return None
    repo_root = ledger_path.parents[len(relative_parts) - 1]
    vault_root = shlex.quote(str(repo_root))
    python_path = shlex.quote(str(repo_root / "scripts" / "python"))
    interpreter = shlex.quote(sys.executable)
    program = shlex.quote(_REPAIR_PROGRAM)
    return (
        f"VAULT_ROOT={vault_root} PYTHONPATH={python_path} "
        f"{interpreter} -c {program}"
    )


def _invalid_message(
    projection: workboard.WorkboardProjection,
    issues: tuple[str, ...],
) -> str:
    next_action = projection.next_action
    if next_action is None:
        next_line = (
            "NEXT ACTION: <none projected; restore one active item or explicitly "
            "declare idle/waiting>"
        )
    else:
        next_line = f"NEXT ACTION: {next_action}"
    repair_command = _workboard_repair_command(projection)
    if repair_command is None:
        repair_line = (
            "REPAIR COMMAND: unavailable because the configured ledger is not "
            f"the canonical {workboard.WORKBOARD_REL} repository path"
        )
    else:
        repair_line = f"REPAIR COMMAND: {repair_command}"
    return "\n".join(
        (
            "FOCUS GATE BLOCK — canonical focus projection is invalid: "
            + "; ".join(issues),
            next_line,
            repair_line,
            "Run the literal canonical append command, then retry. Read-only "
            "inspection and that repair path remain allowed.",
        )
    )


def _simple_shell_words(command: object) -> list[str] | None:
    if not isinstance(command, str) or not command.strip() or "\n" in command or "\r" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        return None
    if any(token and set(token) <= set(";&|<>") for token in words):
        return None
    # Command substitution executes even when embedded in a larger argument.
    if any("$(" in token or "`" in token for token in words):
        return None
    return words


def _literal_arguments(call: ast.Call) -> bool:
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        return False
    if any(keyword.arg is None for keyword in call.keywords):
        return False
    # The escape hatch repairs the one canonical ledger, never an arbitrary
    # caller-selected path.
    if any(keyword.arg == "path" for keyword in call.keywords):
        return False
    try:
        for value in (*call.args, *(keyword.value for keyword in call.keywords)):
            ast.literal_eval(value)
    except (ValueError, TypeError):
        return False
    return True


def _looks_like_workboard_repair(tool_name: str, tool_input: Mapping[str, Any]) -> bool:
    if tool_name not in _SHELL_TOOLS:
        return False
    words = _simple_shell_words(tool_input.get("command"))
    if words is None:
        return False
    while words and _ASSIGNMENT.fullmatch(words[0]):
        words.pop(0)
    if not words or not re.fullmatch(r"python(?:3(?:\.\d+)*)?", Path(words[0]).name):
        return False
    try:
        command_index = words.index("-c")
    except ValueError:
        return False
    if command_index != len(words) - 2:
        return False
    if any(flag not in {"-B", "-E", "-I", "-s", "-S"} for flag in words[1:command_index]):
        return False
    try:
        program = ast.parse(words[command_index + 1], mode="exec")
    except SyntaxError:
        return False
    if len(program.body) != 2:
        return False
    imported, invoked = program.body
    if not (
        isinstance(imported, ast.ImportFrom)
        and imported.module in {"chrono_state", "scripts.python.chrono_state"}
        and len(imported.names) == 1
        and imported.names[0].name == "workboard"
        and imported.names[0].asname in {None, "workboard"}
    ):
        return False
    if not (
        isinstance(invoked, ast.Expr)
        and isinstance(invoked.value, ast.Call)
        and isinstance(invoked.value.func, ast.Attribute)
        and isinstance(invoked.value.func.value, ast.Name)
        and invoked.value.func.value.id == "workboard"
        and invoked.value.func.attr == "append_event"
    ):
        return False
    return _literal_arguments(invoked.value)


def _simple_shell_is_read_only(command: object) -> bool:
    words = _simple_shell_words(command)
    if words is None:
        return False
    while words and _ASSIGNMENT.fullmatch(words[0]):
        words.pop(0)
    if not words:
        return False
    executable = Path(words[0]).name
    arguments = words[1:]
    if executable == "git":
        writes_output = any(
            argument == "--output" or argument.startswith("--output=")
            for argument in arguments
        )
        return (
            bool(arguments)
            and arguments[0] in _READ_ONLY_GIT_SUBCOMMANDS
            and not writes_output
        )
    if executable == "find":
        return not any(
            argument
            in {
                "-delete",
                "-exec",
                "-execdir",
                "-fls",
                "-fprint",
                "-fprint0",
                "-fprintf",
                "-ok",
                "-okdir",
            }
            for argument in arguments
        )
    if executable == "sort" and any(
        argument == "-o" or argument.startswith("--output") for argument in arguments
    ):
        return False
    return executable in _READ_ONLY_SHELL_COMMANDS


def tool_disposition(tool_name: str, tool_input: Mapping[str, Any] | None = None) -> str:
    """Classify a tool conservatively; unknown tools are mutation/dispatch."""

    data = tool_input or {}
    if _looks_like_workboard_repair(tool_name, data):
        return REPAIR
    if tool_name in _READ_ONLY_TOOLS:
        return READ_ONLY
    if tool_name in _SHELL_TOOLS and _simple_shell_is_read_only(data.get("command")):
        return READ_ONLY
    if tool_name.startswith("mcp__sequential_thinking__"):
        return READ_ONLY
    if tool_name.startswith("mcp__"):
        operation = tool_name.rsplit("__", 1)[-1].lower()
        words = frozenset(part for part in operation.split("_") if part)
        if words & _MUTATING_MCP_WORDS:
            return MUTATING
        if any(
            operation == prefix or operation.startswith(prefix + "_")
            for prefix in _READ_ONLY_MCP_PREFIXES
        ):
            return READ_ONLY
    return MUTATING


def decide(
    hook_event: str,
    *,
    ledger_path: Path | str | None = None,
    projection: workboard.WorkboardProjection | None = None,
    tool_name: str = "",
    tool_input: Mapping[str, Any] | None = None,
    stop_hook_active: bool = False,
    prompt: object = None,
) -> GateDecision:
    """Return an allow/block decision without depending on hook plumbing."""

    if hook_event not in SUPPORTED_EVENTS:
        return _fail_open(f"unsupported hook event {hook_event!r}")

    if hook_event == USER_PROMPT_SUBMIT:
        try:
            if is_harness_notice(prompt):
                return GateDecision(True)
        except Exception:
            # This cost optimization must fail open to the original anchor.
            pass

    view, issues, failure = _validated_projection(ledger_path, projection)
    if failure is not None:
        return failure
    if view is None:  # defensive: _validated_projection always explains this
        return _fail_open("canonical validator returned no projection")

    if hook_event == USER_PROMPT_SUBMIT:
        if issues:
            return GateDecision(True, _invalid_message(view, issues), "invalid")
        try:
            context = _active_context(view) if view.active_work_id else _pause_context(view)
        except Exception as exc:
            return _fail_open(f"could not render the active focus: {exc}")
        try:
            context += "\n" + _operator_output_reminder()
        except Exception as exc:
            # An optional documentation excerpt must not discard the focus anchor.
            warning = _fail_open(
                "could not read the operator output reminder from "
                f"{_OUTPUT_STANDARD_REL}: {exc}"
            )
            return GateDecision(True, context + "\n" + warning.message, warning.state)
        return GateDecision(True, context, "valid")

    if not issues:
        return GateDecision(True)

    message = _invalid_message(view, issues)
    if hook_event == PRE_TOOL_USE:
        disposition = tool_disposition(tool_name, tool_input)
        if disposition in {READ_ONLY, REPAIR}:
            return GateDecision(
                True,
                message + f"\nAllowed current tool as {disposition}: {tool_name}",
                "invalid",
            )
        return GateDecision(False, message, "invalid")

    # Claude Code marks the recursive Stop pass after a prior Stop block. Let
    # that pass finish instead of consuming every continuation round in a loop.
    if stop_hook_active:
        return GateDecision(True, state="invalid")

    # STOP: a positively established invalid projection refuses the first end.
    return GateDecision(False, message, "invalid")


def render_hook_output(hook_event: str, decision: GateDecision) -> dict[str, Any] | None:
    """Translate a pure decision into Claude Code's event-specific JSON."""

    if hook_event == USER_PROMPT_SUBMIT:
        if not decision.message:
            return None
        output: dict[str, Any] = {
            "hookSpecificOutput": {
                "hookEventName": USER_PROMPT_SUBMIT,
                "additionalContext": decision.message,
            }
        }
        if decision.fail_open:
            output["systemMessage"] = decision.message
        return output

    if hook_event == PRE_TOOL_USE:
        if not decision.allow:
            return {
                "hookSpecificOutput": {
                    "hookEventName": PRE_TOOL_USE,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": decision.message,
                }
            }
        if decision.message:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": PRE_TOOL_USE,
                    "additionalContext": decision.message,
                }
            }
            if decision.fail_open:
                output["systemMessage"] = decision.message
            return output
        return None

    if hook_event == STOP:
        if not decision.allow:
            return {"decision": "block", "reason": decision.message}
        if decision.fail_open:
            # Stop additionalContext itself continues the loop.  A warning-only
            # systemMessage preserves the required fail-open outcome.
            return {"systemMessage": decision.message}
        return None

    return {"systemMessage": decision.message} if decision.message else None


def _payload_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("hook input must be one JSON object")
    return value


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the Chrono focus hook")
    parser.add_argument("--ledger", type=Path, default=workboard.WORKBOARD_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _cli_parser().parse_args(argv)
    try:
        payload = _payload_mapping(json.load(sys.stdin))
    except Exception as exc:
        decision = _fail_open(f"invalid hook input: {type(exc).__name__}: {exc}")
        print(json.dumps({"systemMessage": decision.message}, sort_keys=True))
        print(decision.message, file=sys.stderr)
        return 0

    payload_cwd = payload.get("cwd")
    if is_board_worktree_path(os.getcwd()) or is_board_worktree_path(
        payload_cwd if isinstance(payload_cwd, str) else None
    ):
        return 0

    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        event = ""
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        tool_name = ""
    raw_tool_input = payload.get("tool_input")
    tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}

    decision = decide(
        event,
        ledger_path=args.ledger,
        tool_name=tool_name,
        tool_input=tool_input,
        stop_hook_active=payload.get("stop_hook_active") is True,
        prompt=payload.get("prompt"),
    )
    output = render_hook_output(event, decision)
    if output is not None:
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    if decision.fail_open:
        print(decision.message, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
