"""
NEXUS AI · agents/tools/search_tools.py
External search tools for agents:
  - web_search: DuckDuckGo search (no API key needed)
  - cve_lookup: NVD CVE database lookup
  - system_log_scan: Mock log scanner (replace with real log source)
"""

from langchain_core.tools import tool


@tool
async def web_search(query: str, max_results: int = 3) -> str:
    """
    Search the web for technical solutions, error messages, and known issues.
    Use this when the knowledge base doesn't have a relevant answer.

    Args:
        query: Technical search query (e.g. 'PostgreSQL max_connections error fix')
        max_results: Number of results (1-5)
    """
    try:
        from langchain_community.tools import DuckDuckGoSearchRun
        search = DuckDuckGoSearchRun()
        result = search.run(query)
        return result[:2000] if result else "No web results found."
    except Exception as e:
        return f"Web search unavailable: {str(e)}"


@tool
async def cve_lookup(keyword: str) -> str:
    """
    Look up known CVEs (Common Vulnerabilities and Exposures) related to a software or error.
    Queries the NVD (National Vulnerability Database) API.

    Args:
        keyword: Software name or vulnerability keyword (e.g. 'OpenSSL', 'Apache Log4j')
    """
    import httpx

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
