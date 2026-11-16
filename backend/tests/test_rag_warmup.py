"""
NEXUS AI · tests/test_rag_warmup.py
The RAG models are loaded at process start (in the background), so the first
AI Knowledge Query doesn't wait for them. No real model is loaded here.
"""

import rag.ingestion.embedder as embedder
import rag.retrieval.reranker as reranker
from rag.config import rag_settings
from rag.warmup import start_warm_up, warm_up_models


class FakeCrossEncoder:
    def __init__(self):
        self.calls = []

    def predict(self, pairs, show_progress_bar=False):
        self.calls.append(pairs)
        return [0.0] * len(pairs)


def test_warm_up_loads_embedder_and_reranker(monkeypatch):
    model = FakeCrossEncoder()
    loaded = []
    monkeypatch.setattr(rag_settings, "reranker_enabled", True)
    monkeypatch.setattr(embedder, "get_embedder", lambda: loaded.append("embedder"))
    monkeypatch.setattr(reranker, "_get_cross_encoder", lambda: model)

    start_warm_up().join(timeout=5)

    assert loaded == ["embedder"]
    assert model.calls == [[("warm up", "warm up")]]   # first prediction done up front


def test_warm_up_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("no network")

    monkeypatch.setattr(embedder, "get_embedder", broken)
    warm_up_models()          # logs a warning; the process must still start


def test_reranker_is_loaded_once(monkeypatch):
    builds = []
    reranker._load_cross_encoder.cache_clear()
    monkeypatch.setattr("sentence_transformers.CrossEncoder",
                        lambda *a, **kw: builds.append(1) or FakeCrossEncoder())
    try:
        first = reranker._get_cross_encoder()
        assert reranker._get_cross_encoder() is first
        assert builds == [1]
    finally:
        reranker._load_cross_encoder.cache_clear()
