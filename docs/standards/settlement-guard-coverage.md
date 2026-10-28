# Settlement guard coverage

This census maps the five settlement functions in
`scripts/python/registry_reconciler.py` to their test evidence.
`scripts/python/tests/test_review_enforcement.py` owns the stated **73 tests**.
`scripts/python/tests/test_guard_census_is_current.py` checks the guard messages,
row counts, evidence totals, and review-test count against the source AST.
It does not rerun the mutation experiments or certify current behavioral coverage.

The evidence classifications below are a snapshot from the 2026-09-01 sweep,
with the review-test count recounted on 2026-09-07. Re-measure the evidence after
changing either source owner. Private runtime measurements are not published.

Scope: every `raise ValueError` in `_validate_factual_attestation`,
`_validate_security_review`, `require_approval_verdict`,
`_validate_standard_review`, and `settle_review`: 36 guards.

## Interpreting the evidence

This is a lower bound based on guard-message assertions. “Uncovered” means no
test was identified naming that message, not that the behavior is unguarded.
Only mutation evidence demonstrates that removing a guard breaks a test.
A failing test can pin a diagnostic while another guard still prevents settlement;
those rows are marked “backstopped.” Do not quote the summary without its basis.

## How each row was established

| Basis | Rows | What it means |
|---|---|---|
| **mutation (this sweep)** | 17 | The guard was deleted in a scratch copy of the reconciler and the test was *observed to fail* against that mutant. |
| **mutation (earlier sweep, not re-verified)** | 3 | The three `must be cross-family` clauses. Their test docstrings claim mutation measurement on 2026-08-26. **This sweep did not re-verify those claims.** Treat as asserted, not confirmed. |
| **assertion text** | 10 | A test asserts the guard's message. Never mutation-proven; the test may pass for another reason. |
| **absence** | 5 | No test anywhere names the message. The weakest basis in this table — see the lower-bound warning. |
| **prose-only** | 1 | The message appears in a module docstring, without an assertion; see the `settle_review` row. |

## Summary

| Class | Count |
|---|---|
| Genuinely controlled | 30 |
| Prose-only — the message exists in the test tree, no test asserts it | 1 |
| Uncovered — no test names it | 5 |

## `_validate_factual_attestation` — 8 of 9 controlled

| Guard | Class | Basis |
|---|---|---|
| `must be authored by from: chrono` | controlled | mutation (this sweep) |
| `requires type: REVIEW_ATTESTATION` | controlled | mutation (this sweep) |
| `must echo review_class: factual` | controlled | mutation (this sweep) |
| `must target the held task` | controlled | mutation (this sweep) |
| `status must be complete` | controlled | mutation (this sweep) |
| `reviewer_lane must match review_model` | **uncovered** | absence |
| `has invalid reviewer_family` | controlled | mutation (this sweep) |
| `must be cross-family` | controlled | mutation (earlier sweep, not re-verified) |
| `response hash mismatch` | controlled | mutation (this sweep) |

## `_validate_security_review` — 5 of 7 controlled

| Guard | Class | Basis |
|---|---|---|
| `tasks require an independent lane review` | controlled | assertion text — `bin/review-loop-guard-selftest.py:201`, not the unittest suite |
| `must target the held task` | **uncovered** | absence |
| `must come from the configured review_model` | controlled | mutation (this sweep) |
| `has invalid reviewer family` | controlled — real gate on one half only | mutation (this sweep) |
| `must be cross-family` | controlled | mutation (earlier sweep, not re-verified) |
| `response hash mismatch` | controlled | mutation (this sweep) |
| `status must be complete` | **uncovered** | absence |

## `require_approval_verdict` — 1 of 1 controlled

| Guard | Class | Basis |
|---|---|---|
| `verdict must be exactly APPROVE; observed …` | controlled | assertion text (message head at L1018; observed-value tail at L409, L1051, L1119) |

## `_validate_standard_review` — 10 of 10 controlled

| Guard | Class | Basis |
|---|---|---|
| `requires an independent lane response` | controlled — **backstopped** | mutation (this sweep); mutant still refuses via `must be a RESULT response` |
| `must be a RESULT response` | controlled | mutation (this sweep) |
| `registry provenance targets a different held task` | controlled | mutation (this sweep) |
| `response reviews conflicts with registry provenance` | controlled | assertion text — `test_b4:456` |
| `response is missing from` | controlled — **backstopped** | mutation (this sweep); another guard still refuses |
| `response from conflicts with registry reviewer lane` | controlled | mutation (this sweep) |
| `must come from the configured review_model` | controlled | mutation (this sweep) |
| `reviewer_family conflicts with registry reviewer lane` | controlled | assertion text — `test_b4:460` |
| `must be cross-family` | controlled | mutation (earlier sweep, not re-verified) |
| `status must be complete or needs_review` | controlled | mutation (this sweep) |

## `settle_review` — 6 of 9 controlled

| Guard | Class | Basis |
|---|---|---|
| `unknown registry task` | controlled | assertion text — `test_registry_batch_close.py:158` |
| `already settled with a different review ref` | controlled | assertion text (L969) |
| `task is not {REVIEW_REQUIRED} or needs_review` | controlled | assertion text (L345, L893) |
| `task has no landed response` | **prose-only** | module docstring, `test_registry_reconciler_repair_envelope.py:8` |
| `does not match dispatched capability snapshot` | controlled | assertion text — `test_capability_dispatch_integrity.py:720` |
| `does not match dispatched worker fence` | **uncovered** | absence |
| `task response status cannot be settled` | controlled | assertion text (L904) |
| `--review-ref must not be the task's own response` | controlled | assertion text (L922) |
| `task does not require cross-family settlement` | **uncovered** | absence |

## Coverage method

1. Extract guards from the AST, including f-strings (`JoinedStr`), and anchor
   each row to its owner function and a distinctive message fragment.
2. Search `scripts/python/tests/**/*.py` and `bin/review-loop-guard-selftest.py`.
   Compare test string literals against guard messages in both directions;
   assertions can name middle fragments or use table-driven variables.
3. Read the assertion and its fixture. Distinguish positive message assertions
   from `assertNotIn`, comments, docstrings, and assertions about adjacent guards.
   A shared fragment does not identify which validator path was exercised.
4. For mutation evidence, remove one guard in a scratch copy of the Python tree
   and run its test against that copy. Never modify the live reconciler.
   Preserve a passing unmodified control and record the mutant's actual result.
5. Separate a changed diagnostic from improper settlement. A test failure alone
   does not show that the removed guard was the only barrier: check whether the
   mutant returns success and closes a task that should remain held.
6. Reconcile every source guard to exactly one documented row, derive totals
   from those rows, and run the census validator before accepting the update.

Use guard messages to locate code; line numbers and a document's own commit
history are not a reliable index into an evolving implementation.

```sh
python3 -m unittest discover -s scripts/python/tests -p test_guard_census_is_current.py
```
