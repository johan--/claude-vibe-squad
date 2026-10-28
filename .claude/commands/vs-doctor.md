# /vs-doctor

Run the Vibe Squad health check + runtime diagnostics (the same fast pre-flight
`squad up` gates on).

Run:

```bash
bash "$(git rev-parse --show-toplevel)"/bin/squad doctor
```

This is the FAST health check: CLI presence + auth, MCP reachability, vault root,
disk, tmux window composition, dispatch volume, and the notification spine. It is
diagnostic — it verifies and reports, and never mutates board or mailbox state.
It does write its own daily health report under
`_state/.../doctor-logs/<date>.md` (that report is the check's output, and what
`/vs-status` reads back). For the costlier checks the fast path defers, run
`squad doctor --deep` in a terminal.

Use this when the operator says `/vs-doctor`, "is everything healthy", or asks to run diagnostics.
