"""
Yaazhi Semantic Retriever — production-hardened.

Audit fixes applied (2026-05-10):
  M5 : hybrid_search SCAN capped at 500 keys; separate sorted-set index maintained.
  M6 : All litellm calls use await litellm.acompletion() — not asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Optional, TYPE_CHECKING

import logfire
import redis.asyncio as aioredis
import tiktoken

from config.settings import settings
from core.state import MemoryResult

if TYPE_CHECKING:
    from memory.vector_store import VectorStore

_MEMORY_INDEX_KEY = "yaazhi:memory_index"   # sorted set: score=timestamp, member=memory_id
_MAX_SCAN_KEYS = 500                         # M5 fix: cap Redis SCAN


class SemanticRetriever:
    """
    Semantic memory retriever with caching, reranking, and hybrid search.

    Wraps VectorStore.search() with a short Redis TTL cache (5 min).
    Reranking uses litellm.acompletion (M6 fix) to score candidates.
    hybrid_search is capped at _MAX_SCAN_KEYS to prevent O(N) Redis exhaustion.
    """

    def __init__(self, vector_store: Optional["VectorStore"] = None) -> None:
        if vector_store is None:
            from memory.vector_store import VectorStore

            vector_store = VectorStore()
        self._vs = vector_store
        self._redis: Optional[aioredis.Redis] = None
        self._tokenizer = tiktoken.get_encoding("cl100k_base")
        self._retrieval_ttl: int = 300  # 5 minutes

    def __repr__(self) -> str:
        return f"SemanticRetriever(vector_store={self._vs!r})"

    async def _ensure_redis(self) -> None:
        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True
            )

    async def ping(self) -> bool:
        return await self._vs.ping()

    # ------------------------------------------------------------------
    # retrieve — cached semantic search
    # ------------------------------------------------------------------

    async def retrieve(self, query: str, top_k: int = 5) -> list[MemoryResult]:
        """
        Retrieve semantically similar memories with Redis caching.

        Cache key: retrieval:{sha256(query)[:32]}:{top_k}  (5-minute TTL).
        """
        logfire.debug("SemanticRetriever.retrieve", query=query[:80], top_k=top_k)
        t_start = time.time()
        await self._ensure_redis()

        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:32]
        cache_key = f"retrieval:{digest}:{top_k}"
        cached_raw = await self._redis.get(cache_key)
        if cached_raw:
            try:
                cached_data = json.loads(cached_raw)
                results = [MemoryResult(**item) for item in cached_data]
                logfire.debug("SemanticRetriever.retrieve: cache hit", results=len(results))
                return results
            except Exception:
                pass  # corrupt cache — fall through to live search

        results = await self._vs.search(query, top_k=top_k)

        try:
            serializable = [r.model_dump(mode="json") for r in results]
            await self._redis.set(cache_key, json.dumps(serializable, ensure_ascii=False), ex=self._retrieval_ttl)
        except Exception as exc:
            logfire.warning("SemanticRetriever: failed to cache results", error=str(exc))

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info("SemanticRetriever.retrieve success", results=len(results), duration_ms=duration_ms)
        return results

    # ------------------------------------------------------------------
    # retrieve_with_rerank — M6 fix: use acompletion
    # ------------------------------------------------------------------

    async def retrieve_with_rerank(
        self, query: str, top_k: int = 10, rerank_to: int = 3
    ) -> list[MemoryResult]:
        """
        Retrieve top_k results then rerank using Groq LLM.

        M6 FIX: Uses await litellm.acompletion() — thread-safe async call.
        """
        logfire.debug(
            "SemanticRetriever.retrieve_with_rerank",
            query=query[:80],
            top_k=top_k,
            rerank_to=rerank_to,
        )
        t_start = time.time()
        candidates = await self.retrieve(query, top_k=top_k)
        if len(candidates) <= rerank_to:
            return candidates[:rerank_to]

        snippets = "\n".join(f"{i}. {r.text[:200]}" for i, r in enumerate(candidates))
        prompt = (
            f"Query: {query}\n\n"
            f"Passages:\n{snippets}\n\n"
            f"Return a JSON array of the {rerank_to} passage indices most relevant "
            f"to the query, ordered from most to least relevant. "
            f"Example: [2, 0, 4]. Respond with JSON only."
        )

        try:
            import litellm  # type: ignore

            model = settings.get_litellm_model("fast_tasks")
            # M6 FIX: acompletion (async, thread-safe)
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=64,
                temperature=0.0,
            )
            raw_json = response.choices[0].message.content.strip()
            indices: list[int] = json.loads(raw_json)
            reranked = [
                candidates[i] for i in indices if 0 <= i < len(candidates)
            ][:rerank_to]
        except Exception as exc:
            logfire.warning("Reranking failed, returning top candidates", error=str(exc))
            reranked = candidates[:rerank_to]

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "SemanticRetriever.retrieve_with_rerank success",
            reranked=len(reranked),
            duration_ms=duration_ms,
        )
        return reranked

    # ------------------------------------------------------------------
    # retrieve_from_session
    # ------------------------------------------------------------------

    async def retrieve_from_session(
        self, query: str, session_id: str
    ) -> list[MemoryResult]:
        """Retrieve memories filtered to a specific session."""
        logfire.debug(
            "SemanticRetriever.retrieve_from_session",
            query=query[:80],
            session_id=session_id[:8],
        )
        return await self._vs.search(
            query, top_k=5, filter={"session_id": session_id}
        )

    # ------------------------------------------------------------------
    # build_context
    # ------------------------------------------------------------------

    async def build_context(self, query: str, max_tokens: int = 2000) -> str:
        """
        Retrieve top memories and format as a numbered context string.

        Token budget enforced with tiktoken cl100k_base.
        """
        logfire.debug("SemanticRetriever.build_context", query=query[:80])
        results = await self.retrieve(query, top_k=5)
        if not results:
            return ""

        lines: list[str] = []
        for i, mem in enumerate(results, start=1):
            lines.append(f"{i}. [{mem.source}]: {mem.text}")

        context = "\n".join(lines)
        tokens = await asyncio.to_thread(self._tokenizer.encode, context)
        if len(tokens) > max_tokens:
            trimmed = await asyncio.to_thread(self._tokenizer.decode, tokens[:max_tokens])
            context = trimmed

        logfire.info(
            "SemanticRetriever.build_context success",
            chars=len(context),
            memories=len(results),
        )
        return context

    # ------------------------------------------------------------------
    # index_memory — maintains sorted-set index for bounded hybrid search
    # ------------------------------------------------------------------

    async def index_memory(self, memory_id: str, score: float | None = None) -> None:
        """
        Add a memory_id to the sorted-set index with current timestamp as score.

        This enables O(log N) ZRANGE lookups instead of O(N) SCAN in hybrid_search.
        """
        await self._ensure_redis()
        if score is None:
            score = time.time()
        try:
            await self._redis.zadd(_MEMORY_INDEX_KEY, {memory_id: score})
        except Exception as exc:
            logfire.warning("SemanticRetriever.index_memory failed", error=str(exc))

    # ------------------------------------------------------------------
    # hybrid_search — M5 fix: bounded SCAN + sorted-set index
    # ------------------------------------------------------------------

    async def hybrid_search(self, query: str) -> list[MemoryResult]:
        """
        Merge semantic search with keyword scan of recent Redis cache entries.

        M5 FIX:
          1. SCAN is capped at _MAX_SCAN_KEYS (500) — no unbounded O(N) ops.
          2. Uses yaazhi:memory_index sorted set when available for O(log N) access.

        Deduplication by memory_id before returning. Results sorted by score DESC.
        """
        logfire.debug("SemanticRetriever.hybrid_search", query=query[:80])
        t_start = time.time()
        await self._ensure_redis()

        semantic_results = await self.retrieve(query, top_k=5)
        seen_ids: set[str] = {r.memory_id for r in semantic_results}
        merged = list(semantic_results)

        query_lower = query.lower()
        query_words = [w for w in query_lower.split() if len(w) > 3]
        keyword_hits: list[MemoryResult] = []

        # Try sorted-set index first (O(log N))
        try:
            recent_ids = await self._redis.zrange(
                _MEMORY_INDEX_KEY, -200, -1
            )
            if recent_ids:
                for mid in recent_ids:
                    if mid in seen_ids:
                        continue
                    # Try retrieval cache key
                    cache_key = f"retrieval:{hashlib.sha256(mid.encode()).hexdigest()[:32]}:5"
                    raw = await self._redis.get(cache_key)
                    if not raw:
                        continue
                    try:
                        items: list[dict] = json.loads(raw)
                        for item in items:
                            if item.get("memory_id") in seen_ids:
                                continue
                            text = item.get("text", "").lower()
                            if query_words and any(w in text for w in query_words):
                                keyword_hits.append(MemoryResult(**item))
                                seen_ids.add(item["memory_id"])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
        except Exception:
            pass  # fall through to SCAN

        # Bounded SCAN fallback (M5: capped at _MAX_SCAN_KEYS)
        if not keyword_hits:
            cursor = 0
            scanned = 0
            while scanned < _MAX_SCAN_KEYS:
                cursor, keys = await self._redis.scan(
                    cursor, match="retrieval:*", count=100
                )
                for key in keys:
                    raw = await self._redis.get(key)
                    if not raw:
                        continue
                    try:
                        items = json.loads(raw)
                        for item in items:
                            if item.get("memory_id") in seen_ids:
                                continue
                            text = item.get("text", "").lower()
                            if query_words and any(w in text for w in query_words):
                                keyword_hits.append(MemoryResult(**item))
                                seen_ids.add(item["memory_id"])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                scanned += len(keys)
                if cursor == 0:
                    break

        merged.extend(keyword_hits)
        merged.sort(key=lambda r: r.score, reverse=True)

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info(
            "SemanticRetriever.hybrid_search success",
            total=len(merged),
            duration_ms=duration_ms,
        )
        return merged
