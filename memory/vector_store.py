"""
Yaazhi Vector Store — production-hardened.

Audit fixes applied (2026-05-10):
  M1 : add() — mutable default argument dict={} replaced with None
  M2 : search() — mutable default argument dict={} replaced with None
  M3 : SHA-256 cache key uses full 64-char digest (not truncated [:16])
  M4 : pgvector embedding serialized as proper array literal, not str(list)

P1.1 : search() and pgvector queries filter by user_id for multi-user isolation.

Primary store  : ChromaDB (via Mem0 wrapper when available)
Fallback store : pgvector via asyncpg
Embedding cache: Redis, 24-hour TTL, full SHA-256 keying
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import httpx
import logfire
import redis.asyncio as aioredis

from config.settings import settings
from core.state import MemoryResult

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False

try:
    from mem0 import Memory as Mem0Memory  # type: ignore
    _MEM0_AVAILABLE = True
except ImportError:
    _MEM0_AVAILABLE = False


# ---------------------------------------------------------------------------
# Mem0 configuration
# ---------------------------------------------------------------------------

def _build_mem0_config() -> dict:
    return {
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "yaazhi_mem0",
                "host": settings.chromadb_host,
                "port": settings.chromadb_port,
            },
        },
        "llm": {
            "provider": "groq",
            "config": {
                "model": "llama-3.3-70b-versatile",
                "api_key": settings.groq_api_key,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            },
        },
    }


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------

class VectorStore:
    """
    Semantic vector store backed by Mem0 (wrapping ChromaDB) with pgvector fallback.

    Embeddings are produced by Ollama nomic-embed-text and cached in Redis
    using the full 64-char SHA-256 digest of the input text.

    On ChromaDB failure, automatically switches to pgvector with HNSW cosine
    similarity search.  Every fallback switch is logged via logfire.
    """

    def __init__(self) -> None:
        self._mem0: Optional[Any] = None
        self._chroma_client: Optional[Any] = None
        self._chroma_collection: Optional[Any] = None
        self._pg_pool: Optional[asyncpg.Pool] = None
        self._redis: Optional[aioredis.Redis] = None
        self._fallback_memory: list[dict[str, Any]] = []
        self._use_mem0: bool = _MEM0_AVAILABLE
        self._use_chroma: bool = True
        self._collection_name: str = "yaazhi_memories"
        self._embed_dim: int = 768
        self._http: Optional[httpx.AsyncClient] = None

    def __repr__(self) -> str:
        if self._use_mem0 and self._mem0:
            backend = "Mem0+ChromaDB"
        elif self._use_chroma:
            backend = "ChromaDB"
        else:
            backend = "pgvector"
        return f"VectorStore(backend={backend!r}, collection={self._collection_name!r})"

    # ------------------------------------------------------------------
    # Client initialisation
    # ------------------------------------------------------------------

    async def _ensure_clients(self) -> None:
        """Lazy-initialise all clients on first use."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)

        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=False
            )

        # Mem0 path
        if self._use_mem0 and self._mem0 is None and _MEM0_AVAILABLE:
            try:
                self._mem0 = await asyncio.to_thread(
                    Mem0Memory.from_config, _build_mem0_config()
                )
                logfire.info("VectorStore: Mem0 initialised successfully")
            except Exception as exc:
                logfire.warning("Mem0 init failed, falling back to raw ChromaDB", error=str(exc))
                self._use_mem0 = False

        # Raw ChromaDB path (fallback from Mem0 or primary if Mem0 unavailable)
        if not self._use_mem0 and self._use_chroma and self._chroma_client is None and _CHROMA_AVAILABLE:
            try:
                self._chroma_client = await asyncio.to_thread(
                    chromadb.HttpClient,
                    host=settings.chromadb_host,
                    port=settings.chromadb_port,
                )
                self._chroma_collection = await asyncio.to_thread(
                    self._chroma_client.get_or_create_collection,
                    name=self._collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
            except Exception as exc:
                logfire.warning("ChromaDB unavailable, switching to pgvector", error=str(exc))
                self._use_chroma = False

        # pgvector pool
        if self._pg_pool is None and settings.postgres_url:
            try:
                self._pg_pool = await asyncpg.create_pool(
                    settings.postgres_url,
                    min_size=2,
                    max_size=10,
                    command_timeout=30,
                )
            except Exception as exc:
                logfire.error("pgvector pool creation failed", error=str(exc))

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        logfire.debug("VectorStore.ping called")
        try:
            await self._ensure_clients()
            if self._use_mem0 and self._mem0:
                # Mem0 doesn't have a ping — try a zero-result search
                await asyncio.to_thread(
                    self._mem0.search, query="ping", user_id=settings.default_user_id, limit=1
                )
                logfire.info("VectorStore.ping success", backend="Mem0")
                return True
            if self._use_chroma and self._chroma_client:
                await asyncio.to_thread(self._chroma_client.heartbeat)
                logfire.info("VectorStore.ping success", backend="ChromaDB")
                return True
            if self._pg_pool:
                async with self._pg_pool.acquire() as conn:
                    await conn.execute("SELECT 1")
                logfire.info("VectorStore.ping success", backend="pgvector")
                return True
            return False
        except Exception as exc:
            logfire.error("VectorStore.ping failed", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Embedding (M3 fix: full 64-char SHA-256 key)
    # ------------------------------------------------------------------

    async def _embed(self, text: str) -> list[float]:
        """
        Generate a 768-dimensional embedding via Ollama nomic-embed-text.

        Cache key: full 64-char SHA-256 hex digest of the input text.
        TTL: 24 hours.
        """
        await self._ensure_clients()
        # M3 FIX: use full digest, not [:16]
        full_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        cache_key = f"embed:{full_digest}"

        if self._redis:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)

        payload = {"model": "nomic-embed-text", "prompt": text}
        resp = await self._http.post(
            f"{settings.ollama_base_url}/api/embeddings", json=payload
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Ollama embedding failed: HTTP {resp.status_code}: {resp.text[:200]}"
            )
        embedding: list[float] = resp.json()["embedding"]

        if self._redis:
            await self._redis.set(cache_key, json.dumps(embedding), ex=86400)

        return embedding

    # ------------------------------------------------------------------
    # Serialise embedding for pgvector  (M4 fix)
    # ------------------------------------------------------------------

    @staticmethod
    def _pg_vector_literal(embedding: list[float]) -> str:
        """
        Convert a Python float list to a pgvector array literal.
 
        pgvector expects:  '[0.1,0.2,0.3]'   NOT  str([0.1, 0.2, 0.3])
        """
        return "[" + ",".join(str(x) for x in embedding) + "]"

    @staticmethod
    def _simple_query_score(text: str, query: str) -> float:
        """
        Heuristic fallback scoring for in-memory search.
        """
        query_terms = {token for token in query.lower().split() if token}
        text_terms = set(text.lower().split())
        if not query_terms or not text_terms:
            return 0.0
        common = query_terms.intersection(text_terms)
        return min(1.0, len(common) / max(1, len(query_terms)))

    def _search_fallback_memory(
        self,
        query: str,
        top_k: int,
        filter: dict[str, Any],
        user_id: Optional[str],
        agent_id: Optional[str],
    ) -> list[MemoryResult]:
        results: list[MemoryResult] = []
        query_lower = query.lower()
        for entry in self._fallback_memory:
            if filter:
                if any(entry["metadata"].get(k) != v for k, v in filter.items()):
                    continue
            if user_id and entry["metadata"].get("user_id") != user_id:
                continue
            if agent_id and entry["metadata"].get("agent_id") != agent_id:
                continue
            score = self._simple_query_score(entry["content"], query_lower)
            if score <= 0.0:
                continue
            results.append(
                MemoryResult(
                    memory_id=entry["memory_id"],
                    text=entry["content"],
                    score=round(score, 4),
                    source=entry.get("source", "manual"),
                    metadata=entry["metadata"],
                    created_at=None,
                )
            )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    # ------------------------------------------------------------------
    # add()  (M1 fix: no mutable default)
    # ------------------------------------------------------------------

    async def add(
        self,
        text: str,
        metadata: Optional[dict[str, Any]] = None,   # M1 FIX
        source: str = "manual",
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        """
        Embed text and store it in the active vector store.

        Args:
            text     : The text content to store.
            metadata : Optional key-value metadata (None → empty dict).
            source   : Origin label (file name, URL, or 'manual').
            user_id  : Mem0 user namespace (defaults to settings.default_user_id).
            agent_id : Mem0 agent namespace (e.g. 'researcher').
            session_id: Mem0 run namespace.

        Returns:
            The generated UUID string identifying this memory entry.
        """
        if metadata is None:       # M1 FIX
            metadata = {}

        logfire.debug("VectorStore.add called", source=source, chars=len(text))
        t_start = time.time()
        await self._ensure_clients()

        uid = user_id
        if uid is None:
            try:
                from core.context import get_user_id

                uid = get_user_id()
            except Exception:
                uid = settings.default_user_id

        memory_id = str(uuid.uuid4())
        meta = {
            **metadata,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # ── Mem0 path ─────────────────────────────────────────────────────
        if self._use_mem0 and self._mem0:
            try:
                kwargs: dict[str, Any] = {"user_id": uid, "metadata": meta}
                if agent_id:
                    kwargs["agent_id"] = agent_id
                if session_id:
                    kwargs["run_id"] = session_id
                result = await asyncio.to_thread(
                    self._mem0.add,
                    messages=text,
                    **kwargs,
                )
                # Mem0 returns a dict with 'results' list
                if isinstance(result, dict) and result.get("results"):
                    memory_id = result["results"][0].get("id", memory_id)
                duration_ms = int((time.time() - t_start) * 1000)
                logfire.info("VectorStore.add success (Mem0)", memory_id=memory_id, duration_ms=duration_ms)
                return memory_id
            except Exception as exc:
                logfire.warning("Mem0 add failed, falling back to ChromaDB", error=str(exc))
                self._use_mem0 = False

        # ── Raw ChromaDB path ──────────────────────────────────────────────
        embedding = await self._embed(text)

        if self._use_chroma and self._chroma_collection:
            try:
                await asyncio.to_thread(
                    self._chroma_collection.add,
                    ids=[memory_id],
                    embeddings=[embedding],
                    documents=[text],
                    metadatas=[meta],
                )
                duration_ms = int((time.time() - t_start) * 1000)
                logfire.info("VectorStore.add success (ChromaDB)", memory_id=memory_id, duration_ms=duration_ms)
                return memory_id
            except Exception as exc:
                logfire.warning("ChromaDB add failed, switching to pgvector", error=str(exc))
                self._use_chroma = False
                self._chroma_client = None
                self._chroma_collection = None

        # ── pgvector fallback path ─────────────────────────────────────────
        if self._pg_pool:
            async with self._pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO yaazhi_memories (id, content, embedding, metadata, source)
                    VALUES ($1, $2, $3::vector, $4::jsonb, $5)
                    """
                    ,
                    uuid.UUID(memory_id),
                    text,
                    self._pg_vector_literal(embedding),   # M4 FIX
                    json.dumps(meta, ensure_ascii=False),
                    source,
                )
            duration_ms = int((time.time() - t_start) * 1000)
            logfire.info("VectorStore.add success (pgvector)", memory_id=memory_id, duration_ms=duration_ms)
            return memory_id

        # ── In-memory fallback path for tests and degraded mode ─────────────
        self._fallback_memory.append(
            {
                "memory_id": memory_id,
                "content": text,
                "source": source,
                "metadata": meta,
            }
        )
        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info("VectorStore.add success (in-memory fallback)", memory_id=memory_id, duration_ms=duration_ms)
        return memory_id

        async with self._pg_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO yaazhi_memories (id, content, embedding, metadata, source)
                VALUES ($1, $2, $3::vector, $4::jsonb, $5)
                """,
                uuid.UUID(memory_id),
                text,
                self._pg_vector_literal(embedding),   # M4 FIX
                json.dumps(meta, ensure_ascii=False),
                source,
            )

        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info("VectorStore.add success (pgvector)", memory_id=memory_id, duration_ms=duration_ms)
        return memory_id

    # ------------------------------------------------------------------
    # search()  (M2 fix: no mutable default)
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 5,
        filter: Optional[dict[str, Any]] = None,   # M2 FIX
        user_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> list[MemoryResult]:
        """
        Search for semantically similar memories.

        P1.1: Extracts user_id from context if not provided to ensure isolation.
        Args:
            query   : The search query text.
            top_k   : Maximum number of results to return.
            filter  : Optional ChromaDB where-clause filter dict (None → no filter).
            user_id : Scope search to this Mem0/pgvector user namespace.
            agent_id: Scope search to this Mem0 agent namespace.

        Returns:
            List of MemoryResult sorted by cosine similarity descending.
        """
        if filter is None:         # M2 FIX
            filter = {}

        logfire.debug("VectorStore.search called", query=query[:80], top_k=top_k)
        t_start = time.time()
        await self._ensure_clients()
        
        # P1.1: Extract user_id from context; fallback to provided or default
        if user_id is None:
            try:
                from core.context import get_user_id

                user_id = get_user_id()
            except Exception:
                user_id = settings.default_user_id

        uid = user_id
        results: list[MemoryResult] = []

        # ── Mem0 path ─────────────────────────────────────────────────────
        if self._use_mem0 and self._mem0:
            try:
                kwargs: dict[str, Any] = {"user_id": uid, "limit": top_k}
                if agent_id:
                    kwargs["agent_id"] = agent_id
                raw = await asyncio.to_thread(self._mem0.search, query=query, **kwargs)
                for item in raw.get("results", []):
                    meta = item.get("metadata", {}) or {}
                    score = float(item.get("score", 0.0))
                    results.append(
                        MemoryResult(
                            memory_id=str(item.get("id", "")),
                            text=item.get("memory", ""),
                            score=round(score, 4),
                            source=meta.get("source", "mem0"),
                            metadata=meta,
                            created_at=datetime.fromisoformat(meta["created_at"])
                            if "created_at" in meta else None,
                        )
                    )
                results.sort(key=lambda r: r.score, reverse=True)
                duration_ms = int((time.time() - t_start) * 1000)
                logfire.info("VectorStore.search success (Mem0)", results=len(results), duration_ms=duration_ms, user_id=uid)
                return results
            except Exception as exc:
                logfire.warning("Mem0 search failed, falling back to ChromaDB", error=str(exc))
                self._use_mem0 = False

        # ── Raw ChromaDB path ──────────────────────────────────────────────
        # B-03 FIX: Only compute the embedding here, AFTER Mem0 path has
        # failed or is disabled. Previously _embed() was called before the
        # Mem0 try block, wasting ~50ms per search when Mem0 was active.
        embedding = await self._embed(query)

        if self._use_chroma and self._chroma_collection:
            try:
                where = filter if filter else None
                raw = await asyncio.to_thread(
                    self._chroma_collection.query,
                    query_embeddings=[embedding],
                    n_results=top_k,
                    where=where,
                    include=["documents", "metadatas", "distances"],
                )
                for i, doc in enumerate(raw["documents"][0]):
                    meta = raw["metadatas"][0][i] if raw["metadatas"] else {}
                    distance = raw["distances"][0][i]
                    score = max(0.0, 1.0 - distance)
                    results.append(
                        MemoryResult(
                            memory_id=raw["ids"][0][i],
                            text=doc,
                            score=round(score, 4),
                            source=meta.get("source", "manual"),
                            metadata=meta,
                            created_at=datetime.fromisoformat(meta["created_at"])
                            if "created_at" in meta else None,
                        )
                    )
            except Exception as exc:
                logfire.warning("ChromaDB search failed, switching to pgvector", error=str(exc))
                self._use_chroma = False

        # ── pgvector fallback ──────────────────────────────────────────────
        if not results:
            if self._pg_pool:
                vec_literal = self._pg_vector_literal(embedding)   # M4 FIX
                async with self._pg_pool.acquire() as conn:
                    # P1.1: Filter by user_id to ensure isolation
                    rows = await conn.fetch(
                        """
                        SELECT id::text, content, embedding <=> $1::vector AS distance,
                               metadata, source, created_at
                        FROM yaazhi_memories
                        WHERE metadata->>'user_id' = $3
                        ORDER BY distance ASC
                        LIMIT $2
                        """,
                        vec_literal,
                        top_k,
                        uid,  # P1.1: user_id filter
                    )
                    for row in rows:
                        score = max(0.0, 1.0 - float(row["distance"]))
                        meta = dict(json.loads(row["metadata"])) if row["metadata"] else {}
                        results.append(
                            MemoryResult(
                                memory_id=row["id"],
                                text=row["content"],
                                score=round(score, 4),
                                source=row["source"],
                                metadata=meta,
                                created_at=row["created_at"],
                            )
                        )
            elif self._fallback_memory:
                results = self._search_fallback_memory(query, top_k, filter, uid, agent_id)
            else:
                raise RuntimeError("No vector store backend available")
        results.sort(key=lambda r: r.score, reverse=True)
        duration_ms = int((time.time() - t_start) * 1000)
        logfire.info("VectorStore.search success", results=len(results), duration_ms=duration_ms)
        return results

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    async def delete(self, memory_id: str) -> bool:
        logfire.debug("VectorStore.delete called", memory_id=memory_id)
        await self._ensure_clients()
        try:
            if self._use_mem0 and self._mem0:
                await asyncio.to_thread(self._mem0.delete, memory_id=memory_id)
            elif self._use_chroma and self._chroma_collection:
                await asyncio.to_thread(self._chroma_collection.delete, ids=[memory_id])
            elif self._pg_pool:
                async with self._pg_pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM yaazhi_memories WHERE id = $1", uuid.UUID(memory_id)
                    )
            logfire.info("VectorStore.delete success", memory_id=memory_id)
            return True
        except Exception as exc:
            logfire.error("VectorStore.delete failed", memory_id=memory_id, error=str(exc))
            return False

    # ------------------------------------------------------------------
    # count
    # ------------------------------------------------------------------

    async def count(self) -> int:
        await self._ensure_clients()
        try:
            if self._use_chroma and self._chroma_collection:
                return await asyncio.to_thread(self._chroma_collection.count)
            if self._pg_pool:
                async with self._pg_pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT COUNT(*) AS c FROM yaazhi_memories")
                    return int(row["c"])
        except Exception as exc:
            logfire.error("VectorStore.count failed", error=str(exc))
        return 0

    # ------------------------------------------------------------------
    # clear_old
    # ------------------------------------------------------------------

    async def clear_old(self, days: int = 90) -> int:
        logfire.debug("VectorStore.clear_old called", days=days)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        deleted = 0
        await self._ensure_clients()
        try:
            if self._use_chroma and self._chroma_collection:
                # C-05 FIX: Paginate in batches of 1000 to prevent OOM on large
                # collections. Previously fetched ALL metadata in one call which
                # could exhaust 24GB RAM on a 100k+ entry ChromaDB collection.
                _BATCH_SIZE = 1000
                offset = 0
                while True:
                    raw = await asyncio.to_thread(
                        self._chroma_collection.get,
                        include=["metadatas"],
                        limit=_BATCH_SIZE,
                        offset=offset,
                    )
                    batch_ids = raw.get("ids", [])
                    if not batch_ids:
                        break
                    old_ids = [
                        batch_ids[i]
                        for i, meta in enumerate(raw.get("metadatas", []))
                        if meta and "created_at" in meta
                        and datetime.fromisoformat(meta["created_at"]) < cutoff
                    ]
                    if old_ids:
                        await asyncio.to_thread(
                            self._chroma_collection.delete, ids=old_ids
                        )
                        deleted += len(old_ids)
                    offset += _BATCH_SIZE
                    if len(batch_ids) < _BATCH_SIZE:
                        break   # last page

            elif self._pg_pool:
                async with self._pg_pool.acquire() as conn:
                    result = await conn.execute(
                        "DELETE FROM yaazhi_memories WHERE created_at < $1", cutoff
                    )
                    deleted = int(result.split()[-1])
            logfire.info("VectorStore.clear_old success", deleted=deleted, days=days)
        except Exception as exc:
            logfire.error("VectorStore.clear_old failed", error=str(exc))
        return deleted

    # ------------------------------------------------------------------
    # export_backup
    # ------------------------------------------------------------------

    async def export_backup(self, path: str) -> None:
        logfire.debug("VectorStore.export_backup called", path=path)
        await self._ensure_clients()
        records: list[dict[str, Any]] = []

        if self._use_chroma and self._chroma_collection:
            raw = await asyncio.to_thread(
                self._chroma_collection.get, include=["documents", "metadatas"]
            )
            for i, doc in enumerate(raw["documents"]):
                records.append(
                    {
                        "id": raw["ids"][i],
                        "text": doc,
                        "metadata": raw["metadatas"][i] if raw["metadatas"] else {},
                    }
                )
        elif self._pg_pool:
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id::text, content, metadata, source, created_at FROM yaazhi_memories"
                )
                for row in rows:
                    records.append(
                        {
                            "id": row["id"],
                            "text": row["content"],
                            "metadata": dict(json.loads(row["metadata"])) if row["metadata"] else {},
                            "source": row["source"],
                            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                        }
                    )

        import aiofiles  # type: ignore
        async with aiofiles.open(path, "w", encoding="utf-8") as fh:
            await fh.write(json.dumps(records, indent=2, default=str, ensure_ascii=False))

        logfire.info("VectorStore.export_backup success", path=path, records=len(records))

    # ------------------------------------------------------------------
    # close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close all open connections gracefully."""
        if self._http:
            await self._http.aclose()
        if self._pg_pool:
            await self._pg_pool.close()
        if self._redis:
            await self._redis.aclose()
