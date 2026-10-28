#!/usr/bin/env python3
"""Board-native dashboard renderer: dynamic spawn cards + completed-history rail.

Consumes the vs-board-snapshot.py stream (@SPAWN/@SUMMARY/@DONE/@DEFECT) and renders:
  * one card per LIVE spawn (0..N) — crew avatar motif + character + specialist +
    model badge + one-line task summary + elapsed timer; a card exists ONLY while
    its spawn is live (no idle/persistent boxes),
  * a rainbow border that sweeps one step per refresh on cards dispatched at the
    top effort tier (registry effort `max`, or an `ultra` flag),
  * the dispatched effort beside the model, and a warning row when the spawn's log
    shows the CLI silently fell back to another model or hit safety-classifier stops,
  * a compact capacity meter when nothing is running (not empty boxes),
  * a bounded completed-CLI history rail underneath for debugging/audit.

Adaptive: card columns scale with the pane width. Reads the snapshot from stdin if
piped, else runs vs-board-snapshot.py. Pure stdlib; rendered each frame by the loop.
"""
import csv
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

_VAULT_ROOT_ENV = os.environ.get("VAULT_ROOT")
if not _VAULT_ROOT_ENV:
    # Fail loud -- no maintainer-path default. Run via bin/vs-dashboard-loop.sh,
    # which sources shared/repo-root.sh and exports VAULT_ROOT before this runs.
    sys.exit("VAULT_ROOT not set. Run via bin/vs-dashboard-loop.sh, or export VAULT_ROOT.")
VAULT = Path(_VAULT_ROOT_ENV)
CARDS = VAULT / "shared" / "cards"
WIDTH = int(os.environ.get("VS_DASH_WIDTH", os.environ.get("COLUMNS", "72")) or "72")
NOW = int(time.time())

LANE_BADGE = {
    "gpt-codex": "codex", "codex": "codex", "claude": "claude",
    "gemini": "gemini", "grok": "grok",
}
# A recognizable emoji beside the model name, and a lane accent colour (256-colour).
MODEL_EMOJI = {"codex": "🤖", "claude": "✴️", "gemini": "♊", "grok": "⚡"}
LANE_COLOR = {"codex": 78, "claude": 170, "gemini": 39, "grok": 208}
CARD_W = 27  # inner width of a card (avatar 13 + gap 1 + info 13); 2 cols in a 61w pane

# --- ANSI colour (kept out of width math via _dwidth's stripper) --------------
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
# A terminal that cannot show colour, or a destination that is not a terminal at
# all, gets plain text. VS_DASH_COLOR=1 forces colour back on for a deliberate
# capture (rendering frames to a file to look at them); VS_DASH_COLOR=0 and
# NO_COLOR force it off.
_NO_COLOR = os.environ.get("VS_DASH_COLOR") != "1" and (
    bool(os.environ.get("NO_COLOR"))
    or os.environ.get("VS_DASH_COLOR") == "0"
    or os.environ.get("TERM") == "dumb"
    or not sys.stdout.isatty()
)


def _sgr(text, *codes):
    if _NO_COLOR or not codes:
        return str(text)
    return "\033[" + ";".join(str(c) for c in codes) + "m" + str(text) + "\033[0m"


def _fg(text, color256, *extra):
    return _sgr(text, 38, 5, color256, *extra)


# --- top-tier rainbow border --------------------------------------------------
# A hue wheel walked one cell at a time. The 256-colour ramp is the floor (every
# terminal the squad runs in has it); truecolor is used only when COLORTERM
# advertises it, where the same wheel comes out smooth instead of stepped.
_RAINBOW_256 = (196, 202, 208, 214, 220, 226, 190, 154, 118, 82, 46, 47, 48, 49,
                50, 51, 45, 39, 33, 27, 57, 93, 129, 165, 201, 199, 198, 197)
RAINBOW_STEPS = len(_RAINBOW_256)
RAINBOW_SPEED = 2  # hue cells advanced per refresh — frames are 1s, so this crawls

_TRUECOLOR = (
    os.environ.get("VS_DASH_TRUECOLOR") == "1"
    or (os.environ.get("VS_DASH_TRUECOLOR") != "0"
        and os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"))
)


def _hue(step):
    """(r, g, b) at `step`/RAINBOW_STEPS of one turn around the hue wheel."""
    sector = (step % RAINBOW_STEPS) * 6.0 / RAINBOW_STEPS
    ramp = int(255 * (1 - abs(sector % 2 - 1)))
    return ((255, ramp, 0), (ramp, 255, 0), (0, 255, ramp),
            (0, ramp, 255), (ramp, 0, 255), (255, 0, ramp))[int(sector)]


def _rainbow(text, start, stride=1):
    """Colour each cell of `text` one hue step further round the wheel.

    `start` is the first cell's position on the wheel and `stride` its direction,
    so a caller can lay one continuous sweep along a path that runs backwards on
    screen (the bottom rule, drawn left-to-right but travelling right-to-left)."""
    out = []
    for i, ch in enumerate(text):
        step = start + i * stride
        out.append(_sgr(ch, 38, 2, *_hue(step)) if _TRUECOLOR
                   else _fg(ch, _RAINBOW_256[step % RAINBOW_STEPS]))
    return "".join(out)


SNAPSHOT_TIMEOUT = float(os.environ.get("VS_DASH_SNAPSHOT_TIMEOUT", "10") or "10")


def _snapshot_lines():
    """Returns (lines, error): `error` is None on success, else a short reason.

    The exit status is load-bearing. A snapshot that dies prints nothing on
    stdout, and a board with no spawns renders the serene idle hourglass -- so
    a dashboard that had gone blind was byte-identical to a quiet squad. Every
    way the read can fail (non-zero exit, hang, unlaunchable) has to come back
    as an error the renderer can show, never as an empty spawn list.
    """
    # Read piped snapshot data only when explicitly asked (--stdin, for tests);
    # otherwise always run the snapshot. Never block on an inherited non-tty stdin
    # (e.g. a frame loop or a pipe that never sends EOF).
    if "--stdin" in sys.argv:
        return sys.stdin.read().splitlines(), None
    try:
        proc = subprocess.run(
            ["python3", str(VAULT / "bin" / "vs-board-snapshot.py")],
            capture_output=True, text=True, timeout=SNAPSHOT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return [], f"snapshot did not answer within {SNAPSHOT_TIMEOUT:g}s"
    except OSError as exc:
        return [], f"snapshot could not be run: {exc}"
    if proc.returncode != 0:
        detail = " ".join((proc.stderr or "").split())[-200:]
        reason = f"snapshot exited {proc.returncode}"
        return [], f"{reason}: {detail}" if detail else reason
    return proc.stdout.splitlines(), None


AVATAR_W = 13  # a "terminal bust" frame is 13 cols x 6 rows


def _card_meta(specialist):
    """(character, motif, tagline, active_frame_lines) from the .card file."""
    path = CARDS / f"{specialist}.card"
    name, motif, tagline = specialist, "•", ""
    frame, section = [], None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() == "---active---":
                section = "active"
                continue
            if line.strip() in ("---idle---", "---"):
                section = None if section != "active" else section
                if line.strip() == "---idle---":
                    section = "idle"
                continue
            if section == "active":
                frame.append(line)
            elif section is None:
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip() or specialist
                elif line.startswith("motif:"):
                    motif = line.split(":", 1)[1].strip() or "•"
                elif line.startswith("tagline:"):
                    tagline = line.split(":", 1)[1].strip()
    except OSError:
        pass
    # Normalise to a fixed 6 x AVATAR_W block — never via _fit, which collapses
    # whitespace and would mangle the art's internal spacing.
    frame = [_fit_art(row, AVATAR_W) for row in frame[:6]]
    while len(frame) < 6:
        frame.append(" " * AVATAR_W)
    return name, motif, tagline, frame


def _elapsed(started):
    secs = max(0, NOW - int(started))
    return f"{secs // 60}:{secs % 60:02d}"


def _dwidth(text):
    """Terminal display width: emoji/CJK count as 2 cells, combining marks as 0.

    Handles the U+FE0F variation selector (turns a text glyph into a width-2 emoji),
    which is the usual cause of drifted alignment with motif emoji like ⚙️/🔒."""
    width = 0
    chars = list(_ANSI_RE.sub("", str(text)))
    i = 0
    while i < len(chars):
        ch = chars[i]
        code = ord(ch)
        nxt = ord(chars[i + 1]) if i + 1 < len(chars) else 0
        if unicodedata.combining(ch):
            i += 1
            continue
        if nxt == 0xFE0F:  # emoji presentation selector -> width 2
            width += 2
            i += 2
            continue
        ea = unicodedata.east_asian_width(ch)
        if ea in ("W", "F") or code >= 0x1F000 or 0x2600 <= code <= 0x27BF:
            width += 2
        else:
            width += 1
        i += 1
    return width


def _fit_art(row, width):
    """Exactly `width` display cells of avatar art, whitespace intact.

    Cut and padded by CELLS, not characters: 53 of 73 crew cards have an emoji in
    a 13-character row, which made that row 14 or 15 cells, so whenever the info
    beside it filled its column the card's right edge stepped out by one. Cutting
    from the right drops trailing blanks before any art."""
    out = ""
    for ch in row:
        if _dwidth(out + ch) > width:
            break
        out += ch
    return out + " " * (width - _dwidth(out))


def _pad(text, width):
    """Left-justify text to a display width of `width` cells."""
    gap = width - _dwidth(text)
    return text + " " * max(0, gap)


def _fit(text, width):
    text = " ".join(str(text).split())
    if _dwidth(text) <= width:
        return text
    out = ""
    for ch in text:
        if _dwidth(out) + _dwidth(ch) > width - 1:
            break
        out += ch
    return out + "…"


def _short_model(model):
    """Compact model name for the card: first path segment before a slash."""
    return str(model).split("/")[0]


def _wrap2(text, width):
    """Wrap plain text into (line1, line2) by words, each <= width display cells. If it
    fits on one line, line2 is empty; overflow past two lines is …-truncated."""
    text = " ".join(str(text).split())
    if _dwidth(text) <= width:
        return text, ""
    words, line1, rest = text.split(" "), "", ""
    i = 0
    while i < len(words):
        cand = (line1 + " " + words[i]).strip()
        if _dwidth(cand) <= width:
            line1 = cand
            i += 1
        else:
            break
    rest = " ".join(words[i:])
    return line1, (_fit(rest, width) if rest else "")


QUIET_AFTER_S = 120  # log silence past this is worth a word on the card
# The warning sign WITH its emoji selector. A bare U+26A0 is the trap TOP_TIER_LABEL
# documents: _dwidth counts it as two cells, but tmux 3.6a draws it in one
# (measured 2026-09-23), so a card row carrying it came out a column short. With
# U+FE0F both _dwidth and tmux agree on two.
WARN = "⚠️"


def _process_alive(pid):
    """True / False when the pid can be probed, None when there is nothing to probe.

    Signal 0 asks the kernel whether the process exists without touching it.
    Board spawns run as this user, so EPERM is not expected -- but a process we
    are not allowed to signal is still a process, so it counts as alive."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _first_fitting(candidates, width):
    """Longest candidate that fits `width` display cells; the shortest otherwise."""
    for text in candidates:
        if _dwidth(text) <= width:
            return text
    return candidates[-1]


def _activity_line(idle_s, alive, width):
    """What a card may truthfully say about its spawn. `idle_s` is seconds since the
    log was last written, or -1 when it could not be read.

    The log is only half the signal. Claude streams to its log, so silence there
    means something; agy writes nothing until it finishes and codex writes only
    between steps, so on those lanes a quiet log is ordinary work. Ask the kernel
    about the pid before saying anything, and NEVER call a live process idle --
    the operator asked about three such cards in one day, every one of them alive.

    A card exists only because the snapshot found no terminal receipt, so a pid
    that is gone means the CLI died without writing one."""
    if 0 <= idle_s <= QUIET_AFTER_S:
        return _fg(_fit("● live", width), 78)
    quiet = "" if idle_s < 0 else f" {idle_s // 60}m{idle_s % 60:02d}"
    if alive is True:
        return _fg(_first_fitting(
            [f"● working (quiet{quiet})", "● working (quiet)", "● working"], width), 220)
    if alive is False:
        return _fg(_fit(f"{WARN} stopped", width), 203)
    # No probeable pid. Log silence is all that was measured, so it is all the
    # card claims -- no verdict about the process either way.
    if idle_s < 0:
        return ""
    return _fg(_fit(f"{WARN} quiet{quiet}, pid unknown", width), 203)


BOARD = VAULT / "_state" / "board-dispatch"
PROFILES = VAULT / "shared" / "registries" / "profiles.tsv"
TOP_TIER_EFFORTS = frozenset({"max"})
TOP_TIER_FLAGS = frozenset({"ultra"})
# ◆ and not ✦: _dwidth counts everything in 0x2600-0x27BF as two cells, but a real
# monospace terminal draws ✦ (U+2726) in one -- measured 2026-09-23 -- which left the
# top rule a column short of the bottom one. U+25C6 is one cell in both.
TOP_TIER_LABEL = "◆ MAX"
# The digest sits in the context's `authority` block, well before the task prompt
# that makes these files ~30 KB; the bound keeps a pathological file cheap.
_SELECTED_MODEL_RE = re.compile(r'"selected_model_sha256"\s*:\s*"([0-9a-f]{64})"')
CONTEXT_READ_BYTES = 262144


def _is_top_tier(profile):
    """Is this profile row the lane's top effort tier?

    Keyed on the registry's `effort` and `flags` columns, NEVER on profile names:
    a later claude.opus55.max or codex.sol6.ultra has to light up without an edit
    here, and a name match would quietly miss it."""
    if not profile:
        return False
    if str(profile.get("effort", "")).strip().lower() in TOP_TIER_EFFORTS:
        return True
    flags = re.split(r"[,\s]+", str(profile.get("flags", "")).strip().lower())
    return not TOP_TIER_FLAGS.isdisjoint(flags)


def _profile_by_bundle():
    """{selected_model_sha256: profile row} over the whole profile registry.

    A dispatch context records the SHA-256 of the selected profile BUNDLE, not
    the profile id (checked 2026-09-23: no context.json in _state/board-dispatch
    carries a `profile_id`). So the only exact way back to the row is to re-hash
    every registry row the way dispatch_context_builder.selected_model_sha256_for
    does and match the digest. ~27 rows, so it costs microseconds. If that hashing
    ever drifts, nothing matches and every card simply renders as normal."""
    table = {}
    try:
        with PROFILES.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                profile_id = row.get("profile_id")
                if not profile_id:
                    continue
                bundle = json.dumps(
                    {"profile_id": profile_id, "profile": dict(row)},
                    sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                ).encode("ascii")
                table[hashlib.sha256(bundle).hexdigest()] = row
    except (OSError, TypeError, ValueError, csv.Error):
        return {}
    return table


def _selected_model_digests(task_ids):
    """{task_id: selected_model_sha256} from each task's newest dispatch context.

    One directory scan per frame — the board keeps thousands of descriptors, so
    globbing per card would re-walk that listing once per card — then a stat and a
    bounded read only for the handful of files belonging to live cards."""
    wanted = set(task_ids)
    if not wanted:
        return {}
    newest = {}
    try:
        with os.scandir(BOARD) as entries:
            for entry in entries:
                if not entry.name.endswith(".context.json"):
                    continue
                task_id = entry.name.split(".", 1)[0]
                if task_id not in wanted:
                    continue
                try:
                    stamp = entry.stat().st_mtime
                except OSError:
                    continue
                if stamp >= newest.get(task_id, (0.0, ""))[0]:
                    newest[task_id] = (stamp, entry.path)
    except OSError:
        return {}
    digests = {}
    for task_id, (_stamp, path) in newest.items():
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                head = handle.read(CONTEXT_READ_BYTES)
        except OSError:
            continue
        match = _SELECTED_MODEL_RE.search(head)
        if match:
            digests[task_id] = match.group(1)
    return digests


def _attach_profiles(active):
    """Give every live spawn its dispatched profile row (None when unresolved) and
    that row's effort, which the card prints beside the model."""
    if not active:
        return
    table = _profile_by_bundle()
    digests = _selected_model_digests([s["task_id"] for s in active]) if table else {}
    for spawn in active:
        spawn["profile"] = table.get(digests.get(spawn["task_id"], ""))
        spawn["effort"] = str((spawn["profile"] or {}).get("effort", "")).strip()


# --- model truth: did the spawn actually run on the model it was given? --------
# Measured 2026-09-23: a canary dispatched on claude.opus55.max ran one message on
# claude-opus-5-5 and 25 on claude-opus-4-8. A safety classifier stopped a reply
# and Claude Code fell back without a word, while this card said opus-5-5 the whole
# time. The stream-json log is the only witness, so the card reads it.
MODEL_STATE = Path(os.environ.get("VS_DASH_MODELSTATE", "/tmp/vs-dash-model-truth.json"))
# The renderer is a fresh process every frame, so "incremental" means a byte offset
# per log kept in MODEL_STATE. A frame reads at most this much of each log: a big
# backlog drains over a few frames instead of stalling one.
LOG_CHUNK_BYTES = 2 * 1024 * 1024
MODEL_STATE_TTL_S = 3600  # forget a log this long after its card was last drawn
# Claude Code's own notice, as the first text of a user-role message. Matched as a
# prefix: a packet or tool result that merely QUOTES it (this feature's packet did)
# must not count as a stop.
CLASSIFIER_STOP = "Your response above was stopped by a safety classifier"
_TRUTH_MARKERS = (b'"assistant"', b"safety classifier", b"model_refusal_fallback")
_TRUTH_START = {"fell_back": False, "fb_to": "", "stops": 0, "model": ""}


def _scan_event(event, entry):
    """Fold one stream-json event into `entry`. Structure decides, never substrings:
    other lanes' logs say "fallback" in plain text, and a spawn working on this very
    dashboard quotes the classifier notice in its tool output."""
    kind = event.get("type")
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if kind == "assistant":
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "fallback":
                entry["fell_back"] = True
                target = (block.get("to") or {}).get("model")
                entry["fb_to"] = str(target or entry["fb_to"])
        model = message.get("model")
        # Only the main thread speaks for the task: a subagent may be pinned to
        # another model on purpose. "<synthetic>" marks harness-made messages.
        if (event.get("parent_tool_use_id") is None and isinstance(model, str)
                and model and not model.startswith("<")):
            entry["model"] = model
    elif kind == "system" and event.get("subtype") == "model_refusal_fallback":
        entry["fell_back"] = True
        entry["fb_to"] = str(event.get("fallback_model") or entry["fb_to"])
    elif kind == "user":
        content = message.get("content")
        blocks = [content] if isinstance(content, str) else content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                block = block.get("text")
            if isinstance(block, str) and block.lstrip().startswith(CLASSIFIER_STOP):
                entry["stops"] += 1


def _scan_log(path, entry):
    """Advance one log's scan from where the last frame stopped. Returns the updated
    entry, or None when the log cannot be read. Only whole lines are consumed; a
    single line longer than a whole chunk is skipped rather than stalling the scan."""
    try:
        info = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    # A new inode or a shorter file is a different log; a malformed entry is no
    # entry. Either way, start over from byte zero.
    if not (isinstance(entry, dict) and entry.get("ino") == info.st_ino
            and isinstance(entry.get("offset"), int) and isinstance(entry.get("skip"), bool)
            and all(isinstance(entry.get(k), type(v)) for k, v in _TRUTH_START.items())
            and entry["offset"] <= info.st_size):
        entry = {"ino": info.st_ino, "offset": 0, "skip": False, **_TRUTH_START}
    if info.st_size <= entry["offset"]:
        return entry
    try:
        with open(path, "rb") as handle:
            handle.seek(entry["offset"])
            data = handle.read(LOG_CHUNK_BYTES)
    except OSError:
        return entry
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) >= LOG_CHUNK_BYTES:
            entry["offset"] += len(data)
            entry["skip"] = True
        return entry  # otherwise a line still being written; wait for its newline
    start = data.find(b"\n") + 1 if entry["skip"] else 0
    for raw in data[start:end].split(b"\n"):
        if not any(marker in raw for marker in _TRUTH_MARKERS):
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            _scan_event(event, entry)
    entry["offset"] += end + 1
    entry["skip"] = False
    return entry


def _load_model_state():
    try:
        state = json.loads(MODEL_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_model_state(state):
    """Temp + fsync + rename, so a frame killed mid-write leaves the old file."""
    tmp = MODEL_STATE.with_name(f".{MODEL_STATE.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, MODEL_STATE)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _attach_model_truth(active):
    """Give every live spawn a `truth` dict from its attempt log: did it fall back,
    to what, how many classifier stops, and which model spoke last. The state file
    is written only when a scan moved, so an idle board never touches it."""
    if not active:
        return
    seen = lambda entry: entry.get("seen") if isinstance(entry.get("seen"), int) else 0
    state = _load_model_state()
    changed = False
    for spawn in active:
        path = str(spawn.get("log") or "")
        before = state.get(path)
        entry = _scan_log(path, dict(before) if isinstance(before, dict) else None)
        if entry is None:
            continue
        # Re-stamp a quiet log now and then, so a long-lived card is never pruned.
        if entry != before or NOW - seen(entry) > MODEL_STATE_TTL_S // 4:
            entry["seen"] = NOW
            state[path] = entry
            changed = True
        spawn["truth"] = entry
    if changed:
        _save_model_state({path: entry for path, entry in state.items()
                           if isinstance(entry, dict) and NOW - seen(entry) <= MODEL_STATE_TTL_S})


def _model_key(model):
    """claude-opus-5-5[1m], claude-opus-5-5-20260901 and claude-opus-5-5 are one model."""
    key = str(model or "").strip().lower().rsplit("/", 1)[-1]
    return re.sub(r"-\d{8}$", "", re.sub(r"\[[^\]]*\]$", "", key))


def _nick(model):
    """claude-opus-4-8 -> opus-4-8: the family name is the lane badge's job."""
    model = str(model)
    return model[len("claude-"):] if model.startswith("claude-") else model


def _selected_model(spawn):
    """The model the spawn was dispatched on: the resolved profile's, else the
    CLI's --model. The snapshot reports the bare lane name when there was no
    --model, and that names no model to compare against."""
    model = (spawn.get("profile") or {}).get("model_id") or spawn.get("model") or ""
    return "" if model == spawn.get("lane") else str(model)


def _model_label(model, effort, width):
    """`claude-opus-5-5 · max`, shortened until it fits. The effort is the new fact
    on this row, so the model name gives way first."""
    if not effort:
        return _fit(model, width)
    tail = f" · {effort}"
    for text in (f"{model}{tail}", f"{_nick(model)}{tail}"):
        if _dwidth(text) <= width:
            return text
    room = width - _dwidth(tail)
    return _fit(_nick(model), room) + tail if room >= 4 else _fit(model, width)


def _truth_line(truth, selected, width):
    """The warning row, or "" when the log shows nothing wrong.

    Two independent signals, either one enough: an explicit fallback record in the
    log, or a main-thread model that is not the one dispatched. The second catches
    a fallback whose record this code does not know how to read."""
    if not truth:
        return ""
    actual = truth.get("model") or ""
    drifted = bool(actual and selected and _model_key(actual) != _model_key(selected))
    stops = int(truth.get("stops") or 0)
    plural = "s" if stops != 1 else ""
    stop_long, stop_short = f"{stops} safety stop{plural}", f"{stops} stop{plural}"
    tail_long = f" · {stop_long}" if stops else ""
    tail_short = f" · {stop_short}" if stops else ""
    if drifted or truth.get("fell_back"):
        # Back on the dispatched model after a fallback is still worth a word.
        target, note = (actual, "") if drifted else (truth.get("fb_to") or "?", ", now back")
        nick = _nick(target)
        candidates = [
            f"{WARN} fell back → {nick}{note}{tail_long}",
            f"{WARN} fell back → {nick}{note}{tail_short}",
            f"{WARN} fell back → {nick}{tail_short}",
            f"{WARN} fell → {nick}{tail_short}",
            f"{WARN} fell back → {nick}{note}",
            f"{WARN} fell back → {nick}",
            f"{WARN} → {nick}",
        ]
        last_resort = f"! {nick}"
    elif stops:
        candidates = [f"{WARN} {stop_long}", f"{WARN} {stop_short}"]
        last_resort = f"! {stops}"
    else:
        return ""
    for text in candidates:
        if _dwidth(text) <= width:
            return text
    # ASCII only: _fit could otherwise cut between U+26A0 and its selector.
    return _fit(last_resort, width)


def _top_rule(cw, label):
    """A card's horizontal rule, `cw` display cells wide, optionally carrying a
    label. The label replaces rule cells rather than adding any, so the box width
    never moves -- and it is the whole cue when colour is off."""
    if not label:
        return "─" * cw
    tag = f"─ {label} "
    fill = cw - _dwidth(tag)
    return tag + "─" * fill if fill >= 0 else "─" * cw


def _render_card(spawn, cw, phase=0):
    """Avatar-forward card scaled to inner width `cw`: 6-row crew bust on the left with
    short info beside it (name, lane, model, elapsed, dispatched-at, activity), then
    full-width specialist / tagline / 2-line summary rows.

    A top-tier spawn (see _is_top_tier) gets a ✦ MAX label in its top rule and, when
    colour is available, a rainbow sweep around the border whose `phase` advances
    every frame. Only the border glyphs change: the body, and every other card, is
    byte-identical to what it renders without the cue."""
    name, motif, tagline, avatar = _card_meta(spawn["specialist"])
    lane = spawn["lane"]
    badge = LANE_BADGE.get(lane, lane)
    color = LANE_COLOR.get(badge, 250)
    memoji = MODEL_EMOJI.get(badge, "•")
    model = _short_model(spawn.get("model", lane))
    timer = _elapsed(spawn["started"])
    disp = time.strftime("%H:%MZ", time.gmtime(int(spawn["started"])))
    iw = cw - AVATAR_W - 1
    # Liveness from two independent signals: the streaming log's last write, and
    # whether the spawn's process is still there. See _activity_line. NOT a
    # timeout, just a cue for whether Chrono should check in.
    try:
        idle_s = max(0, int(NOW - os.path.getmtime(spawn["log"])))
    except OSError:
        idle_s = -1
    activity = _activity_line(idle_s, _process_alive(spawn.get("pid")), iw)
    info = [
        _sgr(_fit(f"{motif} {name}", iw), 1),                 # character
        _fg(_fit(f"{memoji} {badge}", iw), color, 1),         # lane
        _sgr(_model_label(model, spawn.get("effort"), iw), 2),  # dispatched model · effort
        _fg(_fit(f"⏱ {timer}", iw), 227),                    # elapsed
        _sgr(_fit(f"start {disp}", iw), 2),                   # dispatched-at (UTC)
        activity,
    ]
    edge = lambda s: _fg(s, color)
    s1, s2 = _wrap2("› " + (spawn.get("summary") or spawn["task_id"]), cw)
    body = [_pad(edge(avatar[i]) + " " + info[i], cw) for i in range(6)]
    body.append(_pad(_fg(_fit(spawn["specialist"], cw), 45), cw))
    # What the log says the model really did outranks the character's tagline, so
    # the warning takes that row and the card keeps its height.
    warning = _truth_line(spawn.get("truth"), _selected_model(spawn), cw)
    if warning:
        body.append(_pad(_fg(warning, 203, 1), cw))
    else:
        body.append(_pad(_sgr(_fit(f"“{tagline}”", cw), 2, 3) if tagline else "", cw))
    body.append(_pad(_fg(s1, 252), cw))
    body.append(_pad(_fg(s2, 252), cw))

    top = _is_top_tier(spawn.get("profile"))
    animate = top and not _NO_COLOR
    head, foot = _top_rule(cw, TOP_TIER_LABEL if top else ""), "─" * cw
    # One continuous hue sweep around the perimeter: across the top rule, down the
    # right pipes, back along the bottom rule, up the left pipes. Advancing `phase`
    # by RAINBOW_SPEED each frame walks the whole pattern around the box.
    boxw, inner = cw + 2, CARD_H - 2
    if animate:
        rows = [_rainbow("╭" + head + "╮", phase)]
        left = lambda r: _rainbow("│", phase + 2 * boxw + 2 * inner - 1 - r)
        right = lambda r: _rainbow("│", phase + boxw + r)
    else:
        rows = [edge("╭" + head + "╮")]
        left = right = lambda r: edge("│")
    rows.extend(left(r) + body[r] + right(r) for r in range(inner))
    rows.append(_rainbow("╰" + foot + "╯", phase + 2 * boxw + inner - 1, -1)
                if animate else edge("╰" + foot + "╯"))
    return rows  # CARD_H rows


CARD_H = 12  # rows per rendered card (must match _render_card output length)


def _layout(width, n):
    """Choose column count, then EXPAND card width to fill the pane so fields don't
    truncate and the board scales to the terminal. Returns (cols, card_inner_width)."""
    min_w = 28
    cols = max(1, (width + 2) // (min_w + 4))
    cols = max(1, min(cols, n))
    cw = (width - (cols - 1) * 2 - cols * 2) // cols  # fill: minus gaps and borders
    return cols, max(min_w, min(cw, 46))


def _grid(cards, cols):
    if not cards:
        return []
    height = len(cards[0])
    rows = []
    for i in range(0, len(cards), cols):
        block = cards[i : i + cols]
        for r in range(height):
            rows.append("  ".join(card[r] for card in block))
    return rows


def _dcenter(text, width):
    pad = max(0, width - _dwidth(text))
    left = pad // 2
    return " " * left + text + " " * (pad - left)


# Parametric hourglass: 8 sand rows (interior widths) framed by diagonal walls, so
# the sand can drain one cell at a time for a smooth fill instead of jumping.
_HG_INTERIOR = [7, 5, 3, 1, 1, 3, 5, 7]
_HG_TEMPL = [
    "|\\{}/|", "| \\{}/ |", "|  \\{}/  |", "|   \\{}/   |",
    "|   /{}\\   |", "|  /{}\\  |", "| /{}\\ |", "|/{}\\|",
]
_HG_CAP = 16  # total sand cells in one chamber (7+5+3+1)


def _hg_row(idx, n_sand):
    w = _HG_INTERIOR[idx]
    n = max(0, min(w, n_sand))
    pad = w - n
    left = pad // 2
    return _HG_TEMPL[idx].format(" " * left + "▓" * n + " " * (pad - left))


def _hourglass_frame(drained):
    """drained = cells already fallen to the bottom (0.._HG_CAP)."""
    sand = [0] * 8
    top = _HG_CAP - drained
    for r in (3, 2, 1, 0):  # top chamber settles neck-up
        put = min(_HG_INTERIOR[r], top)
        sand[r] = put
        top -= put
    bot = drained
    for r in (7, 6, 5, 4):  # bottom chamber settles base-up
        put = min(_HG_INTERIOR[r], bot)
        sand[r] = put
        bot -= put
    return ["._________."] + [_hg_row(i, sand[i]) for i in range(8)] + ["'---------'"]


def _colorize_glass(row, amber, sand):
    return "".join(
        _fg(ch, sand) if ch == "▓" else (" " if ch == " " else _fg(ch, amber))
        for ch in row
    )


_CLOCKS = "◴◵◶◷"    # quadrant clocks — rotate through the sequence
_CLOCKS2 = "◐◓◑◒"   # half-disc dials — a second, slower ring


def _idle_state(width, cap):
    """A Chrono time-field that scales to the pane: a big draining hourglass centred
    in the canvas, orbited by rings of spinning clock dials (each glyph rotates by the
    wall clock), sparkles, and the Chrono label + live UTC clock. Fills VS_DASH_HEIGHT."""
    t = int(time.time())
    height = int(os.environ.get("VS_DASH_HEIGHT", "26") or "26")
    H = max(16, height - 3)                 # leave room for the collapsed history
    grid = [[" "] * width for _ in range(H)]
    cr, cc = H // 2, width // 2

    # central hourglass (smooth drain)
    cyc = t % 11
    drained = min(_HG_CAP, cyc * 2) if cyc <= 8 else _HG_CAP
    hg = _hourglass_frame(drained)
    ht, hl = cr - len(hg) // 2, cc - 6
    for i, row in enumerate(hg):
        for j, ch in enumerate(row):
            if ch != " " and 0 <= ht + i < H and 0 <= hl + j < width:
                grid[ht + i][hl + j] = ch

    def place(r, c, ch):
        if 0 <= r < H and 0 <= c < width and grid[r][c] == " ":
            grid[r][c] = ch

    # orbit rings of spinning clock dials (Novachrono time magic)
    for ring, (rx, ry, glyphs, n) in enumerate((
        (int(width * 0.42), int(H * 0.44), _CLOCKS, 12),
        (int(width * 0.26), int(H * 0.28), _CLOCKS2, 8),
    )):
        for k in range(n):
            ang = 2 * math.pi * k / n + ring * 0.5
            r = cr + int(round(ry * math.sin(ang)))
            c = cc + int(round(rx * math.cos(ang)))
            place(r, c, glyphs[(t + k + ring) % 4])

    # faint drifting sparkles
    rng = random.Random(7)
    for _ in range(max(4, width * H // 260)):
        place(rng.randint(0, H - 1), rng.randint(0, width - 1),
              "·" if (t + rng.randint(0, 3)) % 2 else "˙")

    # labels under the hourglass
    def overlay(text, r):
        c0 = max(0, (width - len(text)) // 2)
        for k, ch in enumerate(text):
            if 0 <= r < H and 0 <= c0 + k < width:
                grid[r][c0 + k] = ch
    base = ht + len(hg) + 1
    overlay("C H R O N O", base)
    overlay("awaiting dispatch" + (f"   ·   {cap} slots ready" if cap != "?" else ""), base + 1)
    overlay(time.strftime("%H:%M:%S UTC", time.gmtime(t)), base + 2)

    out = []
    for r in range(H):
        cells = []
        for c in range(width):
            ch = grid[r][c]
            if ch == " ":
                cells.append(" ")
            elif ch == "▓":
                cells.append(_fg(ch, 179))
            elif ch in _CLOCKS:
                cells.append(_fg(ch, 45))
            elif ch in _CLOCKS2:
                cells.append(_fg(ch, 141))
            elif ch in "·˙":
                cells.append(_fg(ch, 240))
            elif ch in ".|\\/_'-":
                cells.append(_fg(ch, 214))
            else:
                cells.append(_sgr(ch, 1, 38, 5, 252))
        out.append("".join(cells))
    return out


def _failure_state(width, reason):
    """The board could not be read. Deliberately unlike _idle_state: no
    hourglass, no "awaiting dispatch", no clock -- because the failure this
    guards is a blind dashboard wearing the calm face of a quiet squad. The
    reason travels into the pane, since the operator is looking at the pane and
    not at the loop's stderr. A box, not a canvas: unlike _idle_state this does
    not fill VS_DASH_HEIGHT, because main() already pads the frame to the pane
    and erases below it."""
    inner = max(20, width - 4)
    body = [
        _sgr(_dcenter("⛔  BOARD SNAPSHOT FAILED", inner), 1, 38, 5, 203),
        "",
        *(_fg(_dcenter(line, inner), 216) for line in _wrap2(reason, inner) if line),
        "",
        _fg(_dcenter("the dashboard is blind — this is NOT an idle board", inner), 245),
        _fg(_dcenter("spawns may be running and unshown; check bin/vs-board-snapshot.py", inner), 245),
    ]
    rule = _fg("━" * width, 203)
    body = [_fg("┃ ", 203) + _pad(line, inner) + _fg(" ┃", 203) for line in body]
    return ["", rule, *body, rule]


HIST_STATE = Path(os.environ.get("VS_DASH_HISTSTATE", "/tmp/vs-dash-history.state"))


def _history_open():
    try:
        return HIST_STATE.read_text(encoding="utf-8").strip() == "open"
    except OSError:
        return False


def _history_rail(done, width, open_, limit):
    """Collapsible 'recent spawns' dropdown. Returns (block, header_row_offset) where
    the offset locates the clickable header line inside the block (for the hit-map)."""
    total = len(done)
    if not total:
        return [""], None
    caret = "▾" if open_ else "▸"
    hint = "" if open_ else _sgr("  double-click to expand", 2)
    header = _fg(f"{caret} recent spawns ({total})", 245) + hint
    block = ["", header]
    if open_:
        for entry in done[:limit]:
            ok = entry["status"] in ("complete", "completed", "launched")
            mark = _fg("✓", 78) if ok else _fg("✗", 203)
            spec = _sgr(_pad(_fit(entry["specialist"], 20), 20), 2)
            summ = _pad(_fit(entry["summary"] or entry["task_id"], width - 46), max(0, width - 46))
            dur = _fg(f"{entry['duration']}s".rjust(5), 245)
            lc = LANE_COLOR.get(LANE_BADGE.get(entry["lane"], entry["lane"]), 245)
            lane = _fg(f"{entry['lane'][:6]:<6}", lc)
            mem = "🧠" if entry["memory_id"] not in ("-", "", None) else "  "
            block.append(f"{mark} {spec} {summ} {dur} {lane} {mem}")
    return block, 1  # header sits at block index 1 (index 0 is the spacer)


HITMAP = Path(os.environ.get("VS_DASH_HITMAP", "/tmp/vs-dash-hitmap.tsv"))


def _write_hitmap(active, header_rows, cols, cw, hist_row=None, width=0):
    """Record clickable rectangles for the double-click handler, rewritten every
    frame: each live card -> (task_id, log), plus the history header -> @HISTORY
    (a toggle target). Rows/cols are pane-relative (the pane never scrolls)."""
    stride = cw + 4  # card body (cw + 2 borders) + 2-space column gap
    rows = []
    for i, spawn in enumerate(active):
        r, c = divmod(i, max(1, cols))
        y0 = header_rows + r * CARD_H
        x0 = c * stride
        rows.append(
            f"{y0}\t{y0 + CARD_H}\t{x0}\t{x0 + cw + 2}\t{spawn['task_id']}\t{spawn['log']}"
        )
    if hist_row is not None:
        rows.append(f"{hist_row}\t{hist_row + 1}\t0\t{width}\t@HISTORY\t-")
    try:
        HITMAP.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    except OSError:
        pass


SWARM_STATUS = Path(os.environ.get("VS_SWARM_STATUS", "/tmp/vs-swarm.status"))


def _write_swarm_status(active, snapshot_error=None):
    """Write the tmux status-bar segment: a coloured tag per dispatched specialist
    (character name, lane colour) — like the 'chrono' tag but for the live swarm.

    A failed read must not post the "idle" tag: the status bar is the
    at-a-glance signal, so a blind dashboard reporting idle there is the same
    lie as the idle canvas, in the place most likely to be believed."""
    try:
        if snapshot_error is not None:
            SWARM_STATUS.write_text("#[fg=colour203,bold] ⛔ board unreadable #[default]")
            return
        if not active:
            SWARM_STATUS.write_text("#[fg=colour240]· idle ·#[default]")
            return
        tags = []
        for spawn in active[:4]:
            name, _motif, _tag, _frame = _card_meta(spawn["specialist"])
            color = LANE_COLOR.get(LANE_BADGE.get(spawn["lane"], spawn["lane"]), 250)
            tags.append(f"#[fg=colour{color},bold] {name} #[default]")
        if len(active) > 4:
            tags.append(f"#[fg=colour240]+{len(active) - 4}#[default]")
        SWARM_STATUS.write_text(" ".join(tags))
    except OSError:
        pass


def main():
    active, done, defects = [], [], []
    pending_summary_for = None
    snapshot, snapshot_error = _snapshot_lines()
    for line in snapshot:
        parts = line.split("\t")
        tag = parts[0]
        if tag == "@SPAWN" and len(parts) >= 7:
            active.append({
                "task_id": parts[1], "lane": parts[2], "specialist": parts[3],
                "started": parts[4], "pid": parts[5], "log": parts[6], "summary": "",
                "model": parts[7] if len(parts) > 7 else parts[2],
            })
            pending_summary_for = active[-1]
        elif tag == "@SUMMARY" and pending_summary_for is not None:
            pending_summary_for["summary"] = parts[1] if len(parts) > 1 else ""
            pending_summary_for = None
        elif tag == "@DONE" and len(parts) >= 8:
            done.append({
                "ended": parts[1], "task_id": parts[2], "lane": parts[3],
                "specialist": parts[4], "status": parts[5],
                "duration": int(parts[6]) if parts[6].isdigit() else 0,
                "memory_id": parts[7], "summary": "",
            })
        elif tag == "@DEFECT" and len(parts) >= 5:
            defects.append(f"{parts[1]}/{parts[2]}: {parts[4]}")

    _attach_profiles(active)  # which cards were dispatched at the top effort tier
    _attach_model_truth(active)  # which cards silently fell back to another model
    _write_swarm_status(active, snapshot_error)  # tmux status-bar specialist tag(s)
    lines = []
    n = len(active)
    cap = os.environ.get("VS_DASH_CAPACITY", "?")
    utc = time.strftime("%H:%M:%S UTC", time.gmtime())
    title = _sgr("◢ VIBE SQUAD · SPECIALIST SWARM ◣", 1, 38, 5, 45)
    if snapshot_error is not None:
        # "0 dispatched" would be a measurement; nothing was measured.
        cnt = _fg("board unreadable", 203)
        capstr = ""
    else:
        cnt = _fg(f"{n} dispatched", 78 if n else 245)
        capstr = _fg(f" / {cap}", 245) if cap != "?" else ""
    status = f"{cnt}{capstr}   {_fg('·', 240)}   {_fg(utc, 245)}"
    if defects:
        status += "   " + _fg(f"⚠ {len(defects)} process defect(s): {defects[0]}", 203)
    lines.append(_dcenter(title, WIDTH))
    lines.append(_dcenter(status, WIDTH))
    lines.append("")

    header_rows = 3  # title, status, blank — the grid starts on row 3
    if snapshot_error is not None:
        cols, cw = 1, CARD_W
        lines.extend(_failure_state(WIDTH, snapshot_error))
    elif active:
        cols, cw = _layout(WIDTH, len(active))
        phase = NOW * RAINBOW_SPEED  # one wheel step per frame; the loop paints 1/s
        lines.extend(_grid([_render_card(s, cw, phase) for s in active], cols))
    else:
        cols, cw = 1, CARD_W
        lines.extend(_idle_state(WIDTH, cap))

    # Collapsible history dropdown: short tail when cards are live, longer when idle.
    hist_limit = 5 if active else 14
    hist_start = len(lines)
    block, hdr_rel = _history_rail(done, WIDTH, _history_open(), hist_limit)
    lines.extend(block)
    hist_row = (hist_start + hdr_rel) if hdr_rel is not None else None
    _write_hitmap(active, header_rows, cols, cw, hist_row, WIDTH)
    if os.environ.get("VS_DASH_INPLACE") == "1":
        # Home the cursor, erase each line to EOL, clear below — repaint in place with
        # no full-screen clear (that's what blinks). Pad to the pane height so a shorter
        # frame overwrites a taller previous one.
        height = int(os.environ.get("VS_DASH_HEIGHT", "0") or "0")
        while height and len(lines) < height - 1:
            lines.append("")
        sys.stdout.write("\033[H" + "".join(ln + "\033[K\n" for ln in lines) + "\033[J")
    else:
        sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
