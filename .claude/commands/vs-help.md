# /vs-help

List the Vibe Squad slash commands and shell verbs.

Vibe Squad's own commands are namespaced `/vs-*` so they never clash with Claude
Code's built-ins. Print this reference:

```bash
cat <<'EOF'
Vibe Squad slash commands (type inside the Chrono session):
  /vs-status   where are we — doctor verdict, active registry, mailbox, tmux state
  /vs-stop     gracefully close the squad tmux session
  /vs-help     this list
  /vs-board    render the live specialist-swarm dashboard (one frame)
  /vs-queue    what is waiting on the operator, then live / in-flight tasks
  /vs-lanes    compact per-lane status from the running status poller
  /vs-doctor   health check + runtime diagnostics

Shell verbs (type in a terminal, not the Chrono chat):
  squad            launch / attach the squad tmux session (alias: squad up)
  squad status     print the where-are-we summary without attaching
  squad doctor     health check + MCP/runtime audit (squad doctor --deep for more)
  squad stop       stop the daemon, update live state, then kill the session
  vs               shell alias for bin/connect.sh — attach straight into the
                   chrono pane (add: alias vs='bash ~/Obsidian-Claude-Vibe-Squad/bin/connect.sh')

Note: /status and /stop are NOT Vibe Squad commands. /status is Claude Code's
own built-in; use /vs-status and /vs-stop for the squad.
EOF
```

Use this when the operator says `/vs-help`, "what commands are there", or asks how to drive the squad.
