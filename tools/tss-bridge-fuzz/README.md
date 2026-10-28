# tss-bridge-fuzz

A reusable fuzzing rig for **TSS-signed Solana bridges**, built on
[Mollusk](https://github.com/anza-xyz/mollusk) (Anza, `mollusk-svm`).

## The premise

A coverage fuzzer pointed at a bridge **without a signing oracle spends its
entire budget bouncing off signature verification.** Every mutated payload fails
at the auth check, so the interesting region — the post-signature state space,
where replay guards, gas accounting, PDA lifecycle and CPI scope live — is never
entered.

This rig **holds the TSS key.** It mints the signature a legitimate relayer
would have carried, then mutates everything after it.

Holding the key is a lab device, not a claimed capability. The rig also keeps a
role-less identity that never holds a role, and a built-in forged-signature
negative control, so any claim about what an unprivileged actor can do is made
*with* the unprivileged actor.

## Why Mollusk

| | Mollusk | LiteSVM | `solana-test-validator` |
|---|---|---|---|
| Transaction dedup | none | **`AlreadyProcessed`** | yes |
| Setup cost per call | instruction only | tx + bank | RPC round trip |
| Runs in-sandbox | yes | yes | **no** — faucet cannot bind |
| `cargo-fuzz` + `arbitrary` | documented route | workable | no |

The dedup row is the one that bites. Under LiteSVM two byte-identical calls make
the *runtime* return `AlreadyProcessed` — a harness that reads that as "the
program's replay guard held" is reporting a guard it never tested. Mollusk has
no `Bank`, so every call reaches the program and you get the program's own
answer.

## Retargeting — what you must change

**One file: a `TargetSpec` impl.** Nothing in `rig/` knows about any particular
bridge. The public workspace contains the reusable `rig` library only; target
adapters, smoke binaries, canaries, and campaign entry points are private.
Create a separate crate implementing `rig/src/spec.rs` for your authorized target.

```
rig/                        target-agnostic — DO NOT edit to retarget
  spec.rs                   the TargetSpec trait + Arbitrary FuzzCall/FuzzSession
  tss.rs                    secp256k1 signing oracle + forged-signature control
  signers.rs                AccountsStorage<Keypair> — the stateful signer pool
  world.rs                  account store, fuzzer-addressable roster, rent model
  engine.rs                 Mollusk driver, state commit, invariant checks
  ledger.rs                 outcome accounting + the non-vacuity gate
  buf.rs                    borsh/Anchor byte builder
```

To point the rig at a new bridge, implement these on your spec:

| Method | What it is |
|---|---|
| `program_id()` | the `declare_id!` value — **not** the keypair `anchor build` generates |
| `elf()` | raw `.so` bytes |
| `kinds()` | human names for each instruction, in index order |
| `tss_gated()` | per kind: does it verify a TSS signature? |
| `register_signers()` | the identities, and which ones the seeded state gives a role |
| `seed()` | config/PDA/oracle accounts + the fuzzer-addressable roster |
| `build()` | render a `FuzzCall` into an `Instruction` |
| `invariants()` | properties re-checked after every successful call |

### The three things that will cost you a day if you skip them

1. **`tss_preimage` must match the on-chain verifier byte for byte.** Get it
   wrong and every authorized call returns the auth error — which is
   indistinguishable from having no oracle at all. The smoke run's
   *authorized-success count* is the only proof the layout is right.
2. **Seed executable program accounts** for every program your instructions
   reference — system program, SPL token, and the target's own id (Anchor's
   `Option<Account>` ABI encodes `None` as "this slot holds the program id").
   Without them the runtime substitutes a non-executable stub and you get
   Anchor `3009 InvalidProgramExecutable` / runtime `UnsupportedProgramId`
   before any target logic runs.
3. **Bias the obvious kill-fields, keep a flag to unbias them.** A raw `i32`
   deadline delta is in the past half the time and burns the call on
   `SignatureExpired`. The adapter defaults to a valid window and restores the
   raw value under a flag bit, so expiry stays fuzzable without dominating.

## Build the reusable library

From this directory:

```bash
cargo build --release -p tss-bridge-fuzz-rig
```

A target run additionally needs your own adapter, compiled program ELF, and
smoke or fuzz entry point. Load the ELF at the program's declared ID and verify
an authorized call succeeds before measuring coverage. The public library does
not provide a ready-to-run target campaign.

## The non-vacuity gate

A harness that rejects every input at authentication has not exercised the
post-signature state space. A meaningful run must report:

* how many calls **SUCCEEDED** (not how many ran),
* how many **distinct instruction kinds** were reached — `covered N of M`,
* the split between calls carrying a **real** signature and a **forged** one.

`Vacuous` (0 successes) and `ORACLE DEAD` (successes, but none on a TSS-gated
instruction) both exit non-zero. `Partial` means some kinds were never reached
and is reported, not hidden.

**The false twin.** Every smoke run replays the same session with every
signature forged. If the armed and forged runs succeed equally often on gated
instructions, the depth is not coming from the oracle and the run says nothing.
This is the positive control for the harness itself.

## What counts as a lead

The fuzz target panics — so libFuzzer preserves the input — on:

* an **invariant violation** declared by the spec, or
* an **unauthorized call succeeding on a TSS-gated instruction** (`auth_bypasses`).

Both are leads, not findings. A lead becomes a finding only after a PoC
reproduces it with a `msg.sender`-equivalent swap control: the harness holds the
TSS key, so anything reproduced *with* the oracle armed must be re-shown with a
role-less actor before it means anything about an attacker.
