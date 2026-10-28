from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "academic_search_server", Path(__file__).with_name("mcp_server.py")
)
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class ArxivSearchTest(unittest.TestCase):
    def test_query_categories_metadata_and_fast_failure_settings(self) -> None:
        published = datetime(2026, 1, 2, tzinfo=timezone.utc)
        paper = SimpleNamespace(
            entry_id="https://arxiv.org/abs/2601.00001", title="A paper",
            summary="Abstract", authors=[SimpleNamespace(name="A. Author")],
            published=published, pdf_url="https://arxiv.org/pdf/2601.00001",
        )
        with patch.object(server.arxiv, "Client") as client:
            client.return_value.results.return_value = [paper]
            result = server.arxiv_search("retrieval", 3, ["cs.AI", "cs.CL"])
        client.assert_called_once_with(page_size=3, delay_seconds=0.0, num_retries=0)
        search = client.return_value.results.call_args.args[0]
        self.assertEqual(search.query, "(cat:cs.AI OR cat:cs.CL) AND (retrieval)")
        self.assertEqual(search.max_results, 3)
        self.assertEqual(result, {"ok": True, "result": {"papers": [{
            "id": paper.entry_id, "title": paper.title, "summary": paper.summary,
            "authors": ["A. Author"], "published": published.isoformat(),
            "pdf_url": paper.pdf_url,
        }]}})

    def test_result_limit_is_bounded_and_empty_result_is_successful(self) -> None:
        for requested, expected in ((0, 1), (-2, 1), (100, 10), (5, 5)):
            with self.subTest(requested=requested), patch.object(server.arxiv, "Client") as client:
                client.return_value.results.return_value = []
                result = server.arxiv_search("query", requested)
                self.assertEqual(result, {"ok": True, "result": {"papers": []}})
                self.assertEqual(client.return_value.results.call_args.args[0].max_results, expected)

    def test_missing_publication_date_is_preserved(self) -> None:
        paper = SimpleNamespace(entry_id="paper", title="Title", summary="Abstract",
                                authors=[], published=None, pdf_url=None)
        with patch.object(server.arxiv, "Client") as client:
            client.return_value.results.return_value = [paper]
            result = server.arxiv_search("query")
        self.assertIsNone(result["result"]["papers"][0]["published"])

    def test_throttle_uses_existing_academic_fallback(self) -> None:
        error = server.arxiv.HTTPError("https://export.arxiv.org", 0, 429)
        with patch.object(server.arxiv, "Client") as client, patch.object(
            server, "_openalex_fallback", return_value={"ok": True, "result": {"papers": []}}
        ) as fallback:
            client.return_value.results.side_effect = error
            result = server.arxiv_search("query", 99)
        fallback.assert_called_once_with("query", 10)
        self.assertTrue(result["ok"])

    def test_local_error_does_not_echo_query_or_exception_message(self) -> None:
        with patch.object(server.arxiv, "Client", side_effect=ValueError("private-query")):
            result = server.arxiv_search("private-query")
        self.assertEqual(result, {"ok": False, "error": "arxiv_error: ValueError"})


class AcademicFallbackTest(unittest.TestCase):
    def test_fallback_metadata_and_source_label(self) -> None:
        data = {"results": [{"id": "work", "title": "Title", "publication_year": 2026,
                             "authorships": [{"author": {"display_name": "Author"}}],
                             "primary_location": {"pdf_url": "https://example.com/paper.pdf",
                                                  "landing_page_url": "https://example.com/paper"}}]}
        with patch.object(server.httpx, "Client") as client:
            response = client.return_value.__enter__.return_value.get.return_value
            response.json.return_value = data
            result = server._openalex_fallback("query", 2)
            call = client.return_value.__enter__.return_value.get.call_args
        self.assertEqual(call.args, ("https://api.openalex.org/works",))
        self.assertEqual(call.kwargs["params"]["search"], "query")
        self.assertEqual(call.kwargs["params"]["per-page"], 2)
        self.assertEqual(result["result"]["source"], "openalex_fallback")
        self.assertEqual(result["result"]["papers"][0]["authors"], ["Author"])
        self.assertEqual(result["result"]["papers"][0]["url"], "https://example.com/paper")

    def test_fallback_http_error_exposes_only_status(self) -> None:
        request = server.httpx.Request("GET", "https://example.com/private-query")
        response = server.httpx.Response(429, request=request)
        with patch.object(server.httpx, "Client") as client:
            client.return_value.__enter__.return_value.get.return_value = response
            result = server._openalex_fallback("private-query", 1)
        self.assertEqual(result, {"ok": False, "error": "openalex_http_error", "status_code": 429})

    def test_fallback_local_error_exposes_only_type(self) -> None:
        with patch.object(server.httpx, "Client", side_effect=ValueError("private-value")):
            result = server._openalex_fallback("query", 1)
        self.assertEqual(result, {"ok": False, "error": "openalex_error: ValueError"})


if __name__ == "__main__":
    unittest.main()
