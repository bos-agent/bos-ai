"""Component eval skeleton — retrieval recall@k over a stub dataset."""

import pytest
from conftest import InMemMemoryExtension

from bos.plugins.memory.eval import RetrievalCase, recall_at_k


@pytest.mark.asyncio
async def test_recall_at_k_perfect_retrieval():
    store = InMemMemoryExtension()
    pg = await store.ingest_memory("user prefers PostgreSQL 16", tags=["db"])
    await store.ingest_memory("unrelated note about cats", tags=["pets"])
    cases = [RetrievalCase(query="postgresql", relevant_ids={pg})]
    score = await recall_at_k(store, cases, k=5)
    assert score == 1.0


@pytest.mark.asyncio
async def test_recall_at_k_miss():
    store = InMemMemoryExtension()
    await store.ingest_memory("note about cats", tags=["pets"])
    cases = [RetrievalCase(query="postgresql", relevant_ids={"missing"})]
    score = await recall_at_k(store, cases, k=5)
    assert score == 0.0


@pytest.mark.asyncio
async def test_recall_at_k_skips_empty_relevant_cases_without_diluting():
    """Regression: a case with no relevant ids must be excluded from both the
    numerator AND the denominator. One perfect case + one empty case should
    score 1.0, not 0.5."""
    store = InMemMemoryExtension()
    pg = await store.ingest_memory("user prefers PostgreSQL 16", tags=["db"])
    cases = [
        RetrievalCase(query="postgresql", relevant_ids={pg}),
        RetrievalCase(query="no labels", relevant_ids=set()),
    ]
    score = await recall_at_k(store, cases, k=5)
    assert score == 1.0


@pytest.mark.asyncio
async def test_recall_at_k_all_empty_returns_zero():
    store = InMemMemoryExtension()
    await store.ingest_memory("something", tags=["x"])
    cases = [RetrievalCase(query="a"), RetrievalCase(query="b")]
    score = await recall_at_k(store, cases, k=5)
    assert score == 0.0
