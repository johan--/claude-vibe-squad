# /vs-lanes

Show the compact per-lane status the tmux status bar renders (chrono, claude,
gpt-codex, gemini, grok), read-only.

Run:

```bash
dir="${VIBESQUAD_STATUS_DIR:-/tmp}"
found=0
for f in "$dir"/vs-lane-*.status; do
  [ -e "$f" ] || continue
  found=1
  lane="$(basename "$f" .status | sed 's/^vs-lane-//')"
  printf '%-10s %s\n' "$lane" "$(sed 's/#\[[^]]*\]//g' "$f")"
done
if [ "$found" -eq 1 ]; then
  [ -e "$dir/vs-swarm.status" ]  && printf '%-10s %s\n' "swarm"  "$(sed 's/#\[[^]]*\]//g' "$dir/vs-swarm.status")"
  [ -e "$dir/vs-daemon.status" ] && printf '%-10s %s\n' "daemon" "$(sed 's/#\[[^]]*\]//g' "$dir/vs-daemon.status")"
else
  echo "No live lane status — the squad status poller is not running."
  echo "Start the squad with 'squad up', or see /vs-board for live spawns."
fi
```

This reads the status files the poller (`bin/vs-lane-status.sh`) already
publishes, stripping tmux colour markup. It does NOT run the poller itself:
`bin/vs-lane-status.sh` is a background daemon (infinite loop, needs
`VIBESQUAD_DAEMON_TOKEN`) and cannot be invoked as a one-shot command.

Use this when the operator says `/vs-lanes`, "what's each lane on", or asks which model lanes are busy.
