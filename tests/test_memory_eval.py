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
