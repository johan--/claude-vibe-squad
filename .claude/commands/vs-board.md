# /vs-board

Render the live Vibe Squad specialist-swarm dashboard once (a single frame:
one card per running spawn, or the idle canvas when nothing is dispatched).

Run:

```bash
root="$(git rev-parse --show-toplevel)"
VAULT_ROOT="$root" python3 "$root"/bin/vs-board-dashboard.py
```

`VAULT_ROOT` is required — `bin/vs-board-dashboard.py` exits immediately without
it. The renderer prints one frame and returns; the continuous animated version is
the tmux dashboard pane (`bin/vs-dashboard-loop.sh`), not this command.

Use this when the operator says `/vs-board`, "show the board", or asks what specialists are running right now.
