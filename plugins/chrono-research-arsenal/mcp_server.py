"""chrono-research-arsenal — shared external research arsenal MCP.

Tool: arxiv_search.

Used by all chrono roles needing external data (research, scout, scraping, etc).
Rebuilt from a predecessor project's research tools using FastMCP for consistency.
Sync `def` for blocking ops (FastMCP runs them in a background thread).
Per Rule 17.1 (TOOLS.md): never str(httpx_exc) — only status_code + reason_phrase.
"""
from __future__ import annotations

import logging
from typing import Any

import arxiv
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("chrono-research-arsenal")
logging.getLogger("arxiv").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


def _ok(payload: Any) -> dict[str, Any]:
    return {"ok": True, "result": payload}


def _err(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": reason, **extra}


def _openalex_fallback(query: str, max_results: int) -> dict[str, Any]:
    """Fallback academic search when export.arxiv.org keyword search is throttled."""
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(
                "https://api.openalex.org/works",
                params={
                    "search": query,
                    "per-page": max_results,
                    "select": "id,title,doi,publication_year,primary_location,authorships,abstract_inverted_index",
                },
                headers={"User-Agent": "claude-vibe-squad/1.0"},
            )
        response.raise_for_status()
        data = response.json()
        papers = []
        for item in data.get("results", []):
            authors = []
            for authorship in item.get("authorships", [])[:8]:
                author = authorship.get("author") or {}
                name = author.get("display_name")
                if name:
                    authors.append(name)
            location = item.get("primary_location") or {}
            papers.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "summary": None,
                    "authors": authors,
                    "published": item.get("publication_year"),
                    "pdf_url": location.get("pdf_url"),
                    "url": location.get("landing_page_url") or item.get("doi") or item.get("id"),
                    "source": "openalex_fallback",
                }
            )
        return _ok({"papers": papers, "source": "openalex_fallback"})
    except httpx.HTTPStatusError as exc:
        return _err("openalex_http_error", status_code=exc.response.status_code)
    except Exception as exc:
        return _err(f"openalex_error: {type(exc).__name__}")


@mcp.tool()
def arxiv_search(
    query: str,
    max_results: int = 10,
    categories: list[str] | None = None,
) -> dict[str, Any]:
    """Search arXiv. Returns paper metadata: id, title, summary, authors, published, pdf_url.

    `categories` filters by arXiv category (e.g. ["cs.AI", "cs.CL", "cs.LG"]).
    When supplied, query is wrapped as ``(cat:cs.AI OR cat:cs.CL OR ...) AND (<query>)``.
    """
    try:
        full_query = query
        if categories:
            cat_clause = " OR ".join(f"cat:{c}" for c in categories)
            full_query = f"({cat_clause}) AND ({query})"
        max_results = max(1, min(int(max_results), 10))
        # The arXiv API returns 429 under load; the arxiv package default retry
        # path can exceed model-lane tool timeouts. Fail fast and let the lane
        # use its approved fallback instead of blocking the whole task.
        client = arxiv.Client(page_size=max_results, delay_seconds=0.0, num_retries=0)
        search = arxiv.Search(
            query=full_query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.Relevance,
            sort_order=arxiv.SortOrder.Descending,
        )
        papers = []
        for r in client.results(search):
            papers.append({
                "id": r.entry_id,
                "title": r.title,
                "summary": r.summary,
                "authors": [a.name for a in r.authors],
                "published": r.published.isoformat() if r.published else None,
                "pdf_url": r.pdf_url,
            })
        return _ok({"papers": papers})
    except arxiv.HTTPError:
        return _openalex_fallback(query, max_results)
    except Exception as exc:
        # arxiv lib raises various exception types; we only want class name in the error,
        # never the message (could contain query/network details we don't want to leak).
        return _err(f"arxiv_error: {type(exc).__name__}")


if __name__ == "__main__":
    mcp.run()
