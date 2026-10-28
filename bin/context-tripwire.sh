#!/usr/bin/env bash
# UserPromptSubmit hook: measure Chrono's own context size and warn when it is large.
#
# Why: on 2026-09-18 the coordinator ran 11 hours to ~768k cached input tokens with no
# compaction and its rules drifted. shared/lifecycle.md rule 8 gives specialist CLIs a
# 60%-of-window circuit breaker; nothing measured Chrono itself. The transcript already
# carries the number: every assistant record has message.usage, and
# cache_read + cache_creation + input is the context that was sent on the last call.
#
# Contract: always exit 0; print either one valid hook JSON object or nothing. A missing
# or unreadable transcript, malformed stdin, or any internal error prints nothing.
#
#   CHRONO_CONTEXT_WARN  default 600000  finish the current step, then handoff + /clear
#   CHRONO_CONTEXT_HARD  default 750000  handoff + /clear this turn, before anything else
#
# Retired from automatic registration on 2026-09-21; retained for explicit/manual use.
# See shared/lifecycle.md § 8.
#
# Historical, and still the live path resolution whenever this script is invoked that
# way: it was registered in chrono/.claude/settings.json as
# "${CLAUDE_PROJECT_DIR:-.}/../bin/...". Hooks run with cwd = the launch directory
# (chrono/), so the "." fallback resolves the same path and an unset
# CLAUDE_PROJECT_DIR does not surface as a "No such file" hook error.

set -uo pipefail

# The hook JSON arrives on stdin, so the Python source is passed via -c rather than a
# stdin heredoc; a heredoc would replace the hook payload.
PY_SRC=$(cat <<'PY'
import json
import os
import sys

TAIL_BYTES = 4 * 1024 * 1024
USAGE_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens")


def threshold(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def last_context(path):
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        tail = fh.read()
    for line in reversed(tail.split(b"\n")):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        # After an API error or an empty turn Claude Code appends an assistant record
        # with model "<synthetic>" and all-zero usage. That is not a measurement; skip
        # it and keep walking back, otherwise the hook goes silent exactly when the
        # session is in trouble.
        if message.get("model") == "<synthetic>":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict) or not any(k in usage for k in USAGE_KEYS):
            continue
        total = 0
        for key in USAGE_KEYS:
            value = usage.get(key, 0)
            total += value if isinstance(value, int) else 0
        if total <= 0:
            continue
        return total
    return None


def main():
    try:
        hook = json.loads(sys.stdin.read())
    except ValueError:
        return
    if not isinstance(hook, dict):
        return
    path = hook.get("transcript_path")
    if not isinstance(path, str) or not path or not os.path.isfile(path):
        return
    try:
        context = last_context(path)
    except OSError:
        return
    if context is None:
        return

    warn = threshold("CHRONO_CONTEXT_WARN", 600000)
    hard = threshold("CHRONO_CONTEXT_HARD", 750000)
    # min() so a WARN misconfigured above HARD cannot silence a context already past HARD.
    if context < min(warn, hard):
        return

    # The next session resumes from the regenerated capsule (chrono/CLAUDE.md, Start Of
    # Session), which is built from the charter and _state/chrono/OPEN-WORK.md. So the
    # handoff IS bringing those two up to date; there is no separate handoff document.
    handoff = (
        "tick every finished DONE-WHEN item on the active charter, append anything "
        "raised and not done to _state/chrono/OPEN-WORK.md (workboard.append_event), "
        "then run /clear; the next session resumes from the regenerated capsule"
    )
    if context >= hard:
        text = (
            "CONTEXT TRIPWIRE HARD: this session's context measures {ctx} tokens, "
            "at or above the hard limit of {hard}. Do the handoff and /clear THIS "
            "turn before anything else: {handoff}. Dispatch nothing new and start "
            "no other work first."
        ).format(ctx=context, hard=hard, handoff=handoff)
    else:
        text = (
            "CONTEXT TRIPWIRE WARN: this session's context measures {ctx} tokens, "
            "above the warning limit of {warn} (hard limit {hard}). Finish only the "
            "current small step. Dispatch nothing new. Then {handoff}."
        ).format(ctx=context, warn=warn, hard=hard, handoff=handoff)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }
    sys.stdout.write(json.dumps(out) + "\n")


try:
    main()
except Exception:
    pass
PY
)

python3 -c "$PY_SRC" 2>/dev/null
exit 0
