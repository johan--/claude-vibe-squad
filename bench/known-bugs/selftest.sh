#!/usr/bin/env bash
# Does the bench runner tell a dead engine apart from a clean result?
#
# The bench exists to catch a silent toolchain, so the one failure it must never
# have is its own. Twice now the runner has scored a broken engine as a pass: once
# through a clobbered `$?`, once by reading an engine that died before emitting
# parseable output as both a satisfied negative control and a quiet, discriminating
# rule. This file is the regression for the second one.
#
#   ./bench/known-bugs/selftest.sh          # 47 bench runs, a few minutes
#   ./bench/known-bugs/selftest.sh -v       # also print each failing run's output
#
# Method: shadow one engine on PATH with a stub, so it stays installed, stays
# discoverable by `command -v`, and still answers `--version` -- then fails at the
# moment it is asked to do work. Preflight cannot catch that; only per-invocation
# exit-status and output validation can. Two stub flavours, because they fail in
# opposite directions:
#
#   crash   nonzero exit, no output           -- how a real broken semgrep looked
#   silent  exit 0, well-formed but EMPTY output -- indistinguishable from "found
#           nothing" unless the runner checks which files were actually read.
#           This is the shape that produced the false pass.
#   version fails ONLY on --version and otherwise delegates to the real binary, so
#           the run would go green if the banner's probe did not abort. Regression
#           for an abort that fired inside a command substitution and therefore
#           killed only that subshell while the bench carried on.
#   malformed exit 0, parseable, and CARRYING the coverage evidence the validators
#           check -- but missing one field an expectation predicate dereferences.
#           The third false pass, and the subtlest: coverage and exit status both
#           look right, so the only thing standing between it and a verdict is
#           whether the validators check field types as well as presence. Measured
#           unfixed: --negative-control returned 0 with "NEGATIVE CONTROL PASS" and
#           five of five expectations "missed", no engine named. This flavour is
#           therefore also asserted to emit ZERO scoring lines -- an abort that
#           still prints a scoreboard has published half a verdict on a toolchain
#           it just said it could not judge.
#
# A fifth family follows the four flavours, and it is not a flavour because nothing
# about the SHAPE is wrong. Those cases emit a complete, structurally perfect result
# and are wrong only in a VALUE: a forge status that means the test never ran, a
# status outside the enum forge can emit, two suites disagreeing about the same
# test, a semgrep rule the scan declined to evaluate, a slither detector this build
# does not register. Presence and type checks are blind to every one of them. The
# fourth false pass, and the reason a validator has to check what a field SAYS and
# not just that it is there.
#
# A SIXTH family is wrong in neither shape nor value. Every field is present, every
# status is a legal member of forge's enum, every occurrence agrees -- and the
# result still answers a different question than the one being scored: a test whose
# name merely shares a prefix with the one the expectation owns, or an exact-name
# failure caused by a setup revert, a null reason, or another expectation's
# assertion. Each reads as "the exploit stopped reproducing". Value checks are blind
# to all of them, because the value is fine; only attribution to the owned question
# separates them from a real negative result. The fifth false pass, and the reason
# the runner keeps an expectation ledger instead of a list of test-name prefixes.
#
# Every mutation is asserted in BOTH modes, because negative-control mode is where
# a miscounted engine failure reads as success.
#
# Exit 0 = the runner discriminates correctly. Non-zero = it does not; do not trust
# any bench result until this is green.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
BENCH="$REPO_ROOT/scripts/run-known-bug-bench.sh"
VERBOSE=0
[[ "${1:-}" == "-v" || "${1:-}" == "--verbose" ]] && VERBOSE=1

[[ -x "$BENCH" ]] || { echo "selftest: no runner at $BENCH"; exit 3; }

WORK="$(mktemp -d /tmp/bench-selftest-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0

# Well-formed but empty output, per engine. Each parses, each says "I found
# nothing", and none of them proves any file was ever read.
silent_payload() {
    case "$1" in
        slither) echo '{"success": true, "error": null, "results": {"detectors": []}}' ;;
        semgrep) echo '{"errors": [], "results": [], "paths": {"scanned": []}}' ;;
        forge)   echo '{}' ;;
    esac
}

# Structurally unusable but coverage-bearing. Each parses, each satisfies every
# shape and coverage check the runner makes (success flag, empty errors[], the
# fixtures listed as read, the expected tests listed as run), and each omits
# exactly ONE field an expectation predicate dereferences: slither check,
# semgrep check_id, forge status. A presence-blind validator waves these through
# and the KeyError lands inside a predicate, where it is indistinguishable from
# the finding being absent.
malformed_payload() {
    case "$1" in
        slither) echo '{"success": true, "error": null, "results": {"detectors": [{"description": "ReentrantBank.withdraw() sends eth then writes state", "elements": [{"source_mapping": {"filename_relative": "src/ReentrantBank.sol"}}, {"source_mapping": {"filename_relative": "src/RoundingVault.sol"}}]}]}}' ;;
        semgrep) echo '{"errors": [], "results": [{"path": "src/ReentrantBank.sol"}], "paths": {"scanned": ["src/ReentrantBank.sol", "src/RoundingVault.sol", "negative-control/ReentrantBank.sol", "negative-control/RoundingVault.sol"]}}' ;;
        forge)   echo '{"test/Exploits.t.sol:Exploits": {"test_results": {"test_Exploit_ReentrancyDrainsOtherDepositors()": {}, "test_Exploit_FreeAssetsAtZeroShareCost()": {}, "testFuzz_BurnIsShortOfWhatIsOwed(uint96)": {}}}}' ;;
    esac
}

# A complete forge result for this corpus: both suites, every per-test key a real
# `forge test --json` emits, statuses placeheld. Nothing is missing from it -- which
# is the whole point, because the structural validators cannot see anything wrong.
FORGE_RESULT_TEMPLATE='{"test/Exploits.t.sol:ReentrancyExploitTest":{"duration":"6ms 176us 583ns","test_results":{"test_Exploit_ReentrancyDrainsOtherDepositors()":{"breakpoints":{},"counterexample":null,"decoded_logs":[],"duration":"2ms 679us 500ns","gas_snapshots":{},"kind":{"Unit":{"gas":727496}},"labeled_addresses":{},"logs":[],"reason":__REASON_E3__,"status":"__STATUS__","traces":[]}},"warnings":[]},"test/Exploits.t.sol:RoundingExploitTest":{"duration":"6ms 176us 583ns","test_results":{"testFuzz_BurnIsShortOfWhatIsOwed(uint96)":{"breakpoints":{},"counterexample":null,"decoded_logs":[],"duration":"2ms 679us 500ns","gas_snapshots":{},"kind":{"Fuzz":{"failed_corpus_replays":0,"first_case":{"calldata":"0xb1c108d800000000000000000000000000000000000000000000000000000000000001f4","gas":716610,"stipend":21216},"mean_gas":695394,"median_gas":695394,"runs":256,"traces":[]}},"labeled_addresses":{},"logs":[],"reason":__REASON_E5__,"status":"__STATUS__","traces":[]},"test_Exploit_FreeAssetsAtZeroShareCost()":{"breakpoints":{},"counterexample":null,"decoded_logs":[],"duration":"2ms 679us 500ns","gas_snapshots":{},"kind":{"Unit":{"gas":1392275}},"labeled_addresses":{},"logs":[],"reason":__REASON_E4__,"status":"__STATUS__","traces":[]}},"warnings":[]}}'

# forge_status_payload <Skipped|Corrupt|conflict>
#
#   Skipped   a LEGITIMATE forge status meaning the test was never evaluated. A
#             bench that scores it as a miss is saying "the exploit stopped
#             reproducing" about a test that never ran.
#   Corrupt   a string outside forge's status enum, which forge cannot emit at all.
#   conflict  ONLY legal, evaluated statuses -- but the same expected test comes
#             back Failure from one suite and Success from another. A whitelist over
#             the enum passes this one, so it is the case that proves the boundary
#             is a RULE and not a denylist of the payloads we happen to have seen.
#             The predicate takes whichever suite it reaches first and scores the
#             other out of existence. Realistic rather than contrived: one base test
#             contract inherited by two concrete ones emits exactly this shape.
#
# Measured before the fix, all three: --negative-control exited 0 with NEGATIVE
# CONTROL PASS and five of five expectations "missed", naming no engine -- and in
# the conflict case its own output said the exploit still reproduced.
# The assertion messages test/Exploits.t.sol actually fails on, and which the
# runner's expectation ledger declares each expectation owns. Kept here so a shape
# can be wrong in exactly ONE way: an ambiguity case has to carry an attributable
# reason, or it gets rejected for the reason and never exercises the ambiguity rule.
M_E3='EXPECTATION MISSED: withdraw() did not re-enter'
M_E4='EXPECTATION MISSED: withdrawals burned shares'
M_E5='EXPECTATION MISSED: burn path already rounds up'

owned_reasons() { sed -e "s|__REASON_E3__|\"$M_E3\"|" -e "s|__REASON_E4__|\"$M_E4\"|" -e "s|__REASON_E5__|\"$M_E5\"|"; }
null_reasons()  { sed -e 's/__REASON_E[0-9]*__/null/g'; }

forge_status_payload() {
    if [[ "$1" != "conflict" ]]; then
        sed "s/__STATUS__/$1/g" <<<"$FORGE_RESULT_TEMPLATE" | null_reasons
        return
    fi
    local a b
    a="$(sed -e 's/__STATUS__/Failure/g' -e 's|\(Exploits\.t\.sol:[A-Za-z]*\)"|\1#a"|g' <<<"$FORGE_RESULT_TEMPLATE" | owned_reasons)"
    b="$(sed -e 's/__STATUS__/Success/g' -e 's|\(Exploits\.t\.sol:[A-Za-z]*\)"|\1#b"|g' <<<"$FORGE_RESULT_TEMPLATE" | null_reasons)"
    printf '{%s,%s}\n' "${a:1:${#a}-2}" "${b:1:${#b}-2}"
}

# forge_attribution_payload <unrelated|noreason|crossmarker|shadow>
#
# The sixth family: nothing is missing and no value is illegal. Each of these is a
# complete, unanimous, enum-legal forge result that answers a DIFFERENT question
# than the one being scored, so every shape and value check above waves it through.
#
#   unrelated   exact names, unanimous Failure, blamed on a setup revert rather than
#               on the assertion the expectation owns. Measured unfixed: normal mode
#               called it an ordinary bench miss, --negative-control exited 0 with
#               NEGATIVE CONTROL PASS.
#   noreason    the same, with no reason at all -- forge never says what failed, so
#               nothing can attribute the failure to the question.
#   crossmarker every reason IS a real owned assertion message, but E3 carries E5's.
#               A check for "does this look like one of our markers" passes it; only
#               per-expectation ownership rejects it.
#   shadow      healthy statuses under sibling names that share every prefix
#               character with the owned tests. Measured unfixed: a clean BENCH PASS,
#               exit 0, while not one owned test had run.
forge_attribution_payload() {
    case "$1" in
        unrelated)
            sed 's/__STATUS__/Failure/g' <<<"$FORGE_RESULT_TEMPLATE" \
                | sed 's|__REASON_E[0-9]*__|"selftest: setUp reverted before the assertion ran"|g' ;;
        noreason)
            sed 's/__STATUS__/Failure/g' <<<"$FORGE_RESULT_TEMPLATE" | null_reasons ;;
        crossmarker)
            sed 's/__STATUS__/Failure/g' <<<"$FORGE_RESULT_TEMPLATE" \
                | sed -e "s|__REASON_E3__|\"$M_E5\"|" -e "s|__REASON_E4__|\"$M_E4\"|" -e "s|__REASON_E5__|\"$M_E5\"|" ;;
        shadow)
            # Only test names are `test...(` in this payload: "test_results" is
            # followed by a quote and the suite paths by a slash, so neither matches.
            sed -e 's/__STATUS__/Success/g' -e 's/\(test[A-Za-z_]*\)(/\1_Shadow(/g' <<<"$FORGE_RESULT_TEMPLATE" \
                | null_reasons ;;
    esac
}

# A semgrep scan that read every fixture, reported no errors, found nothing -- and
# declined to run the rule. Coverage is perfect, so only a check on skipped_rules
# tells this apart from a rule that ran and correctly matched nothing.
SEMGREP_SKIPPED_RULES_PAYLOAD='{"errors": [], "results": [], "skipped_rules": [{"rule_id": "semgrep.state-write-after-external-call", "details": "selftest: rule declined"}], "paths": {"scanned": ["src/ReentrantBank.sol", "src/RoundingVault.sol", "negative-control/ReentrantBank.sol", "negative-control/RoundingVault.sol"]}}'

# A slither that scans perfectly well but does not register the detector E1 names.
# E1 then cannot fire, and negative-control mode reads that miss as the bug being
# repaired -- the forge status defect reached through a different door.
SLITHER_DETECTORS_PAYLOAD='[{"check": "low-level-calls", "impact": "Informational", "confidence": "High"}]'

# make_value_shim <engine> <label> <match-arg> <payload> -> echoes a PATH directory
#
# Shadows ONE invocation -- the one whose stdout the bench parses -- and delegates
# every other call to the real binary, for the same reason make_shim does: slither
# compiles through forge, and a stub that answers every call breaks the wrong engine.
make_value_shim() {
    local engine="$1" label="$2" match="$3" payload="$4"
    local dir="$WORK/vshim-$engine-$label"
    local real; real="$(command -v "$engine")"
    mkdir -p "$dir"
    {
        echo '#!/bin/sh'
        echo "# selftest value-stub for $engine ($label)."
        echo 'for a in "$@"; do'
        echo "  [ \"\$a\" = \"--version\" ] && { echo '9.9.9-selftest-stub'; exit 0; }"
        echo 'done'
        echo 'mangle=0'
        if [[ "$engine" == "forge" ]]; then
            echo 'has_test=0; has_json=0'
            echo 'for a in "$@"; do'
            echo '  [ "$a" = "test" ] && has_test=1'
            echo '  [ "$a" = "--json" ] && has_json=1'
            echo 'done'
            echo '[ $has_test -eq 1 ] && [ $has_json -eq 1 ] && mangle=1'
        else
            echo "for a in \"\$@\"; do [ \"\$a\" = \"$match\" ] && mangle=1; done"
        fi
        echo 'if [ $mangle -eq 1 ]; then'
        printf "cat <<'JSON'\n%s\nJSON\n" "$payload"
        echo '  exit 0'
        echo 'fi'
        echo "exec $real \"\$@\""
    } > "$dir/$engine"
    chmod +x "$dir/$engine"
    echo "$dir"
}

# make_shim <engine> <crash|silent|version|malformed> -> echoes a directory to prepend to PATH
make_shim() {
    local engine="$1" flavour="$2"
    local dir="$WORK/shim-$engine-$flavour"
    local real; real="$(command -v "$engine")"
    mkdir -p "$dir"
    {
        echo '#!/bin/sh'
        echo "# selftest stub for $engine ($flavour flavour)."
        if [[ "$flavour" == "malformed" ]]; then
            echo '# Mangles ONLY the invocation whose stdout the bench parses, and hands'
            echo '# every other invocation to the real binary. That delegation is'
            echo '# load-bearing for forge: slither compiles THROUGH forge, so a stub that'
            echo '# answers every forge call breaks slither instead and the bench aborts'
            echo '# naming the wrong engine -- which is a different bug, not this one.'
            echo 'for a in "$@"; do'
            echo '  [ "$a" = "--version" ] && { echo "9.9.9-selftest-stub"; exit 0; }'
            echo 'done'
            echo 'mangle=0'
            if [[ "$engine" == "forge" ]]; then
                echo 'has_test=0; has_json=0'
                echo 'for a in "$@"; do'
                echo '  [ "$a" = "test" ] && has_test=1'
                echo '  [ "$a" = "--json" ] && has_json=1'
                echo 'done'
                echo '[ $has_test -eq 1 ] && [ $has_json -eq 1 ] && mangle=1'
            else
                echo 'for a in "$@"; do [ "$a" = "--json" ] && mangle=1; done'
            fi
            echo 'if [ $mangle -eq 1 ]; then'
            printf "cat <<'JSON'\n%s\nJSON\n" "$(malformed_payload "$engine")"
            echo '  exit 0'
            echo 'fi'
            echo "exec $real \"\$@\""
        elif [[ "$flavour" == "version" ]]; then
            echo '# Breaks the version probe only, then hands the real work to the real'
            echo '# binary -- so this run goes GREEN unless the probe aborts the bench.'
            echo 'for a in "$@"; do'
            echo "  [ \"\$a\" = \"--version\" ] && { echo 'selftest: simulated $engine version-probe failure' >&2; exit 1; }"
            echo 'done'
            echo "exec $real \"\$@\""
        else
            echo '# Installed, discoverable, answers --version, cannot do the work.'
            echo 'for a in "$@"; do'
            echo '  [ "$a" = "--version" ] && { echo "9.9.9-selftest-stub"; exit 0; }'
            echo 'done'
            if [[ "$flavour" == "crash" ]]; then
                echo "echo 'selftest: simulated $engine failure' >&2"
                echo 'exit 1'
            else
                printf "cat <<'JSON'\n%s\nJSON\n" "$(silent_payload "$engine")"
                echo 'exit 0'
            fi
        fi
    } > "$dir/$engine"
    chmod +x "$dir/$engine"
    echo "$dir"
}

# Every line on which the bench publishes a verdict about the toolchain: a scored
# expectation, a vacuity-guard result, or a summary. An abort must emit NONE of
# them -- the runner says "nothing below this point was scored", and this is what
# holds it to that.
SCORING_RE='^[[:space:]]+(MET|MISSED|OK|BROKEN)[[:space:]]|^(RESULT:|NEGATIVE CONTROL:|BENCH )'

# run_case <label> <expected-exit> <expected-substring> <forbidden-ERE|""> <shim|""> [bench args...]
#
# forbidden-ERE, when non-empty, must match no line of the output.
run_case() {
    local label="$1" want_rc="$2" want_txt="$3" forbid="$4" shim="$5"; shift 5
    local out rc
    if [[ -n "$shim" ]]; then
        out="$(PATH="$shim:$PATH" "$BENCH" "$@" 2>&1)"; rc=$?
    else
        out="$("$BENCH" "$@" 2>&1)"; rc=$?
    fi
    local ok=1 why=""
    [[ $rc -eq $want_rc ]] || { ok=0; why="exit $rc, wanted $want_rc"; }
    grep -qF -- "$want_txt" <<<"$out" || { ok=0; why="${why:+$why; }did NOT say \"$want_txt\""; }
    if [[ -n "$forbid" ]] && grep -qE -- "$forbid" <<<"$out"; then
        ok=0
        why="${why:+$why; }scored anyway: $(grep -m1 -E -- "$forbid" <<<"$out" | sed 's/^ *//')"
    fi
    if [[ $ok -eq 1 ]]; then
        PASS=$((PASS + 1))
        printf '  PASS  %-46s exit %s, said %s%s\n' "$label" "$rc" "\"$want_txt\"" \
            "$([[ -n "$forbid" ]] && echo ', scored nothing')"
    else
        FAIL=$((FAIL + 1))
        printf '  FAIL  %-46s %s\n' "$label" "$why"
        VERBOSE=1
    fi
    if [[ $VERBOSE -eq 1 ]]; then
        printf '%s\n' "$out" | sed 's/^/        /'
        echo
    fi
}

echo "Positive control -- the unmodified runner must still pass both modes."
echo "(Without these, a runner that always exited 4 would score a perfect self-test.)"
run_case "unmodified, normal"           0 "BENCH PASS"             "" ""
run_case "unmodified, negative-control" 0 "NEGATIVE CONTROL PASS"  "" "" --negative-control

echo
echo "Mutations -- each engine broken while still installed and on PATH."
for engine in slither semgrep forge; do
    for flavour in crash silent version malformed; do
        shim="$(make_shim "$engine" "$flavour")"
        # The malformed flavour is the only one that gets far enough for the
        # scoreboard to be at risk, so it is the only one asserted to emit none.
        forbid=""; [[ "$flavour" == "malformed" ]] && forbid="$SCORING_RE"
        run_case "$engine $flavour, normal"           4 "ENGINE FAILURE: $engine" "$forbid" "$shim"
        run_case "$engine $flavour, negative-control" 4 "ENGINE FAILURE: $engine" "$forbid" "$shim" --negative-control
    done
done

echo
echo "Wrong VALUES -- complete, structurally perfect output that still cannot be scored."
echo "(Nothing is missing from any of these. Presence and type checks pass; only a"
echo " check on what the field SAYS separates them from a real negative result.)"
for shape in Skipped Corrupt conflict; do
    vshim="$(make_value_shim forge "status-$shape" "" "$(forge_status_payload "$shape")")"
    run_case "forge status=$shape, normal"           4 "ENGINE FAILURE: forge" "$SCORING_RE" "$vshim"
    run_case "forge status=$shape, negative-control" 4 "ENGINE FAILURE: forge" "$SCORING_RE" "$vshim" --negative-control
done

sg_shim="$(make_value_shim semgrep skipped-rules --json "$SEMGREP_SKIPPED_RULES_PAYLOAD")"
run_case "semgrep skipped a rule, normal"           4 "ENGINE FAILURE: semgrep" "$SCORING_RE" "$sg_shim"
run_case "semgrep skipped a rule, negative-control" 4 "ENGINE FAILURE: semgrep" "$SCORING_RE" "$sg_shim" --negative-control

sl_shim="$(make_value_shim slither no-detector --list-detectors-json "$SLITHER_DETECTORS_PAYLOAD")"
run_case "slither lacks the scored detector, normal"   4 "ENGINE FAILURE: slither" "$SCORING_RE" "$sl_shim"
run_case "slither lacks the scored detector, negative" 4 "ENGINE FAILURE: slither" "$SCORING_RE" "$sl_shim" --negative-control

echo
echo "Wrong QUESTION -- complete, legal, unanimous output that answers something else."
echo "(Nothing is missing and no value is illegal here. These are the shapes a value"
echo " whitelist cannot see, because the value is fine and only the attribution is wrong.)"
for shape in unrelated noreason crossmarker shadow; do
    ashim="$(make_value_shim forge "attr-$shape" "" "$(forge_attribution_payload "$shape")")"
    run_case "forge $shape failure, normal"           4 "ENGINE FAILURE: forge" "$SCORING_RE" "$ashim"
    run_case "forge $shape failure, negative-control" 4 "ENGINE FAILURE: forge" "$SCORING_RE" "$ashim" --negative-control
done

echo
echo "Stale expectation ledger -- the positive control for the attribution itself."
echo "(A marker the harness can no longer emit makes every forge failure unattributable."
echo " The runner must name the stale marker BEFORE running forge, not leave a confusing"
echo " exit 4 later. Edit an owned assertion message and the ledger must say so.)"
STALE="$WORK/stale-ledger-bench"
mkdir -p "$STALE"
cp -R "$REPO_ROOT/bench/known-bugs/." "$STALE/"
rm -rf "$STALE/out" "$STALE/cache"
# A genuine rename, not a suffix: the ledger looks the marker up as a substring, so a
# superstring would still be found here (and would be caught later, at scoring).
sed 's/EXPECTATION MISSED: burn path already rounds up/ASSERTION FAILED: burn path already rounds up/' \
    "$STALE/test/Exploits.t.sol" > "$STALE/test/Exploits.t.sol.new" \
    && mv "$STALE/test/Exploits.t.sol.new" "$STALE/test/Exploits.t.sol"
run_case "stale ledger marker, normal"           4 "ENGINE FAILURE: forge" "$SCORING_RE" "" --bench-dir "$STALE"
run_case "stale ledger marker, negative-control" 4 "ENGINE FAILURE: forge" "$SCORING_RE" "" --bench-dir "$STALE" --negative-control

echo
echo "Denied \$HOME -- the shape of the sandbox that produced the original false pass."
echo "(semgrep's state is runner-owned now, but forge and slither still write under"
echo " \$HOME. A green result here would mean the toolchain stopped needing it, which"
echo " is worth confirming rather than assuming -- so this asserts the loud failure.)"
RO_HOME="$WORK/readonly-home"
mkdir -p "$RO_HOME"; chmod 500 "$RO_HOME"
ro_out="$(HOME="$RO_HOME" "$BENCH" 2>&1)"; ro_rc=$?
if [[ $ro_rc -eq 4 ]] && grep -q "ENGINE FAILURE:" <<<"$ro_out"; then
    PASS=$((PASS + 1))
    printf '  PASS  %-46s exit %s, %s\n' "read-only \$HOME, normal" "$ro_rc" \
        "$(grep -m1 'ENGINE FAILURE:' <<<"$ro_out")"
else
    FAIL=$((FAIL + 1))
    printf '  FAIL  %-46s exit %s (wanted 4 + an ENGINE FAILURE line)\n' "read-only \$HOME, normal" "$ro_rc"
    printf '%s\n' "$ro_out" | sed 's/^/        /'
fi
chmod 700 "$RO_HOME"

echo
echo "SELFTEST: $PASS passed, $FAIL failed."
if [[ $FAIL -ne 0 ]]; then
    echo "The runner does not reliably separate a dead engine from a clean result."
    echo "Every bench verdict is uninterpretable until this is fixed."
    exit 1
fi
echo "The runner reports ENGINE FAILURE for every broken engine in both modes,"
echo "and still passes both modes unmodified."
exit 0
