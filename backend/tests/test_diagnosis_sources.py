"""
NEXUS AI · tests/test_diagnosis_sources.py
Where the Diagnosis Agent gets its answer from:
  - the web (Tavily) is searched only when the knowledge base has no good match
  - search failures are told to the LLM plainly, never passed off as results
  - the internal documents / web pages used are saved on the ticket
  - the Qdrant client is not reused across event loops (Celery runs each task
    on a new loop, which broke every RAG search after a worker's first task)
No real LLM, Qdrant or Tavily calls are made.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.documents import Document

import agents.diagnosis_agent as diag
import agents.tools.search_tools as search_tools
from agents.tools.rag_tool import KBSearchResult, format_kb_results
from agents.tools.search_tools import WebSearchResult, format_web_results, search_web
from rag.config import rag_settings


def _doc(name):
    return Document(page_content=f"content of {name}", metadata={"file_name": name})


# ── Knowledge base strength ──────────────────────────────────

def test_kb_strength_uses_reranker_threshold(monkeypatch):
    monkeypatch.setattr(rag_settings, "kb_min_relevance", 0.0)
    assert KBSearchResult(docs=[(_doc("a.md"), 2.4)], reranked=True).is_strong
    assert not KBSearchResult(docs=[(_doc("a.md"), -6.0)], reranked=True).is_strong
    assert not KBSearchResult(docs=[], reranked=True).is_strong
    assert not KBSearchResult(error="Event loop is closed").is_strong
    # Without reranker scores relevance can't be judged: any result counts
    assert KBSearchResult(docs=[(_doc("a.md"), 0.016)], reranked=False).is_strong


def test_kb_error_is_not_presented_as_content():
    text = format_kb_results(KBSearchResult(error="ResponseHandlingException: Event loop is closed"))
    assert "UNAVAILABLE" in text
    assert "Event loop" not in text            # the raw error must not reach the LLM as a "clue"


def test_weak_kb_results_are_flagged():
    text = format_kb_results(KBSearchResult(docs=[(_doc("printer.md"), -11.0)], reranked=True))
    assert "none is a good match" in text


# ── Tavily web search ────────────────────────────────────────

def _mock_tavily(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(search_tools.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.asyncio
async def test_web_search_disabled_without_key(monkeypatch):
    monkeypatch.setattr(rag_settings, "tavily_api_key", "")
    result = await search_web("nginx 502")
    assert not result.ok and "TAVILY_API_KEY" in result.error


@pytest.mark.asyncio
async def test_web_search_returns_titles_and_urls(monkeypatch):
    monkeypatch.setattr(rag_settings, "tavily_api_key", "tvly-test")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"title": "Fix 502 Bad Gateway", "url": "https://example.com/502", "content": "Raise proxy_read_timeout"},
            {"title": "No url", "content": "dropped"},
        ]})

    _mock_tavily(monkeypatch, handler)
    result = await search_web("nginx 502", max_results=3)

    assert seen["auth"] == "Bearer tvly-test"
    assert seen["body"]["query"] == "nginx 502" and seen["body"]["max_results"] == 3
    assert result.ok
    assert result.results == [{"title": "Fix 502 Bad Gateway", "url": "https://example.com/502",
                               "content": "Raise proxy_read_timeout"}]
    assert "URL: https://example.com/502" in format_web_results(result)


@pytest.mark.asyncio
async def test_web_search_failure_is_reported_not_passed_as_results(monkeypatch):
    monkeypatch.setattr(rag_settings, "tavily_api_key", "tvly-test")
    _mock_tavily(monkeypatch, lambda request: httpx.Response(432, text="usage limit exceeded"))

    result = await search_web("nginx 502")

    assert not result.ok and "432" in result.error
    text = format_web_results(result)
    assert "WEB SEARCH UNAVAILABLE" in text and "do not cite" in text


# ── Diagnosis agent ──────────────────────────────────────────

class FakeLLM:
    prompts = []

    def __init__(self, **kwargs):
        pass

    async def ainvoke(self, messages):
        FakeLLM.prompts.append(messages[-1].content)
        return SimpleNamespace(content=json.dumps({
            "root_cause": "upstream timeout",
            "confidence": 0.7,
            "evidence": [],
            "resolution_steps": [{"step": 1, "action": "Raise proxy_read_timeout"}],
            "escalation_needed": False,
            "references": [],
        }))


@pytest.fixture
def run_diagnosis(monkeypatch):
    """Run the agent with fake KB / web / LLM; returns (state, web_calls)."""
    FakeLLM.prompts = []
    monkeypatch.setattr(diag, "ChatOpenAI", FakeLLM)
    monkeypatch.setattr(diag, "check_sla_status",
                        SimpleNamespace(ainvoke=lambda _: asyncio.sleep(0, result="SLA ok")))

    async def _run(kb: KBSearchResult, web: WebSearchResult | None = None):
        web_calls = []

        async def fake_kb(*a, **kw):
            return kb

        async def fake_web(query, max_results=None):
            web_calls.append(query)
            return web or WebSearchResult()

        monkeypatch.setattr(diag, "search_knowledge_base", fake_kb)
        monkeypatch.setattr(diag, "search_web", fake_web)
        state = await diag.diagnosis_agent({
            "ticket_id": "t1", "ticket_number": "NX-2026-00001", "title": "Nginx 502",
            "description": "Users get 502", "category": "network", "priority": "high",
            "triage_result": {"keywords": ["nginx", "502"]}, "iteration": 1,
        })
        return state, web_calls

    return _run


@pytest.mark.asyncio
async def test_strong_kb_match_skips_web(run_diagnosis):
    state, web_calls = await run_diagnosis(
        KBSearchResult(docs=[(_doc("nginx_runbook.md"), 6.2)], reranked=True))

    assert web_calls == []
    d = state["diagnosis_result"]
    assert d["knowledge_sources"] == ["nginx_runbook.md"]
    assert d["web_sources"] == []
    assert d["answer_basis"] == "knowledge_base"
    assert d["search_summary"]["web"] == "not_needed"
    assert "- Knowledge base: nginx_runbook.md" in state["final_resolution"]
    assert "Not searched" in FakeLLM.prompts[0]


@pytest.mark.asyncio
async def test_weak_kb_falls_back_to_web_and_saves_urls(run_diagnosis):
    web = WebSearchResult(results=[{"title": "Fix 502", "url": "https://example.com/502", "content": "..."}])
    state, web_calls = await run_diagnosis(
        KBSearchResult(docs=[(_doc("printer.md"), -9.5)], reranked=True), web)

    assert len(web_calls) == 1
    d = state["diagnosis_result"]
    assert d["knowledge_sources"] == []                       # weak matches aren't credited
    assert d["web_sources"] == [{"title": "Fix 502", "url": "https://example.com/502"}]
    assert d["answer_basis"] == "web"
    assert d["search_summary"]["knowledge_base"] == "weak"
    assert "- Web: Fix 502 - https://example.com/502" in state["final_resolution"]
    assert "[W1] Fix 502" in FakeLLM.prompts[0]


@pytest.mark.asyncio
async def test_kb_error_and_web_failure_are_stated_plainly(run_diagnosis):
    state, web_calls = await run_diagnosis(
        KBSearchResult(error="ResponseHandlingException: Event loop is closed"),
        WebSearchResult(error="web search failed (HTTP 432)"))

    assert len(web_calls) == 1
    d = state["diagnosis_result"]
    assert d["answer_basis"] == "model_general_knowledge"
    assert d["search_summary"] == {"knowledge_base": "error", "knowledge_base_top_score": None,
                                   "web": "error", "web_error": "web search failed (HTTP 432)"}
    assert "general IT knowledge" in state["final_resolution"]
    prompt = FakeLLM.prompts[0]
    assert "KNOWLEDGE BASE UNAVAILABLE" in prompt and "WEB SEARCH UNAVAILABLE" in prompt
    assert "Event loop" not in prompt


# ── Qdrant client per event loop ─────────────────────────────

def test_qdrant_client_is_recreated_for_each_event_loop():
    from rag.retrieval.vector_store import get_qdrant_client

    async def grab():
        return get_qdrant_client(), get_qdrant_client()

    a1, a2 = asyncio.run(grab())
    b1, _ = asyncio.run(grab())
    assert a1 is a2          # reused within one loop
    assert a1 is not b1      # a new loop (a new Celery task) gets a fresh client
