# Native notification-hook canary

Run this **as Chrono, from a native terminal outside any lane sandbox**, after the
fix commit passes its required cross-family review. This procedure does not
authorize enabling the live repository. It enables only an archived scratch
copy for at most 90 minutes per run. Use **two separate scratch runs**, each
with its own directory, private tmux server and watchdog. Keep both evidence
directories afterward; neither run alone constitutes the complete canary.

| Run | Sections | Setup/launch minutes | Case minutes | Catch-up minutes | Rollback minutes | Total minutes | Watchdog margin minutes |
|---|---|---:|---:|---:|---:|---:|---:|
| interactive | 1, 2, 3, 5, 6 | 10 | 25 | 0 | 5 | 40 | 50 |
| soak | 1, 2, 4, 6 | 8 | 67 | 3 | 5 | 83 | 7 |

The interactive case budget includes 15 minutes for section 3 and 10 for
section 5. Run the soak from a fresh section 1 setup; do not append it to the
interactive run. These are execution budgets, not measured durations. If
approvals or setup consume the margin, stop and retain an incomplete result;
reserve the final five minutes for section 6, without extending the watchdog.

The unit tests use fixture process identities. They do not prove installed
Claude settings inheritance, asyncRewake delivery, draft preservation, host CPU
cost, or model token cost. This canary measures those surfaces. A missing
measurement is not a pass.

## 1. Prepare the reviewed copy and automatic rollback

Use a dedicated **bash** terminal for these blocks; keep it open. Choose the
reviewed commit explicitly. No bypass-permissions option is used.
`set -euo pipefail` exits this dedicated shell on a failed assertion. Print the
scratch path immediately after creating it so the evidence remains findable
even if setup fails before the final attach command.

```bash
set -euo pipefail
LIVE="${REPO_ROOT:?Set REPO_ROOT to your repository checkout}"
REF=REPLACE_WITH_REVIEWED_COMMIT
RUN=interactive  # Repeat sections 1 and 2 later with RUN=soak in a fresh terminal.
case "$RUN" in interactive|soak) ;; *) exit 1 ;; esac
test ! -e "$LIVE/_state/chrono/notification-hooks.enabled"
test ! -L "$LIVE/_state/chrono/notification-hooks.enabled"
REF=$(git -C "$LIVE" rev-parse "$REF^{commit}")
CLAUDE_BIN=$(command -v claude)
command -v tmux
"$CLAUDE_BIN" --version
S=$(mktemp -d /tmp/notification-canary.XXXXXX)
printf 'Evidence directory (retain on failure): %s\n' "$S"
R="$S/repo"
SOCKET="notice-$(basename "$S")"
mkdir -p "$R" "$S/probe"
git -C "$LIVE" archive "$REF" | tar -x -C "$R"
printf '%s\n' "$REF" > "$S/reviewed-commit.txt"
printf '%s\n' "$RUN" > "$S/run-kind.txt"
FLAG="$R/_state/chrono/notification-hooks.enabled"
mkdir -p "$(dirname "$FLAG")"
export S R SOCKET CLAUDE_BIN FLAG RUN
export CANARY_COST_LOG="$S/native-cost.jsonl"

disable_canary() {
    if [ -f "$FLAG" ]; then mv "$FLAG" "$FLAG.disabled"; fi
}
trap disable_canary EXIT INT TERM HUP

# This independent watchdog remains effective if the terminal disconnects.
# It disables the scratch flag and stops only this uniquely named tmux server.
python3 -B - "$FLAG" "$SOCKET" "$S" <<'PY' &
import pathlib, signal, subprocess, sys, time
signal.signal(signal.SIGHUP, signal.SIG_IGN)
flag, socket, evidence = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
deadline = time.monotonic() + 5400
(evidence / 'deadline-utc.txt').write_text(str(time.time() + 5400) + '\n')
while time.monotonic() < deadline and not (evidence / 'finished').exists():
    time.sleep(0.5)
if flag.exists():
    flag.replace(flag.with_suffix('.disabled'))
time.sleep(2)
subprocess.run(['tmux', '-L', socket, 'kill-server'], capture_output=True)
(evidence / 'watchdog-done').touch()
PY
WATCHDOG_PID=$!
printf '%s\n' "$WATCHDOG_PID" > "$S/watchdog.pid"
```

The live-flag checks are read-only. The watchdog owns only the newly created
scratch flag and unique server. If time expires, stop; do not extend the window
or reuse a terminated watchdog. A later attempt needs a new scratch directory.

## 2. Record native cost without replacing process or filesystem observations

This scratch-only `sitecustomize` counts actual Python subprocess launches of
`ps`, parsed notification stores, exclusive flocks and fsyncs. It calls every
original function and flushes counts at normal process exit. Its small counting
overhead is included in the CPU measurement. Shell pane-validation forks are
outside these worker counters; hook process count and elapsed time are recorded
separately. A killed process without a final row is an incomplete measurement.
Copy the probe into the archive's Python search path so the independently
launched sampler also loads it; the reviewed production modules stay unchanged.

```bash
cat > "$S/probe/sitecustomize.py" <<'PY'
import atexit, fcntl, json, os, resource, subprocess, sys, time
started = time.monotonic()
cpu_started = resource.getrusage(resource.RUSAGE_SELF)
counts = dict(ps=0, store_parses=0, exclusive_locks=0, fsyncs=0)
original_popen = subprocess.Popen
original_loads = json.loads
original_flock = fcntl.flock
original_fsync = os.fsync
def popen(args, *a, **kw):
    if isinstance(args, (list, tuple)) and args and os.path.basename(str(args[0])) == 'ps':
        counts['ps'] += 1
    return original_popen(args, *a, **kw)
def loads(*a, **kw):
    value = original_loads(*a, **kw)
    if isinstance(value, dict) and value.get('schema') == 'chrono-notifications/v1':
        counts['store_parses'] += 1
    return value
def flock(fd, operation):
    if operation & fcntl.LOCK_EX:
        counts['exclusive_locks'] += 1
    return original_flock(fd, operation)
def fsync(fd):
    counts['fsyncs'] += 1
    return original_fsync(fd)
subprocess.Popen, json.loads = popen, loads
fcntl.flock, os.fsync = flock, fsync
@atexit.register
def finish():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    row = dict(counts, pid=os.getpid(), argv=sys.argv, elapsed=time.monotonic() - started,
               cpu_seconds=usage.ru_utime + usage.ru_stime - cpu_started.ru_utime - cpu_started.ru_stime)
    fd = os.open(os.environ['CANARY_COST_LOG'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.write(fd, (json.dumps(row) + '\n').encode())
    os.close(fd)
PY
cp "$S/probe/sitecustomize.py" "$R/scripts/python/sitecustomize.py"
cmp "$S/probe/sitecustomize.py" "$R/scripts/python/sitecustomize.py"

cat > "$S/launch.sh" <<'SH'
#!/bin/bash
set -euo pipefail
root=$1
subdir=$2
export CHRONO_NOTIFICATION_WAIT_SECONDS=$3
export CHRONO_NOTIFICATION_DISK_WARN_GIB=${4:-25}
export CHRONO_NOTIFICATION_DISK_CRITICAL_GIB=${5:-15}
export CHRONO_NOTIFICATION_ROOT="$root"
export PYTHONPATH="$root/../probe"
export PYTHONDONTWRITEBYTECODE=1
export CANARY_COST_LOG="$root/../native-cost.jsonl"
unset CHRONO_NOTIFICATION_PANE_HELPER CHRONO_NOTIFICATION_PYTHON CHRONO_COORDINATOR_PIDFILE
unset CHRONO_NOTIFICATION_POLL_SECONDS CHRONO_NOTIFICATION_SAMPLE_SECONDS CHRONO_NOTIFICATION_RETRY_SECONDS
unset CHRONO_NOTIFICATION_RECOVERY_SAMPLES CHRONO_NOTIFICATION_DISK_HYSTERESIS_GIB TMUX_BIN
mkdir -p "$root/_state/runtime/chrono-coordinator"
start=$(LC_ALL=C ps -o lstart= -p $$ | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//')
printf 'pid %s\nshape pane\ntarget notice-canary:chrono.0\nstart %s\n' "$$" "$start" \
  > "$root/_state/runtime/chrono-coordinator/notice-canary.pid"
cd "$root/$subdir"
exec "$CLAUDE_BIN" --setting-sources project
SH

n() { PYTHONPATH="$R/scripts/python" python3 -B -m chrono_state.notifications --vault "$R" "$@"; }
start_canary() {
    test ! -f "$S/watchdog-done"
    test -f "$FLAG"
    tmux -L "$SOCKET" new-session -d -s notice-canary -n chrono -c "$R" \
      "exec bash '$S/launch.sh' '$R' '$1' '$2' '${3:-25}' '${4:-15}'"
}
touch "$FLAG"
if [ "$RUN" = interactive ]; then
    start_canary . 120
else
    start_canary chrono 1500
fi
printf 'Evidence: %s\nAttach in another terminal: tmux -L %s attach\n' "$S" "$SOCKET"
```

Do not start the squad, reconciler, watchers, or board dispatcher in the copy.
It has no live registry or local settings file. Use only the tests below.
Normal Claude authentication and subscription use remain native to Chrono.

Prepare this delivery checker in **each** run. For idle notices, save the first
offer record before retries can overwrite `offered_at`, then the final `n show`
record after approving the exact ACK. `VISIBLE_AT` is the UTC timestamp of the
system reminder containing that exact notice ID in this session's transcript
(or a timestamped native recording); it is not the ACK tool invocation time.
If that timestamp cannot be measured, delivery remains UNMEASURED.

```bash
cat > "$S/check-delivery.py" <<'PY'
import json, pathlib, sys
from datetime import datetime
notice = json.loads(pathlib.Path(sys.argv[1]).read_text())
ack = json.loads(pathlib.Path(sys.argv[2]).read_text())
def epoch(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert parsed.tzinfo is not None, 'delivery timestamps require a timezone'
    return parsed.timestamp()
enqueued = epoch(notice['enqueued_at'])
offered = epoch(notice['offered_at']) - enqueued
visible = epoch(sys.argv[3]) - enqueued
assert notice['id'] == ack['id'] and notice['enqueued_at'] == ack['enqueued_at'], 'mismatched ACK evidence'
assert notice.get('offer_count') == 1, 'idle SLA needs the first offer, not a retry'
assert 0 <= offered <= 5, f'waiter offer missed delivery SLA: {offered}'
assert 0 <= visible <= 5, f'visible reminder missed delivery SLA: {visible}'
assert ack['state'] == 'acknowledged', 'exact ACK still required eventually'
print(json.dumps(dict(offer_latency_seconds=offered, visible_latency_seconds=visible,
                     ack_latency_seconds=ack.get('ack_latency_seconds'))))
PY
```

This checker applies the five-second SLA to **delivery**, independently of
operator approval and model/tool execution. ACK latency is reported separately
without a five-second limit. Waiting for permission never grants consent.

## 3. Short interactive cases (interactive run only; about 15 minutes)

In the attached Claude session, inspect `/hooks`: all seven events must be
present. Ask: “For this canary, preserve any draft and pending permission; after
reading and classifying a CHRONO SYSTEM NOTICE, run its exact ACK command, then
stop. Do not dispatch work.” Save `/hooks` evidence and the session's transcript
path/ID. Match transcripts by this scratch cwd and session ID; never select a
global newest transcript or scan unrelated sessions.

```bash
n health > "$S/health-root.json"
n enqueue-task --task-id TASK-CANARY-IDLE-1 --generation 1 --kind complete --summary 'canary idle one' > "$S/idle-1.json"
```

Repeat with `IDLE-2` and `IDLE-3`. For each returned ID, save `n show ID`; require
an offer and a visible reminder within five seconds of enqueue, then an eventual
exact-ID acknowledgment. Save the first `n show ID` as `idle-1-first.json` while
`offer_count` is 1, and a separate `idle-1-final.json` after the ACK. Run
`python3 -B "$S/check-delivery.py" "$S/idle-1-first.json" "$S/idle-1-final.json" "$VISIBLE_AT"`
(substitute that case's paths/time). Save its output; retries while ACK approval
is pending may change the final record without changing first delivery. ACK latency includes
operator approval; report it separately without applying the delivery SLA. After
each turn, require a live waiter and refreshed `last_armed_at` within 2 seconds.
The waiter and sampler must both be `live` in health.

| Case | Action | Observable pass condition |
|---|---|---|
| Nested session | Ask the canary to run `claude -p 'say hi'` in the copy. If the installed CLI refuses nested execution, record that restriction and run an operator-started headless child inheriting this pane environment as a separate test. Do not bypass a CLI restriction silently. | Original session stays open, no child notification session is added, and a new idle notice still arrives. Native reachability remains unmeasured if neither nested path is available. |
| Draft | Type `DRAFT-DO-NOT-SEND-7731` without Enter; enqueue another notice. | Record delivery latency; draft text survives. The exact canary transcript contains no user submission of that text. |
| Busy | Ask for `sleep 20`; enqueue during the tool call. | Notice appears at the tool boundary and receives an exact ack. No fabricated operator input. |
| Permission | Request a harmless command that the native CLI requires approval for; enqueue while the dialog is open. | Dialog remains unanswered; delivery/ack delay is recorded separately from idle SLA. |
| Stop recursion | Enqueue two notices during one turn. | At most one Stop block for that batch; no recursive blocking. |
| Renewal | Stay idle through the 120-second waiter deadline, then enqueue. | One renewal reminder, re-arm, then notice delivery. Record renewal token usage. |
| Clear | Run `/clear`, then enqueue. | Prior session has `closed_at`; new session arms and receives notice. |
| Resume | Use in-process `/resume` to return to the conversation recorded before Clear, then enqueue. Switch to another conversation and resume back again; enqueue a distinct notice each time. | The original session ID reopens despite its earlier `closed_at`. Each abandoned same-owner session closes, its sampler exits cooperatively, and the resumed conversation has one sampler and receives its notice. |

For Resume, retain the session IDs and before/after health/store snapshots.
Require the resumed ID to equal the earlier recorded ID; a fresh ID does not
exercise the resume-back regression. Verify cooperative exit using the abandoned
session's captured worker identities, and record delivery/ACK evidence for both
returns (after Clear and after switching conversations). The store-level
regressions cover these transitions with fixture identities; native `/resume`
payloads, worker exits and visible reminders still need this canary.

Run `/exit`; require a closed session and no surviving matching worker
identities. Relaunch with `start_canary chrono 1500`. Repeat `/hooks`, health,
and one idle delivery. This is the real coordinator's working-directory shape.
If inherited root settings do not load from `chrono/`, stop and report failure.
Continue to section 5, then section 6; do not run section 4 in this scratch copy.

The parent guard accepts the validated coordinator directly or within four
ancestors (up to three intervening helpers) from one process snapshot. The
nearest Claude ancestor must be the recorded coordinator; a nested Claude is
rejected. The fixture suite covers a real `/usr/bin/time` wrapper with an
injected process snapshot, plus nested Claude ancestry; native `ps` is a separate
canary measurement. Native `/hooks` visibility alone does not prove parentage:
require `health` to show a session and both workers live, plus actual delivery.
Unknown parentage or deeper chains fail closed; missing sessions/delivery is a
canary failure, even if the hook exits zero.

## 4. Default-duration retry, renewal, and idle cost (soak run only; 67 minutes)

Start with fresh sections 1 and 2 using `RUN=soak`; do not run sections 3 or 5.
Inspect `/hooks`, record the exact transcript path/session ID, and require live
waiter/sampler health before beginning the 67-minute measurement.
In this fresh `chrono/` session, instruct the model to leave the next notice
unacknowledged, including during renewals. Enqueue it and retain its ID:

```bash
n enqueue-task --task-id TASK-CANARY-UNACKED --generation 1 --kind complete --summary 'leave this canary unacknowledged' > "$S/unacked.json"
```

Record actual `offered_at`, `offer_count`, lease expiry, reminder timestamps and
turn usage from the exact canary transcript. Expected notice offers, relative to
their preceding offer: initial, +60 seconds, +300 seconds, +1800 seconds; at most
four total automatic offers. Hook scheduling adds latency, so compare intervals,
not exact clock ticks. After the fourth, `context_only_pending` is 1. This notice
must not cause further async wakes, including after a fresh waiter process.
Do not send operator prompts or manually request Stop catch-up during this
measurement. Renewal reminders remain separate events, approximately every
25 minutes of a waiter's idle lifetime; do not count them as notice retries.

At elapsed 31 and 61 minutes, enqueue a **different** ordinary notice from the
operator terminal. Require first offer and visible reminder within 5 seconds
using `check-delivery.py`, eventual exact ACK with its latency reported
separately, and re-arm within 2 seconds after the resulting turn. These test a
waiter that lived across renewals, not an accelerated
clock. Continue idle through minute 67, past the fourth offer's 30-minute lease
expiry (nominally minute 66). Require no fifth automatic offer; this distinguishes
the wake cap from backoff alone. Then send a user prompt requesting context catch-up: the unacknowledged
notice must be surfaced while its obligation and any held-attention state stay
intact. Explicitly ack it to finish, then go directly to section 6. Budget three
minutes for this catch-up and five for rollback within the 90-minute watchdog.

Cost evidence:

- Save transcript usage for every notice, renewal, and otherwise unexplained
  model turn: input, cached input, cache-write input if provided, and output
  tokens. Deduplicate transcript usage by message ID; streamed updates can repeat
  the same usage. Report observed token totals and turns/hour separately for
  renewal and notice delivery. Do not invent a dollar price for subscription
  usage. If usage is unavailable, token cost remains unmeasured.
- After cooperative worker exits in step 6, inspect `native-cost.jsonl`. For
  each waiter/sampler row lasting at least 30 seconds, calculate `ps/elapsed`,
  `store_parses/elapsed`, and `100*cpu_seconds/elapsed` (percent of one core).
  Proposed canary acceptance budgets: each long-lived worker <=1 `ps` launch/s,
  <=1 store parse/s, and <=1% of one core. These are limits to verify, not
  performance claims. Report rows and totals, including locks/fsyncs.
- For context-hook rows, report count, median and p95 process duration. Compare
  root versus `chrono/`. Startup, fault, and ordinary hook cases must be separated.
  Record shell-level whole-hook duration separately with `/usr/bin/time -lp` on
  a flag-absent invocation (step 6); the Python counters exclude that shell path.
- Chrono must decide whether the measured renewal tokens/turns are acceptable
  before considering any live enablement. Native delivery success alone does
  not settle the polling or model-cost question.

## 5. Resource warning and sampler failure (interactive run only; 10 minutes)

Exit the prior Claude session first. In the operator terminal derive thresholds
that make available disk space a warning, without filling the disk:

```bash
read -r WARN CRITICAL < <(python3 -B - <<'PY'
import shutil
free = shutil.disk_usage('/System/Volumes/Data').free / 1024**3
assert free > 6, 'insufficient headroom for this canary'
print(free + 5, free - 5)
PY
)
start_canary chrono 1500 "$WARN" "$CRITICAL"
```

Observe at least three 60-second samples. Require one disk-warn transition and
no duplicates for unchanged samples; ack the warning. Record real memory enum
and any `unknown` result without calling it healthy. Re-escalation/recovery are
covered by the deterministic fixture suite; do not generate real resource load.

Exit and launch `start_canary chrono 1500 10 20` (deliberately invalid threshold
pair). Require a sampler fault reason in health, while the waiter remains live.
Observe the persisted failure count and retry deadline: retries follow 5 seconds,
60 seconds, then a 600-second cap. Within about 80 seconds, three faults must
produce one durable `RESOURCE MONITORING OFF` notice. Approve its exact ACK;
subsequent context must still state monitoring is off until a successful sample.
The context warning begins at the third consecutive failure, matching the
durable notice; health reports the first transient failure immediately.
Further faults in the same outage must not enqueue another fault identity.
Enqueue `TASK-CANARY-SAMPLER-FAULT`; it must still arrive. No sequence of tool
hooks should turn this sampler failure into repeated waiter-fault reminders.
The six-process concurrent hook-fault and corrupt-store throttle are fixture
regressions; do not corrupt the canary inbox containing delivery evidence.

## 6. Disable, verify rollback, and retain evidence

While the last canary session is running, snapshot the store to capture exact
worker identities, then disable the scratch flag. The production code signals
no process; cancellation is cooperative. Only the watchdog's private server
shutdown is a forced backstop.

```bash
cp "$R/_state/notifications/store.json" "$S/before-disable.json"
disable_canary
PYTHONPATH="$R/scripts/python" python3 -B - "$S/before-disable.json" <<'PY'
import json, pathlib, sys, time
from chrono_state.notifications import process_start_identity
data = json.loads(pathlib.Path(sys.argv[1]).read_text())
workers = [w for s in data.get('sessions', {}).values() for w in s['workers'].values()]
deadline = time.monotonic() + 2
while True:
    remaining = [w for w in workers if process_start_identity(w['pid']) == w['process_start']]
    if not remaining or time.monotonic() >= deadline:
        break
    time.sleep(0.1)
assert not remaining, f'workers survived rollback: {remaining}'
print('PASS cooperative rollback: captured worker identities exited')
PY
n health > "$S/health-disabled.json"
test ! -e "$LIVE/_state/chrono/notification-hooks.enabled"
test ! -L "$LIVE/_state/chrono/notification-hooks.enabled"
```

Require `consumer_health: disabled`. If the process probe fails, record its
literal error; do not convert unavailable telemetry into “no workers.” The prior
live-worker health snapshot is the positive control for the exit assertion.

Run `/exit` to close Claude. For the flag-absent control use a fresh notice root:

```bash
OFF="$S/flag-absent"
mkdir -p "$OFF"
(cd "$R" && /usr/bin/time -lp env CHRONO_NOTIFICATION_ROOT="$OFF" \
  CLAUDE_PROJECT_DIR="$R" bash "$R/bin/chrono-notification-hook.sh" context \
  </dev/null > "$S/off.stdout" 2> "$S/off.stderr")
test ! -s "$S/off.stdout"
test ! -e "$OFF/_state"
# off.stderr must contain only /usr/bin/time output, no hook diagnostic.
touch "$S/finished"
wait "$WATCHDOG_PID"
test -f "$S/watchdog-done"
printf 'Retained evidence: %s\n' "$S"
```

Never use `pkill -f`, kill an unrecorded PID, or clean shared caches. Keep the
archive, transcripts, store snapshots and cost log for review; deletion is a
separate operator decision. If the canary aborts, `disable_canary` is the first
action. The independent deadline also disables the flag and terminates only the
private server. Record any worker that missed cooperative exit before teardown.

## Acceptance record

Attach both run kinds and evidence directories, the reviewed commit, CLI version,
UTC windows, `/hooks` captures, exact
session IDs/transcript paths, notice IDs/times/acks, before/after health, worker
exit proof, native counters, token/turn totals, and literal command failures.
List each case as PASS, FAIL, or UNMEASURED. All delivery/lifecycle/cost cases
must pass and costs must be accepted by Chrono before a separate live-enablement
decision. This runbook's existence and passing unit tests do not enable hooks.
