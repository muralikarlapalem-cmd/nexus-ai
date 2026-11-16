"""
NEXUS AI · agents/tools/search_tools.py
External search tools for agents:
  - web_search: Tavily web search (needs TAVILY_API_KEY), returns results with URLs
  - cve_lookup: NVD CVE database lookup
  - system_log_scan: Mock log scanner (replace with real log source)
"""

from dataclasses import dataclass, field

import httpx
from langchain_core.tools import tool
from loguru import logger

from rag.config import rag_settings

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


@dataclass
class WebSearchResult:
    """Outcome of a web search. `error` is set (and `results` empty) when it failed."""
    results: list[dict] = field(default_factory=list)   # [{title, url, content}]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def search_web(query: str, max_results: int | None = None) -> WebSearchResult:
    """
    Search the web with Tavily. Never raises: failures come back as `error`,
    so callers can tell the LLM plainly that web results are unavailable.
    """
    if not rag_settings.tavily_api_key:
        return WebSearchResult(error="web search is not configured (TAVILY_API_KEY is empty)")

    n = max(1, min(max_results or rag_settings.web_search_max_results, 5))
    payload = {
        "query": query[:400],          # Tavily rejects very long queries
        "max_results": n,
        "search_depth": "basic",
        "include_answer": False,
    }
    try:
        async with httpx.AsyncClient(timeout=rag_settings.web_search_timeout) as client:
            resp = await client.post(
                TAVILY_SEARCH_URL,
                json=payload,
                headers={"Authorization": f"Bearer {rag_settings.tavily_api_key}"},
            )
        if resp.status_code != 200:
            detail = resp.text[:200].replace("\n", " ")
            logger.warning(f"Tavily search failed: HTTP {resp.status_code} {detail}")
            return WebSearchResult(error=f"web search failed (HTTP {resp.status_code})")
        items = resp.json().get("results", [])
    except httpx.TimeoutException:
        logger.warning("Tavily search timed out")
        return WebSearchResult(error="web search timed out")
    except Exception as e:
        logger.warning(f"Tavily search error: {e}")
        return WebSearchResult(error=f"web search error: {type(e).__name__}")

    results = [
        {
            "title": (r.get("title") or r.get("url") or "Untitled").strip(),
            "url": r.get("url", ""),
            "content": (r.get("content") or "").strip()[:700],
        }
        for r in items
        if r.get("url")
    ]
    return WebSearchResult(results=results)


def format_web_results(result: WebSearchResult) -> str:
    """Text for an LLM prompt; failures are stated plainly, never passed off as results."""
    if not result.ok:
        return f"WEB SEARCH UNAVAILABLE ({result.error}). There are no web results; do not cite web sources."
    if not result.results:
        return "Web search returned no results."
    return "\n\n".join(
        f"[W{i}] {r['title']}\nURL: {r['url']}\n{r['content']}"
        for i, r in enumerate(result.results, 1)
    )


@tool
async def web_search(query: str, max_results: int = 3) -> str:
    """
    Search the web for technical solutions, error messages, and known issues.
    Use this when the knowledge base doesn't have a relevant answer.

    Args:
        query: Technical search query (e.g. 'PostgreSQL max_connections error fix')
        max_results: Number of results (1-5)
    """
    return format_web_results(await search_web(query, max_results))


@tool
async def cve_lookup(keyword: str) -> str:
    """
    Look up known CVEs (Common Vulnerabilities and Exposures) related to a software or error.
    Queries the NVD (National Vulnerability Database) API.

    Args:
        keyword: Software name or vulnerability keyword (e.g. 'OpenSSL', 'Apache Log4j')
    """
    try:
        url = "https://services.nvd.nist.gov/rest/json/cves/2.0"
        params = {"keywordSearch": keyword, "resultsPerPage": 5}

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        vulns = data.get("vulnerabilities", [])
        if not vulns:
            return f"No CVEs found for '{keyword}'"

        parts = []
        for item in vulns[:3]:
            cve = item.get("cve", {})
            cve_id = cve.get("id", "Unknown")
            desc = cve.get("descriptions", [{}])[0].get("value", "No description")[:300]
            severity = "Unknown"
            metrics = cve.get("metrics", {})
            if "cvssMetricV31" in metrics:
                severity = metrics["cvssMetricV31"][0]["cvssData"].get("baseSeverity", "Unknown")
            elif "cvssMetricV2" in metrics:
                severity = metrics["cvssMetricV2"][0].get("baseSeverity", "Unknown")
            parts.append(f"CVE: {cve_id} | Severity: {severity}\n{desc}")

        return "\n\n".join(parts)

    except httpx.TimeoutException:
        return f"NVD API timeout — could not fetch CVEs for '{keyword}'"
    except Exception as e:
        return f"CVE lookup error: {str(e)}"


@tool
async def scan_system_logs(log_type: str, keywords: str, lines: int = 50) -> str:
    """
    Scan recent system logs for errors, warnings, or specific keywords.
    In production, connect this to your log aggregation system (ELK, CloudWatch, etc.).

    Args:
        log_type: 'application', 'system', 'database', 'network'
        keywords: Comma-separated keywords to filter for
        lines: Number of recent log lines to scan
    """
    # STUB — replace with real log source (ELK/CloudWatch/Loki API call)
    # Example real implementation:
    # async with httpx.AsyncClient() as client:
    #     resp = await client.get(f"{ELASTICSEARCH_URL}/logs-*/_search", json=query)
    #     return format_log_hits(resp.json())

    keyword_list = [k.strip() for k in keywords.split(",")]
    return (
        f"[STUB] Log scan for '{log_type}' logs filtering: {keyword_list}\n"
        f"Connect to your log aggregator (ELK/CloudWatch/Loki) in "
        f"agents/tools/search_tools.py:scan_system_logs() to enable real log scanning.\n"
        f"Simulated result: No critical errors found in recent {lines} lines."
    )
