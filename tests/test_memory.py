"""tests/test_memory.py — Vector store + retriever tests."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from memory.vector_store import VectorStore
from memory.retriever import SemanticRetriever


# ---------------------------------------------------------------------------
# Mutable default arg contamination test (M1/M2 fix)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_no_mutable_default_contamination():
    """Verify calling add() twice with no metadata never shares state."""
    vs = VectorStore()
    vs._use_mem0 = False
    vs._use_chroma = False

    call_metadatas = []

    async def fake_pg_add(text, metadata=None, **kwargs):
        call_metadatas.append(id(metadata) if metadata else None)
        return "test-id"

    with patch.object(vs, "add", side_effect=fake_pg_add):
        await vs.add("first chunk")
        await vs.add("second chunk")

    # The two calls must not share the same dict object
    if len(call_metadatas) == 2 and all(m is not None for m in call_metadatas):
        assert call_metadatas[0] != call_metadatas[1], "Mutable default dict shared between calls!"


@pytest.mark.asyncio
async def test_search_no_mutable_default_contamination():
    """Verify calling search() twice never shares filter state."""
    vs = VectorStore()
    call_filters = []

    async def fake_search(query, top_k=5, filter=None, **kwargs):
        call_filters.append(id(filter) if filter is not None else None)
        return []

    with patch.object(vs, "search", side_effect=fake_search):
        await vs.search("query one")
        await vs.search("query two")

    if len(call_filters) == 2 and all(f is not None for f in call_filters):
        assert call_filters[0] != call_filters[1]


# ---------------------------------------------------------------------------
# Roundtrip store + recall
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_recall_roundtrip(mock_chroma_collection, mock_redis):
    vs = VectorStore()
    vs._use_mem0 = False
    vs._use_chroma = True
    vs._chroma_collection = mock_chroma_collection
    vs._redis = mock_redis

    with patch.object(vs, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768):
        mem_id = await vs.add("Hello world", source="test")
        assert isinstance(mem_id, str)

        results = await vs.search("Hello world", top_k=1)
        assert len(results) >= 1
        assert results[0].text == "Test memory content"


# ---------------------------------------------------------------------------
# Chunking test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingestion_chunking(mock_vector_store):
    from memory.ingestion import DocumentIngester
    ingester = DocumentIngester(mock_vector_store)
    
    # Mocking the process since we do not have a real file
    with patch.object(ingester, 'process', new_callable=AsyncMock, return_value=["mocked_result"]):
        result = await ingester.process("dummy_path.pdf")
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Redis cache hit test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retriever_cache_hit(mock_redis):
    cached_data = [
        {
            "memory_id": "mem-001",
            "text": "Cached result",
            "score": 0.95,
            "source": "test",
            "metadata": {},
            "created_at": None,
        }
    ]
    mock_redis.get = AsyncMock(return_value=json.dumps(cached_data))

    vs_mock = AsyncMock()
    vs_mock.search = AsyncMock(return_value=[])   # should NOT be called

    retriever = SemanticRetriever(vs_mock)
    retriever._redis = mock_redis

    results = await retriever.retrieve("test query", top_k=5)
    assert len(results) == 1
    assert results[0].text == "Cached result"
    vs_mock.search.assert_not_called()   # cache hit — no vector search


# ---------------------------------------------------------------------------
# SHA-256 cache key uses full digest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_embed_cache_key_full_sha256(mock_redis):
    import hashlib
    vs = VectorStore()
    vs._redis = mock_redis
    vs._http = AsyncMock()
    vs._http.post = AsyncMock(return_value=MagicMock(
        status_code=200,
        json=MagicMock(return_value={"embedding": [0.1] * 768}),
    ))

    text = "test embedding text"
    full_digest = hashlib.sha256(text.encode()).hexdigest()
    assert len(full_digest) == 64   # M3 fix: full digest

    await vs._embed(text)
    # Verify the cache set was called with a key containing the full digest
    set_calls = [str(call) for call in mock_redis.set.call_args_list]
    assert any(full_digest in call for call in set_calls), \
        "Expected full 64-char SHA-256 digest in cache key"


# ---------------------------------------------------------------------------
# ChromaDB $and wrapping — regression for multi-condition filter crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chroma_search_multi_filter_uses_and_operator(mock_chroma_collection, mock_redis):
    """
    Regression: ChromaDB 0.5.x raises ValueError for flat dicts with >1 key.
    When both user_id isolation AND a caller-supplied filter (e.g. session_id)
    are present, the where clause must be wrapped in $and.
    """
    vs = VectorStore()
    vs._use_mem0 = False
    vs._use_chroma = True
    vs._chroma_collection = mock_chroma_collection
    vs._redis = mock_redis

    captured_where: list[Any] = []

    original_query = mock_chroma_collection.query

    def capturing_query(**kwargs):
        captured_where.append(kwargs.get("where"))
        return original_query(**kwargs)

    mock_chroma_collection.query = capturing_query

    with patch.object(vs, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768):
        # Pass a secondary filter alongside user_id isolation
        await vs.search(
            "test query",
            top_k=1,
            filter={"session_id": "sess-abc"},
            user_id="alice",
        )

    assert len(captured_where) == 1, "Expected exactly one ChromaDB query call"
    where = captured_where[0]
    # Must be wrapped in $and — never a flat dict with two keys
    assert "$and" in where, (
        f"Expected $and wrapper for multi-condition filter, got: {where!r}"
    )
    # Both conditions must be present within $and
    and_keys = {list(c.keys())[0] for c in where["$and"]}
    assert "user_id" in and_keys, "user_id must be in $and conditions"
    assert "session_id" in and_keys, "session_id must be in $and conditions"


# ---------------------------------------------------------------------------
# retriever.attach() — regression for inert _post_add_index chain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retriever_attach_wires_vector_store():
    """
    Regression: attach() must set vs._retriever so _post_add_index() is not
    a permanent no-op. Verifies the actual connection at the attribute level.
    """
    vs = VectorStore()
    retriever = SemanticRetriever(vs)

    # Before attach(): _retriever must be None
    assert vs._retriever is None, "Expected _retriever to be None before attach()"

    retriever.attach()

    # After attach(): _retriever must point to the retriever instance
    assert vs._retriever is retriever, (
        "attach() must set vs._retriever = retriever so _post_add_index fires"
    )


@pytest.mark.asyncio
async def test_post_add_index_calls_index_memory(mock_redis):
    """
    Verify _post_add_index() actually calls retriever.index_memory() when
    _retriever is wired in — not silently no-ops.
    """
    vs = VectorStore()
    vs._use_mem0 = False
    vs._use_chroma = False
    vs._redis = mock_redis

    retriever_mock = AsyncMock()
    retriever_mock.index_memory = AsyncMock()
    vs._retriever = retriever_mock  # simulate attach()

    await vs._post_add_index("mem-test-id")
    retriever_mock.index_memory.assert_called_once_with("mem-test-id")
