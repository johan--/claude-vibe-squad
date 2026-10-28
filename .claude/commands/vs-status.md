# /vs-status

Show current Vibe Squad state from inside Chrono.

Run:

```bash
bash "$(git rev-parse --show-toplevel)"/bin/where-are-we.sh
```

Use this when the operator says `/vs-status`, "where are we", or asks what is active.

Note: this used to be `/status`, which collides with Claude Code's own built-in
`/status`. Vibe Squad's version now lives under `/vs-status`.
