#!/usr/bin/env python3
"""Qualify one macOS build for the worker Seatbelt sandbox.

Runs the complete docs/design/2026-09-02-worker-sandbox-scope.md section 4
list on the current host: the compatibility canary, allowed and denied reads,
writes and execs, loopback, provider egress, arbitrary-network denial, and a
real lane smoke test. The candidate build is admitted in this process only;
KNOWN_MACOS_BUILDS in seatbelt_profile.py is never edited. Admitting a build
for production stays a separate reviewed change.

Exit 0 only when every item passes; 1 when any item fails; 2 when the
qualification could not run (wrong host, unreadable build).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Callable, Mapping, Sequence

import seatbelt_probe
from seatbelt_profile import (
    CANONICAL_SANDBOX_EXEC,
    KNOWN_MACOS_BUILDS,
    LANE_CLI_PATHS,
    HostCompatibility,
    SandboxCompatibilityError,
    _read_macos_build,
    verify_sandbox_compatibility,
)


SCHEMA = "macos-build-qualification/v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "_state" / "sandbox-qualification"


# One item per clause of the section 4 list, in the order it names them.
ITEM_NAMES = (
    "compatibility_canary",
    "reads",
    "writes",
    "execs",
    "loopback",
    "provider_egress",
    "arbitrary_network_denial",
    "lane_smoke",
)


class QualificationError(RuntimeError):
    """Qualification could not run; distinct from an item failing."""


def candidate_verifier(candidate_build: str) -> Callable[[], HostCompatibility]:
    """Admit ``candidate_build`` for this process only.

    The real verifier still reads the host build and runs the canary, so a
    host that is not the candidate is refused exactly as in production.
    """

    return functools.partial(
        verify_sandbox_compatibility,
        allowed_builds=frozenset(KNOWN_MACOS_BUILDS | {candidate_build}),
    )


def _item(passed: bool, evidence: Mapping[str, object]) -> dict[str, object]:
    return {"passed": bool(passed), "evidence": dict(evidence)}


def build_items(
    *,
    compatibility: HostCompatibility | None,
    compatibility_error: str | None,
    suite: seatbelt_probe.ProbeSuiteResult,
    egress: seatbelt_probe.ProviderEgressResult,
    lanes: Sequence[seatbelt_probe.LaneSmokeResult],
) -> dict[str, dict[str, object]]:
    """Map probe results onto the section 4 list, one item per clause."""

    return {
        "compatibility_canary": _item(
            compatibility is not None,
            {
                "compatibility": (
                    {**asdict(compatibility), "sandbox_exec": str(compatibility.sandbox_exec)}
                    if compatibility
                    else None
                ),
                "error": compatibility_error,
            },
        ),
        "reads": _item(
            suite.reads_passed,
            {
                "allowed_read": asdict(suite.allowed_read),
                "denied_read": asdict(suite.denied_read),
            },
        ),
        "writes": _item(
            suite.writes_passed,
            {
                "declared_write": asdict(suite.declared_write),
                "undeclared_write": asdict(suite.undeclared_write),
            },
        ),
        "execs": _item(
            suite.execs_passed,
            {
                "allowed_exec": asdict(suite.allowed_exec),
                "denied_exec": asdict(suite.denied_exec),
            },
        ),
        "loopback": _item(
            suite.loopback_passed,
            {
                "network_deny": asdict(suite.network_deny),
                "exact_broker_port": asdict(suite.exact_broker_port),
                "wrong_broker_port": asdict(suite.wrong_broker_port),
            },
        ),
        "provider_egress": _item(egress.passed, asdict(egress)),
        "arbitrary_network_denial": _item(
            suite.arbitrary_network_denial_passed,
            {
                "target": "%s:%d" % seatbelt_probe.NON_LOOPBACK_TARGET,
                "nonloopback_deny": asdict(suite.nonloopback_deny),
                "nonloopback_unsandboxed_control": asdict(
                    suite.nonloopback_unsandboxed_control
                ),
            },
        ),
        "lane_smoke": _item(
            bool(lanes) and all(lane.passed for lane in lanes),
            {lane.lane: {**asdict(lane), "passed": lane.passed} for lane in lanes},
        ),
    }


def build_report(
    *,
    candidate_build: str,
    host_build: str,
    product_version: str,
    sandbox_exec_sha256: str,
    items: Mapping[str, Mapping[str, object]],
    profiles: Mapping[str, str],
    fixture_root: str,
    qualified_at: str,
) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "candidate_build": candidate_build,
        "host_build": host_build,
        "product_version": product_version,
        "already_admitted": candidate_build in KNOWN_MACOS_BUILDS,
        "admission": "in-process only; KNOWN_MACOS_BUILDS was not edited",
        "qualified_at": qualified_at,
        "sandbox_exec": str(CANONICAL_SANDBOX_EXEC),
        "sandbox_exec_sha256": sandbox_exec_sha256,
        # Profile text embeds the fixture paths, so these hashes bind this run
        # and are not expected to repeat across runs.
        "profile_sha256": dict(profiles),
        "fixture_root": fixture_root,
        "items": {name: dict(item) for name, item in items.items()},
        "failed_items": sorted(
            name for name, item in items.items() if not item["passed"]
        ),
        "passed": bool(items) and all(item["passed"] for item in items.values()),
    }


def write_json_atomically(path: Path, payload: Mapping[str, object]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _product_version() -> str:
    completed = subprocess.run(
        ["/usr/bin/sw_vers", "-productVersion"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return completed.stdout.strip()


def qualify(
    candidate_build: str,
    *,
    fixture_root: Path,
    lanes: Sequence[str],
    provider_host: str,
    build_reader: Callable[[], str] = _read_macos_build,
) -> dict[str, object]:
    try:
        host_build = build_reader().strip()
    except SandboxCompatibilityError as exc:
        raise QualificationError(str(exc)) from exc
    if host_build != candidate_build:
        raise QualificationError(
            f"candidate build {candidate_build!r} must be qualified on a host "
            f"running it; this host runs {host_build!r}"
        )
    fixture_root = Path(fixture_root)
    fixture_root.mkdir(parents=True, exist_ok=False)
    verifier = candidate_verifier(candidate_build)
    compatibility: HostCompatibility | None = None
    compatibility_error: str | None = None
    try:
        compatibility = verifier()
    except SandboxCompatibilityError as exc:
        compatibility_error = str(exc)
    if compatibility is None:
        # Every probe compiles through the same gate, so nothing else can run;
        # the result still records the failure against this build.
        skipped = {"skipped": "compatibility canary failed"}
        items = {name: _item(False, skipped) for name in ITEM_NAMES}
        items["compatibility_canary"] = _item(
            False, {"compatibility": None, "error": compatibility_error}
        )
        return build_report(
            candidate_build=candidate_build,
            host_build=host_build,
            product_version=_product_version(),
            sandbox_exec_sha256=_sha256_file(CANONICAL_SANDBOX_EXEC),
            items=items,
            profiles={},
            fixture_root=str(fixture_root),
            qualified_at=_utc_now(),
        )

    suite = seatbelt_probe.run_probe_suite(
        fixture_root / "suite", compatibility_verifier=verifier
    )
    egress = seatbelt_probe.run_provider_egress_probe(
        fixture_root / "egress",
        provider_host=provider_host,
        compatibility_verifier=verifier,
    )
    lane_results = [
        seatbelt_probe.run_lane_smoke(
            lane, fixture_root / f"lane-{lane}", compatibility_verifier=verifier
        )
        for lane in lanes
    ]
    items = build_items(
        compatibility=compatibility,
        compatibility_error=compatibility_error,
        suite=suite,
        egress=egress,
        lanes=lane_results,
    )
    profiles = {
        "deny": suite.deny_profile_sha256,
        "broker": suite.broker_profile_sha256,
        "provider_deny": egress.deny_profile_sha256,
        "provider_egress": egress.egress_profile_sha256,
        **{f"lane:{lane.lane}": lane.profile_sha256 for lane in lane_results},
    }
    return build_report(
        candidate_build=candidate_build,
        host_build=host_build,
        product_version=_product_version(),
        sandbox_exec_sha256=_sha256_file(CANONICAL_SANDBOX_EXEC),
        items=items,
        profiles=profiles,
        fixture_root=str(fixture_root),
        qualified_at=_utc_now(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-build",
        help="build ID to qualify; defaults to this host's build",
    )
    parser.add_argument(
        "--lane",
        action="append",
        choices=sorted(LANE_CLI_PATHS),
        help="lane CLI for the smoke test; repeatable; default all five",
    )
    parser.add_argument(
        "--provider-host", default=seatbelt_probe.DEFAULT_PROVIDER_HOST
    )
    parser.add_argument("--output", type=Path, help="result JSON path")
    parser.add_argument(
        "--fixture-root",
        type=Path,
        help="retained fixture directory; must not exist",
    )
    arguments = parser.parse_args(argv)
    try:
        candidate = arguments.candidate_build or _read_macos_build()
    except SandboxCompatibilityError as exc:
        print(f"qualification could not run: {exc}", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = arguments.output or DEFAULT_OUTPUT_ROOT / f"{candidate}-{stamp}.json"
    fixture_root = arguments.fixture_root or (
        DEFAULT_OUTPUT_ROOT / f"{candidate}-{stamp}-fixtures"
    )
    try:
        report = qualify(
            candidate,
            fixture_root=Path(os.path.realpath(fixture_root)),
            lanes=arguments.lane or sorted(LANE_CLI_PATHS),
            provider_host=arguments.provider_host,
        )
    except QualificationError as exc:
        print(f"qualification could not run: {exc}", file=sys.stderr)
        return 2
    write_json_atomically(output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    print(
        f"{'PASSED' if report['passed'] else 'FAILED'}: {candidate} "
        f"failed_items={report['failed_items']} result={output}",
        file=sys.stderr,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
