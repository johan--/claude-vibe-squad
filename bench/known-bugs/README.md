# Known-bug smoke bench

Two Solidity contracts with one deliberately planted bug each, plus the assertion that
our existing engines still find them.

```
./scripts/run-known-bug-bench.sh                     # every planted bug must be found  -> exit 0
./scripts/run-known-bug-bench.sh --negative-control  # bugs repaired, every check must stop firing
./bench/known-bugs/selftest.sh                       # does the runner catch a DEAD engine?
```

Exit codes: `0` behaved as expected, `1` an expectation came out wrong, `2` bad usage,
`3` an engine or fixture is missing before anything ran, **`4` an engine ran but did not
produce a usable result**.

## What a green run proves

slither, semgrep and forge execute, are wired to each other and to these sources, and
still detect bug classes we already know how to detect. When a hunt reports "nothing
found", a green bench says the pipeline was alive; a red bench says the result is
worthless.

## What it does not prove

That we would find a **novel** bug. Every bug here is planted, of a class the tools were
built to catch, and each detector is pointed at it. This is a smoke test, not a
capability measurement. It also says nothing about the engines it does not run —
echidna, medusa, halmos, myth and aderyn are all in the arsenal and all unmeasured here.

## The planted bugs

| Contract | Bug | Class | Caught by |
|---|---|---|---|
| `src/ReentrantBank.sol` | ether sent before the balance is zeroed | reentrancy / CEI, SWC-107 | slither `reentrancy-eth`, semgrep `state-write-after-external-call`, forge PoC |
| `src/RoundingVault.sol` | withdrawal burns `floor(assets * shares / total)` | rounding in the caller's favour | forge PoC + fuzz only |

`RoundingVault` is the interesting one. No static tool decides which way a division
should round, so nothing but the dynamic engine catches it — which is the point of
having it here. It also acts as a control on the reentrancy detectors: it contains the
same value-bearing low-level call as `ReentrantBank` with correct ordering, and slither
and semgrep both stay quiet on it.

## Why `negative-control/` exists

A check that cannot fail proves nothing. `negative-control/` holds the repaired form of
each contract; `--negative-control` swaps them in (in a temp dir, never in the tree) and
requires every expectation to stop firing. The default run additionally checks that the
semgrep rule stays silent on the repaired contracts, so a rule that degraded into
"matches any file" is caught in the normal pass.

This was not theoretical. The first version of the runner reported all five expectations
MET even on fully repaired sources, because `check "$id" "$desc" "$(detail...)" "$?"`
evaluates the command substitution first and clobbers `$?`. The negative control is what
caught it.

## Why exit 4 exists: found, not-found, and FAILED are three different facts

The negative control above is necessary and was not sufficient. The second version still
passed with a dead engine: semgrep could not write `$HOME/.semgrep/semgrep.log` in one
sandbox, died before emitting parseable output, and its empty result was scored **twice**
as success -- once as the E2 miss that negative-control mode wants, and once as a quiet,
discriminating rule at V1. Exit 0, "5 of 5 missed", engine broken. The bench's own control
had the failure the bench exists to detect.

So every engine invocation is now judged on three outcomes, and only the first two are ever
scored:

| Outcome | How it is recognised | Effect |
|---|---|---|
| found | valid output containing the finding | MET |
| not-found | valid output, files provably read, and a *negative result about the question being scored* | MISSED |
| **FAILED** | anything else: nonzero status, unparseable output, a non-empty `errors[]`, files it never read, a status that is not a verdict, or a verdict the runner cannot attribute to the question | **exit 4, naming the engine, in both modes** |

## The scoring rule: a status is not a verdict unless it answers the question

Exit 4 above was necessary and *also* not sufficient. Three further rounds each rejected the
wrong **shape** and were each defeated by the next one, because output can be complete, legal
and unanimous and still be about something else:

- a forge status like `Skipped`, meaning the test was never evaluated;
- a status outside forge's enum, which forge could not have serialised;
- two suites reporting the same test with disagreeing verdicts;
- a test whose name merely shares a **prefix** with the one the expectation owns
  (`…_Shadow()` scored as a clean `BENCH PASS` while no owned test ran at all);
- an exact-name `Failure` caused by a **setup revert, a null reason, or another expectation's
  assertion**, which reads as "the exploit stopped reproducing".

Rejecting those one at a time is a denylist, and the next shape always won. The rule that ends
it is: **a status is not a verdict unless the runner can attribute it to the exact question
being scored.** So each forge expectation declares an *expectation ledger* entry naming what it
owns — the exact test key, signature included, and the exact assertion messages that expectation
is allowed to fail on — and each observation normalises to `found`, `not-found` or `unusable`:

| Observation | Normalises to |
|---|---|
| `Success` on the exact owned test | found |
| `Failure` on the exact owned test, blamed on an assertion that expectation owns | not-found |
| zero occurrences of the exact owned name | **unusable** |
| any other status, or a reason the expectation does not own | **unusable** |
| two occurrences that disagree | **unusable** |

Only a single agreed `found`/`not-found` is scored; every `unusable` is exit 4. The ledger's
markers are themselves checked against the test sources *before forge runs*, so a marker the
harness could never emit is named at once instead of quietly making every negative control
uninterpretable. What this does **not** prove is that the marker came from the owned assertion
rather than from other code emitting the same string; exact-match on a unique, per-expectation
message is the strongest attribution forge's JSON supports.

E1 and E2 need no ledger entry because each already has a live positive control for its own
question: E2 is its own control in normal mode, and `V2` re-scans the tracked unrepaired `src/`
in negative-control mode, so renaming the semgrep rule id turns both modes red (exit 1) rather
than green. Slither has no such control — its detector vocabulary is versioned and open — which
is why it gets an explicit registration probe and semgrep does not.

Three more consequences worth knowing before editing the runner:

- **The exit status alone tells you nothing, and differently per engine.** `slither` returns
  255 merely for *having* findings, so it runs with `--fail-none`. `forge test` returns 1
  both when a test legitimately fails -- the entire point of negative-control mode -- and
  when the build collapses, so forge is judged on its output, not its status: valid JSON
  naming every test expected to have run.
- **"Scanned nothing" and "found nothing" look identical**, so each engine must prove it read
  the fixtures: semgrep via `paths.scanned`, slither via the files its detectors reference
  (both contracts carry a low-level call in every variant, which is what makes that a real
  positive control), forge via the test names it actually ran.
- **forge is validated before slither, and that order is load-bearing.** Slither compiles
  through forge here, so checking slither first makes a broken forge abort the run naming
  *slither*. That misattribution was real and `selftest.sh` catches it.

The runner also owns semgrep's log and settings paths instead of inheriting `$HOME/.semgrep`,
which is what made the same commit pass on one machine and fail on another.

## `selftest.sh` -- the regression for all of that

`selftest.sh` is 47 bench runs and takes a few minutes. It shadows one engine at a time
on `PATH` with a stub that stays installed, stays discoverable, and still answers
`--version`, then breaks it in one of four ways:

- `crash` — nonzero exit, no output.
- `silent` — exit 0 with well-formed but **empty** output; the shape that caused the false pass.
- `version` — fails only the version probe and otherwise delegates to the real binary, so
  the run would go green if the banner's probe did not abort.
- `malformed` — exit 0, parseable, carrying every piece of coverage evidence the validators
  check, and missing exactly one field a predicate dereferences.

Two further families are not stub flavours, because nothing about their *shape* is wrong.
The **wrong-value** cases are complete and structurally perfect and wrong only in what a
field says: a `Skipped` or out-of-enum forge status, two suites disagreeing about one test,
a semgrep rule the scan declined to run, a slither detector this build does not register.
The **wrong-question** cases are wrong in neither shape nor value — unanimous, enum-legal,
fully-populated output that answers something other than the expectation being scored:
`unrelated` (blamed on a setup revert), `noreason` (no failure reason at all),
`crossmarker` (another expectation's assertion message), and `shadow` (sibling names
sharing every prefix character with the owned tests).

Every case is asserted in both modes and — for everything that gets far enough for the
scoreboard to be at risk — asserted to emit **zero** scoring lines, because an abort that
still prints a scoreboard has published half a verdict on a toolchain it just said it could
not judge. Alongside those: a read-only `$HOME` case, a stale-ledger case that edits an
owned assertion message and requires the runner to name it, and two positive controls
proving the unmodified runner still passes both modes — without those, a runner that always
exited 4 would score a perfect self-test.

## Pins

solc 0.8.28, evm_version cancun, optimizer off, fuzz seed `0x1120`, no libraries. There
are no external dependencies on purpose: forge-std would be an unpinned network fetch,
and the bench should measure our engines, not our ability to reach GitHub.

No numeric version floor is asserted for the engines. The runner depends on specific
flags and JSON shapes rather than on version strings, and it checks those directly -- an
engine too old to satisfy them fails by name at exit 4. A floor invented without a host
old enough to test it would only manufacture false failures. The banner prints each
engine's resolved path and version so two hosts that disagree about a run can diff them.
