"""Security regressions for chrono-dedup's public HTTP transport."""

from __future__ import annotations

import io
import importlib.util
import json
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = ROOT / "plugins" / "chrono-dedup"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

if PLUGIN_ROOT.is_dir():
    from chrono_dedup import sources  # noqa: E402


class PublicPluginCapabilityTests(unittest.TestCase):
    """This publication guard must run even when the private SSRF suite skips."""

    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "model-lanes/check_public_plugin_capabilities.py"
        spec = importlib.util.spec_from_file_location("public_plugin_capabilities", path)
        assert spec is not None and spec.loader is not None
        cls.check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.check)

    def public_surface(self, root: Path) -> Path:
        """Project private inputs; audit existing public inputs without repair."""
        shutil.copytree(ROOT / "model-lanes", root / "model-lanes")
        for relative in ("shared/specialist-runtime-map.tsv",
                         "tools/export/policy/path-policy.json"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, path)
        payload = json.loads((root / "model-lanes/specialist-lane-capabilities.v1.json").read_text())
        if PLUGIN_ROOT.is_dir() and "public_projection" not in payload:
            sys.path.insert(0, str(ROOT / "model-lanes"))
            from project_public_capabilities import project_public_capabilities
            project_public_capabilities(
                root, self.check.load_policy(root / "tools/export/policy/path-policy.json")
            )
        return root

    def test_existing_public_surface_is_audited_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            published = self.public_surface(Path(temporary) / "published")
            source = published / "model-lanes/specialist-lane-capabilities.v1.json"
            payload = json.loads(source.read_text())
            entry = next(e for e in payload["entries"]
                         if (e["specialist"], e["lane"]) == ("experimental-attacker", "gpt-codex"))
            next(r for r in entry["mcps"] if r["id"] == "chrono-dedup").update(
                availability="available", evidence="installed-or-shared-authored")
            source.write_text(json.dumps(payload, indent=2) + "\n")
            poisoned = source.read_bytes()
            with mock.patch.dict(globals(), ROOT=published, PLUGIN_ROOT=published / "plugins/chrono-dedup"):
                audited = self.public_surface(Path(temporary) / "audited")
            self.assertEqual((audited / source.relative_to(published)).read_bytes(), poisoned)
            self.assertTrue(any(i["identifier"] == "chrono-dedup" and
                                i["path"] == source.relative_to(published).as_posix()
                                for i in self.check.audit(audited)))

    def test_public_declarations_have_no_withheld_plugin_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.public_surface(Path(temporary))
            self.assertEqual(self.check.audit(root), [])

    def test_new_withheld_component_is_detected_in_direct_and_brokered_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.public_surface(Path(temporary))
            # Only declaration inputs are copied; no plugin implementation is
            # present. The control therefore also covers a fresh public clone.
            policy_path = root / "tools/export/policy/path-policy.json"
            policy = json.loads((ROOT / "tools/export/policy/path-policy.json").read_text())
            policy["deny"].append("plugins/control-withheld/**")
            policy_path.write_text(json.dumps(policy))
            direct = root / "model-lanes/gpt-codex/.codex/agents/control.toml"
            brokered = root / "model-lanes/grok/.grok/agents/control.yaml"
            direct.write_text('mcps = ["control-withheld"]\n')
            brokered.write_text('mcps: ["lead:control-withheld"]\n')
            issues = self.check.audit(root)
            self.assertEqual({i["identifier"] for i in issues},
                             {"control-withheld", "lead:control-withheld"})
            self.assertEqual({i["path"] for i in issues},
                             {direct.relative_to(root).as_posix(), brokered.relative_to(root).as_posix()})

    def test_policy_change_catches_source_index_lane_and_runtime_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.public_surface(Path(temporary))
            policy_path = Path(temporary) / "policy.json"
            policy = json.loads((ROOT / "tools/export/policy/path-policy.json").read_text())
            # A real shipped plugin is the positive control; no component
            # spellings are baked into the checker itself.
            policy["deny"].append("plugins/chrono-vault/**")
            policy_path.write_text(json.dumps(policy))
            issues = self.check.audit(root, self.check.load_policy(policy_path))
            self.assertEqual({i["identifier"] for i in issues},
                             {"chrono-vault"})
            paths = {i["path"] for i in issues}
            self.assertTrue({"model-lanes/specialist-lane-capabilities.v1.json",
                             "model-lanes/generated-specialist-capabilities.json",
                             "model-lanes/lane-capabilities.tsv",
                             "shared/specialist-runtime-map.tsv"} <= paths)
            self.assertTrue(any("/.codex/agents/" in p for p in paths))
            self.assertTrue(any("/.claude/agents/" in p for p in paths))

    def test_unknown_policy_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.public_surface(Path(temporary))
            policy_path = Path(temporary) / "policy.json"
            policy = json.loads((ROOT / "tools/export/policy/path-policy.json").read_text())
            policy["public"].remove("plugins/**")
            policy_path.write_text(json.dumps(policy))
            issues = self.check.audit(root, self.check.load_policy(policy_path))
            self.assertTrue(issues)
            self.assertTrue(any(i["rule"] == "<no matching rule>" for i in issues))


def dns_answer(address: str, port: int = 443) -> list[tuple[object, ...]]:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr: tuple[object, ...]
    if family == socket.AF_INET6:
        sockaddr = (address, port, 0, 0)
    else:
        sockaddr = (address, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


class StubResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _: int) -> bytes:
        return self.body


class FakeSocket:
    def __init__(self) -> None:
        self.connected_to: tuple[object, ...] | None = None
        self.sent = bytearray()

    def settimeout(self, _: object) -> None:
        return None

    def bind(self, _: tuple[str, int]) -> None:
        raise AssertionError("unexpected source bind")

    def connect(self, address: tuple[object, ...]) -> None:
        self.connected_to = address

    def setsockopt(self, *_: object) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def makefile(self, *_: object, **__: object) -> io.BytesIO:
        return io.BytesIO(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"ok"
        )

    def close(self) -> None:
        return None


class RedirectingOpener:
    def __init__(self, handlers: tuple[object, ...], redirect_url: str) -> None:
        self.handlers = handlers
        self.redirect_url = redirect_url
        self.loopback_reached = False
        self.redirected_request: urllib.request.Request | None = None

    def open(self, request: urllib.request.Request, **_: object) -> StubResponse:
        redirect_handler = next(
            handler
            for handler in self.handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        )
        self.redirected_request = redirect_handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            self.redirect_url,
        )
        self.loopback_reached = True
        return StubResponse(b"loopback secret")


@unittest.skipUnless(PLUGIN_ROOT.is_dir(), "chrono-dedup is withheld from the public export")
class ChronoDedupSsrfSecurityTests(unittest.TestCase):
    def test_public_redirect_to_loopback_is_refused_before_second_request(self) -> None:
        openers: list[RedirectingOpener] = []

        def build_opener(*handlers: object) -> RedirectingOpener:
            opener = RedirectingOpener(
                handlers,
                "http://127.0.0.1:27123/commands/",
            )
            openers.append(opener)
            return opener

        def resolve(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
            address = "93.184.216.34" if host == "public.example" else "127.0.0.1"
            return dns_answer(address, port)

        with (
            mock.patch("socket.getaddrinfo", side_effect=resolve),
            mock.patch.object(sources, "build_opener", side_effect=build_opener, create=True),
            mock.patch.object(
                sources,
                "urlopen",
                return_value=StubResponse(b"loopback secret"),
                create=True,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "non-public"):
                sources.TargetProgramHistorySource().search(
                    "acme",
                    {
                        "component": "widget",
                        "program_history_url": "https://public.example/reports",
                    },
                    {"terms": ["widget"]},
                )

        self.assertEqual(len(openers), 1)
        self.assertFalse(openers[0].loopback_reached)

    def test_direct_private_loopback_and_link_local_destinations_are_refused(self) -> None:
        cases = (
            ("http://127.0.0.1/admin", "127.0.0.1"),
            ("http://10.1.2.3/admin", "10.1.2.3"),
            ("http://172.16.2.3/admin", "172.16.2.3"),
            ("http://192.168.2.3/admin", "192.168.2.3"),
            ("http://169.254.169.254/latest/meta-data", "169.254.169.254"),
            ("http://[::1]/admin", "::1"),
            ("http://mapped.example/admin", "::ffff:127.0.0.1"),
        )
        for url, address in cases:
            with (
                self.subTest(url=url),
                mock.patch(
                    "socket.getaddrinfo",
                    return_value=dns_answer(address, 80),
                ),
                mock.patch.object(
                    sources,
                    "urlopen",
                    return_value=StubResponse(b"private service"),
                    create=True,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "non-public"):
                    sources.HttpTransport().get_text(url)

    def test_public_redirect_is_revalidated_and_remains_available(self) -> None:
        openers: list[RedirectingOpener] = []

        def build_opener(*handlers: object) -> RedirectingOpener:
            opener = RedirectingOpener(
                handlers,
                "https://reports.example/disclosures",
            )
            openers.append(opener)
            return opener

        def resolve(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
            address = {
                "program.example": "93.184.216.34",
                "reports.example": "93.184.216.35",
            }[host]
            return dns_answer(address, port)

        with (
            mock.patch("socket.getaddrinfo", side_effect=resolve) as getaddrinfo,
            mock.patch.object(sources, "build_opener", side_effect=build_opener),
        ):
            body = sources.HttpTransport().get_text(
                "https://program.example/history"
            )

        self.assertEqual(body, "loopback secret")
        self.assertEqual(getaddrinfo.call_count, 2)
        self.assertTrue(openers[0].loopback_reached)
        target = getattr(openers[0].redirected_request, "_chrono_dedup_target")
        self.assertEqual(str(target.ip), "93.184.216.35")

    def test_connection_uses_validated_address_instead_of_resolving_again(self) -> None:
        fake_socket = FakeSocket()
        with (
            mock.patch(
                "socket.getaddrinfo",
                side_effect=[
                    dns_answer("93.184.216.34", 80),
                    dns_answer("127.0.0.1", 80),
                ],
            ) as getaddrinfo,
            mock.patch.object(sources.socket, "socket", return_value=fake_socket),
        ):
            body = sources.HttpTransport().get_text("http://rebind.example/history")

        self.assertEqual(body, "ok")
        self.assertEqual(getaddrinfo.call_count, 1)
        self.assertEqual(fake_socket.connected_to, ("93.184.216.34", 80))
        self.assertIn(b"Host: rebind.example\r\n", bytes(fake_socket.sent))


if __name__ == "__main__":
    unittest.main()
