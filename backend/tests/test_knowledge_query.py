"""
NEXUS AI · tests/test_knowledge_query.py
AI Knowledge Query: where the answer comes from.
  - a good knowledge-base match → answered from it, web not searched
  - weak matches are dropped (not shown to the LLM as "context") → web search
  - nothing anywhere → the LLM may answer from general IT knowledge
Before this, every question the runbooks didn't cover got
"The context provided does not contain...".
No real LLM, Qdrant or Tavily calls are made.
"""

from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

import rag.pipeline as pipeline
import rag.retrieval.reranker as reranker
from agents.tools.search_tools import WebSearchResult
from rag.config import rag_settings


class FakeLLM:
    def __init__(self):
        self.prompts = []
        self.answer = "answer"

    async def ainvoke(self, messages):
        self.prompts.append(messages[-1].content)
        return SimpleNamespace(content=self.answer)

    async def astream(self, messages):
        self.prompts.append(messages[-1].content)
        half = len(self.answer) // 2
        for piece in (self.answer[:half], self.answer[half:]):
            yield SimpleNamespace(content=piece)


@pytest.fixture
def setup(monkeypatch):
    llm = FakeLLM()
    state = {"web_calls": 0}
    monkeypatch.setattr(rag_settings, "kb_min_relevance", 0.0)
    monkeypatch.setattr(rag_settings, "reranker_enabled", True)
    monkeypatch.setattr(reranker, "_get_cross_encoder", lambda: object())
    monkeypatch.setattr(pipeline, "_get_llm", lambda streaming=False: llm)

    def configure(kb_docs, web=None, technical=True):
        async def fake_check(query):
            state["checked"] = True
            return technical

        monkeypatch.setattr(pipeline, "_needs_web_search", fake_check)

        async def fake_hybrid(**kw):
            return kb_docs

        async def fake_rerank(query, candidates, top_k=None):
            return candidates

        async def fake_web(query):
            state["web_calls"] += 1
            return web or WebSearchResult()

        monkeypatch.setattr(pipeline, "hybrid_search", fake_hybrid)
        monkeypatch.setattr(pipeline, "rerank_async", fake_rerank)
        monkeypatch.setattr(pipeline, "_search_web", fake_web)
        return llm, state

    return configure


def _doc(name, text="runbook text"):
    return Document(page_content=text, metadata={"file_name": name, "source_type": "runbook"})


@pytest.mark.asyncio
async def test_good_kb_match_is_used_and_web_skipped(setup):
    llm, state = setup([(_doc("postgres_runbook.md", "max_connections fix"), 5.1)])
    llm.answer = "Raise max_connections [1]."          # cited by its marker
    result = await pipeline.rag_query("database refusing connections")

    assert state["web_calls"] == 0
    assert result["basis"] == "knowledge_base"
    assert [s["source"] for s in result["sources"]] == ["postgres_runbook.md"]
    assert "max_connections fix" in llm.prompts[0]


@pytest.mark.asyncio
async def test_weak_kb_matches_are_dropped_and_web_is_searched(setup):
    web = WebSearchResult(results=[{"title": "ORA-01555 explained",
                                    "url": "https://example.com/ora", "content": "snapshot too old"}])
    llm, state = setup([(_doc("printer.md", "printer toner"), -10.8)], web)
    llm.answer = "Snapshot too old. See https://example.com/ora"
    result = await pipeline.rag_query("what does ORA-01555 mean")

    assert state["web_calls"] == 1
    assert result["basis"] == "web"
    assert result["sources"] == [{"source": "ORA-01555 explained", "source_type": "web", "score": None,
                                  "excerpt": "snapshot too old", "url": "https://example.com/ora"}]
    assert "printer toner" not in llm.prompts[0]      # unrelated runbook not passed as context
    assert "https://example.com/ora" in llm.prompts[0]


@pytest.mark.asyncio
async def test_nothing_found_falls_back_to_general_knowledge(setup):
    llm, _ = setup([], WebSearchResult(error="web search failed (HTTP 432)"))
    result = await pipeline.rag_query("what is an oracle database")

    assert result["basis"] == "general_knowledge"
    assert result["sources"] == []
    assert "WEB SEARCH UNAVAILABLE" in llm.prompts[0]


@pytest.mark.asyncio
async def test_match_for_a_different_error_code_counts_as_weak(setup):
    """A high-scoring ORA-01031 runbook must not block the web search for ORA-01555."""
    web = WebSearchResult(results=[{"title": "ORA-01555", "url": "https://example.com/ora", "content": "..."}])
    llm, state = setup([(_doc("ora_01031.md", "Fix ORA-01031: insufficient privileges"), 4.0)], web)
    llm.answer = "Snapshot too old [W1]."
    result = await pipeline.rag_query("what does ORA-01555 mean")

    assert state["web_calls"] == 1 and result["basis"] == "web"
    assert "insufficient privileges" not in llm.prompts[0]


@pytest.mark.asyncio
async def test_match_mentioning_the_code_is_kept(setup):
    llm, state = setup([(_doc("ora_01555.md", "ORA-01555: raise UNDO_RETENTION"), 4.0)])
    llm.answer = "Raise UNDO_RETENTION [1]."
    result = await pipeline.rag_query("what does ora-01555 mean")

    assert state["web_calls"] == 0 and result["basis"] == "knowledge_base"


@pytest.mark.asyncio
async def test_non_technical_question_skips_the_web(setup):
    llm, state = setup([], technical=False)
    llm.answer = "I help with IT issues, for example ..."
    result = await pipeline.rag_query("what kind of questions can we ask")

    assert state["web_calls"] == 0 and result["basis"] == "general_knowledge"
    assert "not a technical question" in llm.prompts[0]


@pytest.mark.asyncio
async def test_good_kb_match_skips_the_web_check(setup):
    llm, state = setup([(_doc("vpn.md"), 3.0)])
    await pipeline.rag_query("vpn drops")
    assert "checked" not in state          # no extra LLM call when the KB answers


@pytest.mark.asyncio
async def test_uncited_sources_are_not_listed(setup):
    """A declined or general answer must not list web pages / runbooks it didn't use."""
    web = WebSearchResult(results=[{"title": "Poem Generator", "url": "https://poems.example", "content": "..."}])
    llm, _ = setup([], web)
    llm.answer = "I can only help with IT questions."
    result = await pipeline.rag_query("write a poem for me")

    assert result["sources"] == [] and result["basis"] == "general_knowledge"


def test_cited_sources_removes_duplicates():
    s = {"source": "vpn.md", "source_type": "runbook", "score": 3.0, "excerpt": "", "url": None}
    used, basis = pipeline.cited_sources("Per the vpn runbook (vpn.md) ...", [s, dict(s, score=2.0)])
    assert len(used) == 1 and basis == "knowledge_base"


@pytest.mark.asyncio
async def test_stream_sends_tokens_then_cited_sources(setup):
    llm, _ = setup([(_doc("vpn.md"), 3.0)])
    llm.answer = "Restart the client (vpn.md)."
    events = [e async for e in pipeline.rag_query_stream("vpn drops")]

    assert "".join(e["token"] for e in events[:-1]) == "Restart the client (vpn.md)."
    assert events[-1]["basis"] == "knowledge_base" and events[-1]["sources"][0]["source"] == "vpn.md"


def test_prompt_allows_general_it_answers():
    prompt = pipeline.RAG_SYSTEM_PROMPT
    assert "ONLY the context" not in prompt
    assert "general IT knowledge" in prompt
    assert "what you can do" in prompt
