#!/usr/bin/env python3
"""Real effect probe harness for the V2 Seatbelt profile compiler.

``run_probe_suite`` is hermetic: reads, writes, execs, loopback and
non-loopback denial, with no traffic beyond one SYN to TEST-NET-1 for the
unsandboxed control. ``run_provider_egress_probe`` opens one real TCP
connection to a provider host on 443 (no bytes sent). ``run_lane_smoke``
starts one installed lane CLI through the strict launch path.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import errno
import functools
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from typing import Callable, Iterator, Sequence

import launch_hygiene
from seatbelt_profile import (
    DEFAULT_LANE_PATH,
    LANE_CLI_PATHS,
    CompiledProfile,
    HostCompatibility,
    ProfileSpec,
    compile_profile,
    scoped_lane_launch_profile,
)


CompatibilityVerifier = Callable[[], HostCompatibility]

# TEST-NET-1 (RFC 5737): never routed, so an unsandboxed connect times out or
# is unreachable. Only the sandbox answers it with an immediate EPERM.
NON_LOOPBACK_TARGET = ("192.0.2.1", 80)
DEFAULT_PROVIDER_HOST = "api.anthropic.com"
# SBPL rejects any remote host other than "*" or "localhost" ("host must be *
# or localhost in network address"), so port 443 is the narrowest provider
# grant the language can express.
PROVIDER_EGRESS_RULE = '(allow network-outbound (remote tcp "*:443"))'
_CONNECT_TIMEOUT_MS = 1500
# A sandbox denial is synchronous; anything slower means the kernel tried the
# network before refusing.
_IMMEDIATE_DENIAL_MS = 1000
_READ_FIXTURE_CONTENT = "seatbelt-read-probe-content"
_EXEC_MARKER = "seatbelt-exec-probe-ran"
_ALLOWED_EXEC_TARGET = Path("/bin/echo")
_DENIED_EXEC_TARGET = Path("/usr/bin/printf")


@dataclass(frozen=True)
class ProbeEffect:
    succeeded: bool
    returncode: int
    errno: int | None
    stdout: str
    stderr: str
    target_exists: bool | None = None
    elapsed_ms: int | None = None
    content: str | None = None


@dataclass(frozen=True)
class ProbeSuiteResult:
    fixture_root: str
    deny_profile_sha256: str
    broker_profile_sha256: str
    declared_write: ProbeEffect
    undeclared_write: ProbeEffect
    network_deny: ProbeEffect
    exact_broker_port: ProbeEffect
    wrong_broker_port: ProbeEffect
    allowed_read: ProbeEffect
    denied_read: ProbeEffect
    allowed_exec: ProbeEffect
    denied_exec: ProbeEffect
    nonloopback_deny: ProbeEffect
    nonloopback_unsandboxed_control: ProbeEffect

    @property
    def writes_passed(self) -> bool:
        return bool(
            self.declared_write.succeeded
            and self.declared_write.target_exists
            and not self.undeclared_write.succeeded
            and self.undeclared_write.errno == errno.EPERM
            and self.undeclared_write.target_exists is False
        )

    @property
    def loopback_passed(self) -> bool:
        return bool(
            not self.network_deny.succeeded
            and self.network_deny.errno == errno.EPERM
            and self.exact_broker_port.succeeded
            and not self.wrong_broker_port.succeeded
            and self.wrong_broker_port.errno == errno.EPERM
        )

    @property
    def reads_passed(self) -> bool:
        return bool(
            self.allowed_read.succeeded
            and self.allowed_read.content == _READ_FIXTURE_CONTENT
            and not self.denied_read.succeeded
            and self.denied_read.errno == errno.EPERM
            and self.denied_read.content is None
            # Positive control: the denied file exists and is readable
            # outside the sandbox, so the EPERM is the profile's doing.
            and self.denied_read.target_exists is True
        )

    @property
    def execs_passed(self) -> bool:
        return bool(
            self.allowed_exec.succeeded
            and _EXEC_MARKER in self.allowed_exec.stdout
            and not self.denied_exec.succeeded
            and self.denied_exec.errno == errno.EPERM
            and _EXEC_MARKER not in self.denied_exec.stdout
            # Positive control: the denied target is a real executable.
            and self.denied_exec.target_exists is True
        )

    @property
    def arbitrary_network_denial_passed(self) -> bool:
        return bool(
            not self.nonloopback_deny.succeeded
            and self.nonloopback_deny.errno == errno.EPERM
            and self.nonloopback_deny.elapsed_ms is not None
            and self.nonloopback_deny.elapsed_ms < _IMMEDIATE_DENIAL_MS
            # Positive control: the same probe outside the sandbox does not
            # get EPERM, so EPERM cannot come from the host network stack.
            and self.nonloopback_unsandboxed_control.errno != errno.EPERM
        )

    @property
    def passed(self) -> bool:
        return (
            self.writes_passed
            and self.loopback_passed
            and self.reads_passed
            and self.execs_passed
            and self.arbitrary_network_denial_passed
        )


@dataclass(frozen=True)
class ProviderEgressResult:
    provider_host: str
    provider_address: str
    egress_rule: str
    deny_profile_sha256: str
    egress_profile_sha256: str
    provider_connect: ProbeEffect
    provider_deny_control: ProbeEffect
    egress_other_port_denied: ProbeEffect
    host_scoped_rule_returncode: int
    host_scoped_rule_stderr: str

    @property
    def passed(self) -> bool:
        return bool(
            self.provider_connect.succeeded
            # Control: the same address and port under the no-network profile
            # is refused, so the egress rule is what opened the connection.
            and not self.provider_deny_control.succeeded
            and self.provider_deny_control.errno == errno.EPERM
            # The grant is port-scoped: the same host on port 80 is refused.
            and not self.egress_other_port_denied.succeeded
            and self.egress_other_port_denied.errno == errno.EPERM
        )


@dataclass(frozen=True)
class LaneSmokeResult:
    lane: str
    command: tuple[str, ...]
    profile_sha256: str
    canary_passed: bool
    canary_details: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None

    @property
    def passed(self) -> bool:
        return bool(
            self.error is None
            and self.canary_passed
            and self.returncode == 0
            and self.stdout.strip()
        )


class _LoopbackListener:
    def __init__(self, *, timeout: float = 3.0) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        self._socket.settimeout(timeout)
        self.port = self._socket.getsockname()[1]
        self.accepted = False
        self._thread = threading.Thread(target=self._serve, daemon=False)

    def _serve(self) -> None:
        try:
            connection, _address = self._socket.accept()
        except TimeoutError:
            return
        else:
            self.accepted = True
            with connection:
                connection.recv(1)
        finally:
            self._socket.close()

    def start(self) -> None:
        self._thread.start()

    def finish(self) -> None:
        self._thread.join(timeout=4)
        if self._thread.is_alive():
            raise RuntimeError(
                f"loopback listener on port {self.port} did not terminate"
            )


def _run_sandbox(
    profile: str,
    command: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, *command],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
        close_fds=True,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )


def _run_unsandboxed(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
        close_fds=True,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )


def _effect(
    completed: subprocess.CompletedProcess[str],
    *,
    target: Path | None = None,
    target_exists: bool | None = None,
) -> ProbeEffect:
    observed_errno: int | None = None
    elapsed_ms: int | None = None
    for line in completed.stdout.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("errno"), int):
            observed_errno = payload["errno"]
        if isinstance(payload.get("elapsed_ms"), int):
            elapsed_ms = payload["elapsed_ms"]
    combined = f"{completed.stdout}\n{completed.stderr}".lower()
    if (
        completed.returncode != 0
        and observed_errno is None
        and "operation not permitted" in combined
    ):
        observed_errno = errno.EPERM
    if target is not None:
        target_exists = target.exists()
    return ProbeEffect(
        succeeded=completed.returncode == 0,
        returncode=completed.returncode,
        errno=observed_errno,
        stdout=completed.stdout,
        stderr=completed.stderr,
        target_exists=target_exists,
        elapsed_ms=elapsed_ms,
    )


def _read_effect(
    completed: subprocess.CompletedProcess[str], *, target: Path
) -> ProbeEffect:
    effect = _effect(completed, target_exists=os.access(target, os.R_OK))
    content: str | None = None
    if completed.returncode == 0:
        # First line is the JSON byte count; the file bytes follow verbatim.
        _header, _separator, content = completed.stdout.partition("\n")
    return ProbeEffect(**{**asdict(effect), "content": content})


# One retained binary covers every effect so each result carries the exact
# errno from the failing syscall rather than a tool's error text.
#   connect <ipv4> <port> <timeout_ms>  non-blocking connect, no bytes sent
#   read <path>                         open + read, file bytes after a header
#   exec <path> [args...]               execv in place
_PROBE_SOURCE = r"""
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static long elapsed_ms(const struct timeval *start) {
    struct timeval now;
    gettimeofday(&now, NULL);
    return (now.tv_sec - start->tv_sec) * 1000L
        + (now.tv_usec - start->tv_usec) / 1000L;
}

static int report(const char *stage, int error, const struct timeval *start) {
    printf("{\"stage\":\"%s\",\"errno\":%d,\"elapsed_ms\":%ld}\n",
           stage, error, start ? elapsed_ms(start) : 0L);
    return 73;
}

static int probe_connect(const char *host, const char *port, const char *wait) {
    struct sockaddr_in address;
    struct pollfd waiter;
    struct timeval start;
    socklen_t length = sizeof(int);
    int descriptor, flags, ready, error = 0;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons((unsigned short)strtoul(port, NULL, 10));
    if (inet_pton(AF_INET, host, &address.sin_addr) != 1) return 66;
    gettimeofday(&start, NULL);
    descriptor = socket(AF_INET, SOCK_STREAM, 0);
    if (descriptor < 0) return report("socket", errno, &start);
    flags = fcntl(descriptor, F_GETFL, 0);
    if (flags < 0 || fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) != 0) {
        error = errno;
        close(descriptor);
        return report("fcntl", error, &start);
    }
    if (connect(descriptor, (struct sockaddr *)&address, sizeof(address)) != 0) {
        if (errno != EINPROGRESS) {
            error = errno;
            close(descriptor);
            return report("connect", error, &start);
        }
        waiter.fd = descriptor;
        waiter.events = POLLOUT;
        ready = poll(&waiter, 1, atoi(wait));
        if (ready == 0) {
            error = ETIMEDOUT;
        } else if (ready < 0) {
            error = errno;
        } else if (getsockopt(descriptor, SOL_SOCKET, SO_ERROR, &error, &length) != 0) {
            error = errno;
        }
        if (error != 0) {
            close(descriptor);
            return report("connect", error, &start);
        }
    }
    printf("{\"connected\":true,\"elapsed_ms\":%ld}\n", elapsed_ms(&start));
    close(descriptor);
    return 0;
}

static int probe_read(const char *path) {
    char buffer[4096];
    ssize_t count;
    int error;
    int descriptor = open(path, O_RDONLY);
    if (descriptor < 0) return report("open", errno, NULL);
    count = read(descriptor, buffer, sizeof(buffer));
    if (count < 0) {
        error = errno;
        close(descriptor);
        return report("read", error, NULL);
    }
    close(descriptor);
    printf("{\"bytes\":%zd}\n", count);
    fwrite(buffer, 1, (size_t)count, stdout);
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 5 && strcmp(argv[1], "connect") == 0)
        return probe_connect(argv[2], argv[3], argv[4]);
    if (argc == 3 && strcmp(argv[1], "read") == 0)
        return probe_read(argv[2]);
    if (argc >= 3 && strcmp(argv[1], "exec") == 0) {
        execv(argv[2], &argv[2]);
        report("exec", errno, NULL);
        return 75;
    }
    return 64;
}
"""


def _build_probe(target: Path) -> None:
    completed = subprocess.run(
        ["/usr/bin/clang", "-x", "c", "-o", str(target), "-"],
        input=_PROBE_SOURCE,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        close_fds=True,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0 or not target.is_file():
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"could not build retained effect probe: {detail}")


def _compile(
    spec: ProfileSpec, verifier: CompatibilityVerifier | None
) -> CompiledProfile:
    if verifier is None:
        return compile_profile(spec)
    return compile_profile(spec, compatibility_verifier=verifier)


def _connect_command(executable: Path, host: str, port: int) -> list[str]:
    return [str(executable), "connect", host, str(port), str(_CONNECT_TIMEOUT_MS)]


def _connect(
    profile: str, executable: Path, port: int, *, host: str = "127.0.0.1"
) -> ProbeEffect:
    return _effect(_run_sandbox(profile, _connect_command(executable, host, port)))


def run_probe_suite(
    fixture_root: Path,
    *,
    compatibility_verifier: CompatibilityVerifier | None = None,
) -> ProbeSuiteResult:
    """Run the hermetic effects, retaining fixtures for audit/no-delete."""

    root = Path(fixture_root)
    allowed = root / "allowed"
    readable = root / "readable"
    denied = root / "denied"
    for directory in (allowed, readable, denied):
        directory.mkdir(parents=True, exist_ok=False)
    declared_target = allowed / "declared.txt"
    denied_target = denied / "undeclared.txt"
    readable_file = readable / "granted.txt"
    secret_file = denied / "secret.txt"
    readable_file.write_text(_READ_FIXTURE_CONTENT, encoding="utf-8")
    secret_file.write_text(_READ_FIXTURE_CONTENT, encoding="utf-8")
    probe = allowed / "effect-probe"
    _build_probe(probe)

    network_deny_listener = _LoopbackListener()
    network_deny_listener.start()
    deny_profile = _compile(
        ProfileSpec(
            read_paths=(readable,),
            write_paths=(allowed,),
            executable_paths=(
                Path("/bin/sh"),
                Path("/bin/bash"),
                _ALLOWED_EXEC_TARGET,
                probe,
            ),
            allow_fork=True,
        ),
        compatibility_verifier,
    )

    declared_completed = _run_sandbox(
        deny_profile.text,
        ["/bin/sh", "-c", 'printf permitted > "$1"', "probe", str(declared_target)],
    )
    undeclared_completed = _run_sandbox(
        deny_profile.text,
        ["/bin/sh", "-c", 'printf denied > "$1"', "probe", str(denied_target)],
    )
    network_deny = _connect(deny_profile.text, probe, network_deny_listener.port)
    network_deny_listener.finish()

    allowed_read = _read_effect(
        _run_sandbox(deny_profile.text, [str(probe), "read", str(readable_file)]),
        target=readable_file,
    )
    denied_read = _read_effect(
        _run_sandbox(deny_profile.text, [str(probe), "read", str(secret_file)]),
        target=secret_file,
    )
    allowed_exec = _effect(
        _run_sandbox(
            deny_profile.text,
            [str(probe), "exec", str(_ALLOWED_EXEC_TARGET), _EXEC_MARKER],
        ),
        target_exists=os.access(_ALLOWED_EXEC_TARGET, os.X_OK),
    )
    denied_exec = _effect(
        _run_sandbox(
            deny_profile.text,
            [str(probe), "exec", str(_DENIED_EXEC_TARGET), _EXEC_MARKER],
        ),
        target_exists=os.access(_DENIED_EXEC_TARGET, os.X_OK),
    )
    nonloopback_host, nonloopback_port = NON_LOOPBACK_TARGET
    nonloopback_deny = _connect(
        deny_profile.text, probe, nonloopback_port, host=nonloopback_host
    )
    nonloopback_control = _effect(
        _run_unsandboxed(
            _connect_command(probe, nonloopback_host, nonloopback_port)
        )
    )

    broker_listener = _LoopbackListener()
    wrong_listener = _LoopbackListener()
    broker_listener.start()
    wrong_listener.start()
    broker_profile = _compile(
        ProfileSpec(
            write_paths=(allowed,),
            executable_paths=(probe,),
            broker_port=broker_listener.port,
        ),
        compatibility_verifier,
    )
    exact_broker = _connect(broker_profile.text, probe, broker_listener.port)
    wrong_broker = _connect(broker_profile.text, probe, wrong_listener.port)
    broker_listener.finish()
    wrong_listener.finish()

    return ProbeSuiteResult(
        fixture_root=str(root),
        deny_profile_sha256=deny_profile.sha256,
        broker_profile_sha256=broker_profile.sha256,
        declared_write=_effect(declared_completed, target=declared_target),
        undeclared_write=_effect(undeclared_completed, target=denied_target),
        network_deny=network_deny,
        exact_broker_port=exact_broker,
        wrong_broker_port=wrong_broker,
        allowed_read=allowed_read,
        denied_read=denied_read,
        allowed_exec=allowed_exec,
        denied_exec=denied_exec,
        nonloopback_deny=nonloopback_deny,
        nonloopback_unsandboxed_control=nonloopback_control,
    )


def _resolve_ipv4(host: str) -> str:
    records = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    if not records:
        raise RuntimeError(f"no IPv4 address for provider host {host}")
    return str(records[0][4][0])


def run_provider_egress_probe(
    fixture_root: Path,
    *,
    provider_host: str = DEFAULT_PROVIDER_HOST,
    compatibility_verifier: CompatibilityVerifier | None = None,
) -> ProviderEgressResult:
    """One TCP connect to the provider on 443. No bytes, no credentials.

    The compiler emits no provider-egress rule, so the egress profile is the
    compiled no-network profile plus ``PROVIDER_EGRESS_RULE``. DNS resolves
    outside the sandbox; in-sandbox name resolution is not exercised.
    """

    root = Path(fixture_root)
    root.mkdir(parents=True, exist_ok=False)
    probe = root / "effect-probe"
    _build_probe(probe)
    address = _resolve_ipv4(provider_host)
    deny_profile = _compile(
        ProfileSpec(write_paths=(root,), executable_paths=(probe,)),
        compatibility_verifier,
    )
    egress_text = deny_profile.text + PROVIDER_EGRESS_RULE + "\n"
    host_scoped = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            deny_profile.text
            + f'(allow network-outbound (remote tcp "{provider_host}:443"))\n',
            "/usr/bin/true",
        ],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
        close_fds=True,
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    return ProviderEgressResult(
        provider_host=provider_host,
        provider_address=address,
        egress_rule=PROVIDER_EGRESS_RULE,
        deny_profile_sha256=deny_profile.sha256,
        egress_profile_sha256=hashlib.sha256(egress_text.encode("utf-8")).hexdigest(),
        provider_connect=_connect(egress_text, probe, 443, host=address),
        provider_deny_control=_connect(deny_profile.text, probe, 443, host=address),
        egress_other_port_denied=_connect(egress_text, probe, 80, host=address),
        host_scoped_rule_returncode=host_scoped.returncode,
        host_scoped_rule_stderr=host_scoped.stderr.strip(),
    )


@contextmanager
def _launch_hygiene_verifier(
    verifier: CompatibilityVerifier | None,
) -> Iterator[None]:
    """Route launch_hygiene's own compile through ``verifier`` in-process."""

    if verifier is None:
        yield
        return
    original = launch_hygiene.compile_profile
    launch_hygiene.compile_profile = functools.partial(
        original, compatibility_verifier=verifier
    )
    try:
        yield
    finally:
        launch_hygiene.compile_profile = original


def run_lane_smoke(
    lane: str,
    fixture_root: Path,
    *,
    compatibility_verifier: CompatibilityVerifier | None = None,
    timeout: float = 60,
) -> LaneSmokeResult:
    """Run ``<lane CLI> --version`` through the strict launch path.

    Mirrors board-supervisor's strict lane launch: the lane-scoped compiled
    profile, the Stage-1 preflight canary on that exact profile, the lane
    PATH, and the 4096-process limit. It omits the chrono-vault broker relay
    grant and the controller vault deny subtrees, and it proves only that the
    CLI starts and exits confined; no authentication, provider call or task
    work happens.
    """

    root = Path(os.path.realpath(fixture_root))
    root.mkdir(parents=True, exist_ok=False)
    command = (str(LANE_CLI_PATHS[lane]), "--version")
    prepared = None
    try:
        with _launch_hygiene_verifier(compatibility_verifier):
            with scoped_lane_launch_profile(lane=lane):
                prepared = launch_hygiene.run_preflight_canary(
                    root, retain_launch=True
                )
        prepared.environment["PATH"] = DEFAULT_LANE_PATH
        canary = prepared.canary
        completed = launch_hygiene.launch_if_canary_passes(
            lambda: prepared,
            command,
            expected_profile_sha256=canary.profile_sha256,
            expected_request_sha256="",
            expected_scope_sha256=canary.scope_sha256,
            timeout=timeout,
            limits=launch_hygiene.ResourceLimits(process_count=4096),
        )
    except Exception as exc:  # recorded, never raised: qualification reports it
        if prepared is not None:
            prepared.close()
        return LaneSmokeResult(
            lane=lane,
            command=command,
            profile_sha256=prepared.profile.sha256 if prepared else "",
            canary_passed=bool(prepared and prepared.canary.passed),
            canary_details=prepared.canary.details if prepared else (),
            returncode=None,
            stdout="",
            stderr="",
            error=f"{type(exc).__name__}: {exc}",
        )
    return LaneSmokeResult(
        lane=lane,
        command=command,
        profile_sha256=prepared.profile.sha256,
        canary_passed=canary.passed,
        canary_details=canary.details,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr[-4000:],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", type=Path, required=True)
    arguments = parser.parse_args()
    result = run_probe_suite(arguments.fixture_root)
    payload = asdict(result)
    payload["passed"] = result.passed
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
