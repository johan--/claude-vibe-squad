# /vs-queue

Show what is waiting on the operator, then the live / in-flight board tasks.

Run:

```bash
root="$(git rev-parse --show-toplevel)"
awaiting="$root/_state/chrono/awaiting-operator.md"
if [ -f "$awaiting" ]; then
  echo "== Awaiting operator =="
  cat "$awaiting"
  echo
fi
echo "== Live tasks (dispatched / in-flight / review-required) =="
bash "$root"/bin/chrono-resume-capsule.sh --stdout \
  | awk '/^## Live tasks/{p=1;print;next} p&&/^## /{exit} p'
```

`--stdout` regenerates the capsule fresh and prints it without rewriting
`_state/chrono/resume.md` (that file is Chrono's session-resume contract to
manage, so this viewer leaves it alone).

Note: the capsule has no section literally named "Pending completions"; the
section that lists work awaiting completion is `## Live tasks (dispatched /
in-flight / review-required)`, which is what this extracts.

Use this when the operator says `/vs-queue`, "what's pending", or asks what needs their input or is still running.
