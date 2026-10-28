# Guarded security MCPs (retired)

`guarded-semgrep`, `guarded-slither`, and `guarded-solodit` were staged to run
behind the Trail of Bits `mcp-context-protector` wrapper, then retired on
2026-10-27. No lane declares them, and there is nothing to install here.

Run the security CLIs directly instead:

- **Semgrep** and **Slither**: the starting commands are in the
  [security arsenal guide](../tooling/security-arsenal-guide.md).
- **Solodit prior-art lookups** have no CLI replacement. Use a separately
  provisioned prior-art service, or search Solodit and the GitHub advisory
  database by hand, as the `dedup-prior-art-check` skill describes.
