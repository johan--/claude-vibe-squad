"""Pin the remaining public tool surface after research-provider retirement."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("arsenal_surface_server", PLUGIN / "mcp_server.py")
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class AcademicToolSurfaceTest(unittest.TestCase):
    def test_tool_list_contains_only_the_retained_academic_search(self) -> None:
        tools = asyncio.run(server.mcp.list_tools())
        self.assertEqual([tool.name for tool in tools], ["arxiv_search"])
        self.assertEqual(tools[0].inputSchema["required"], ["query"])
        self.assertEqual(set(tools[0].inputSchema["properties"]),
                         {"query", "max_results", "categories"})

    def test_manifest_has_no_provider_credentials(self) -> None:
        manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
        config = manifest["mcpServers"]["chrono-research-arsenal"]
        self.assertEqual(config.get("env", {}), {})
        self.assertIn("arxiv_search", manifest["description"])
        self.assertEqual(config["args"], ["${CLAUDE_PLUGIN_ROOT}/mcp_server.py"])


if __name__ == "__main__":
    unittest.main()
