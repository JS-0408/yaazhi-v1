"""
Yaazhi Episodic Memory — Redis primary + PostgreSQL durable backup.

Audit fixes (2026-05-10):
  INF-6  : yaazhi_conversations table now written to via PostgreSQLEpisodicStore.
  SEC-9  : default_user_id from settings — never hardcoded 'santhosh'.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import logfire
import redis.asyncio as aioredis

from config.settings import settings
from core.context import get_user_id
from memory.resilience import PG_CONNECT_TIMEOUT_S, RetryGate


class EpisodicMemory:
    """
    Conversation history store with Redis (primary) + PostgreSQL (durable)
    + bounded in-memory fallback when both Redis and PostgreSQL are unreachable.

    All methods are async. Falls back gracefully if Redis or PG is unavailable.
    SEC-9 FIX: user_id always read from settings.default_user_id or context.
    P1.1 FIX: Redis keys namespaced by user_id to ensure multi-user isolation.
    
    Redis key format (P1.1): user:{user_id}:session:{session_id}:messages
    """

    _SESSION_TTL = 86400          # 24 h
    _MAX_MESSAGES_IN_REDIS = 200
    _MAX_FALLBACK_SESSIONS = 200

    def __init__(self) -> None:
        self._redis: Optional[aioredis.Redis] = None
        self._pg_pool: Optional[asyncpg.Pool] = None
        self._pg_gate: RetryGate = RetryGate()
        self._fallback_sessions: dict[str, list[dict]] = {}
        self._fallback_prefs: dict[str, str] = {}

    async def _ensure_redis(self) -> None:
        if self._redis is None:
            try:
                self._redis = aioredis.from_url(
                    settings.redis_url, encoding="utf-8", decode_responses=True
                )
            except Exception as exc:
                logfire.warning("EpisodicMemory: Redis client creation failed", error=str(exc))

    async def _ensure_pg(self) -> None:
        if self._pg_pool is None and settings.postgres_url and self._pg_gate.ready():
            try:
                self._pg_pool = await asyncpg.create_pool(
                    settings.postgres_url,
                    min_size=1,
                    max_size=5,
                    timeout=PG_CONNECT_TIMEOUT_S,
                    statement_cache_size=0,
                )
                self._pg_gate.reset()
            except Exception as exc:
                self._pg_gate.record_failure()
                logfire.warning("EpisodicMemory: PostgreSQL pool failed", error=str(exc))

    async def ping(self) -> bool:
        try:
            await self._ensure_redis()
            await self._redis.ping()
            return True
        except Exception as exc:
            logfire.error("EpisodicMemory.ping failed", error=str(exc))
            return False

    # ------------------------------------------------------------------
    # add_message — Redis + PG write
    # ------------------------------------------------------------------

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        language: str = "en",
        user_id: Optional[str] = None,
    ) -> None:
        """
        Persist a conversation turn to Redis (primary) and PostgreSQL (backup).

        P1.1 FIX: Extracts user_id from context if not provided.
        SEC-9 FIX: user_id defaults to settings.default_user_id or context user_id.
        INF-6 FIX: writes to yaazhi_conversations table.
        """
        # P1.1: Extract user_id from context; fallback to provided or default
        if user_id is None:
            try:
                user_id = get_user_id()
            except RuntimeError:
                user_id = settings.default_user_id
        
        await self._ensure_redis()
        await self._ensure_pg()

        msg = {
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "language": language,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # P1.1: Redis key namespaced by user_id for isolation
        redis_key = f"user:{user_id}:session:{session_id}:messages"
        redis_ok = False
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.rpush(redis_key, json.dumps(msg, ensure_ascii=False))
                pipe.ltrim(redis_key, -self._MAX_MESSAGES_IN_REDIS, -1)
                pipe.expire(redis_key, self._SESSION_TTL)
                await pipe.execute()
                redis_ok = True
            except Exception as exc:
                logfire.warning("EpisodicMemory.add_message: Redis write failed, using fallback", error=str(exc))

        if not redis_ok:
            bucket = self._fallback_sessions.setdefault(redis_key, [])
            bucket.append(msg)
            if len(bucket) > self._MAX_MESSAGES_IN_REDIS:
                del bucket[:-self._MAX_MESSAGES_IN_REDIS]
            if len(self._fallback_sessions) > self._MAX_FALLBACK_SESSIONS:
                oldest_key = next(iter(self._fallback_sessions))
                self._fallback_sessions.pop(oldest_key, None)

        # PostgreSQL: durable backup (INF-6 FIX)
        if self._pg_pool:
            try:
                async with self._pg_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO yaazhi_conversations
                          (session_id, user_id, role, content, language)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        session_id, user_id, role, content, language,
                    )
            except Exception as exc:
                self._pg_gate.record_failure()
                logfire.warning("EpisodicMemory: PG write failed (non-fatal)", error=str(exc))

    # ------------------------------------------------------------------
    # get_history
    # ------------------------------------------------------------------

    async def get_history(
        self,
        session_id: str,
        last_n: int = 20,
        user_id: Optional[str] = None,
    ) -> list[dict]:
        """
        Fetch the most recent messages for a session.

        P1.1: Extracts user_id from context if not provided to ensure isolation.
        Tries Redis first; falls back to PostgreSQL, then in-memory fallback.
        """
        # P1.1: Extract user_id from context; fallback to provided or default
        if user_id is None:
            try:
                user_id = get_user_id()
            except RuntimeError:
                user_id = settings.default_user_id
        
        await self._ensure_redis()
        await self._ensure_pg()
        redis_key = f"user:{user_id}:session:{session_id}:messages"
        if self._redis is not None:
            try:
                raw_msgs = await self._redis.lrange(redis_key, -last_n, -1)
                if raw_msgs:
                    return [json.loads(m) for m in raw_msgs]
            except Exception as exc:
                logfire.warning("EpisodicMemory.get_history: Redis read failed, falling back to PG", error=str(exc))

        # PG fallback
        if self._pg_pool:
            try:
                async with self._pg_pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT session_id, user_id, role, content, language,
                               created_at::text AS timestamp
                        FROM yaazhi_conversations
                        WHERE session_id = $1 AND user_id = $2
                        ORDER BY created_at DESC
                        LIMIT $3
                        """,
                        session_id, user_id, last_n,
                    )
                    if rows:
                        return [dict(r) for r in reversed(rows)]
            except Exception as exc:
                self._pg_gate.record_failure()
                logfire.warning("EpisodicMemory.get_history PG failed", error=str(exc))

        # In-memory fallback
        fallback_msgs = self._fallback_sessions.get(redis_key, [])
        return list(fallback_msgs[-last_n:])

    # ------------------------------------------------------------------
    # clear_session
    # ------------------------------------------------------------------

    async def clear_session(self, session_id: str, user_id: Optional[str] = None) -> None:
        """P1.1: Clears only the user-scoped session data."""
        # P1.1: Extract user_id from context; fallback to provided or default
        if user_id is None:
            try:
                user_id = get_user_id()
            except RuntimeError:
                user_id = settings.default_user_id
        
        redis_key = f"user:{user_id}:session:{session_id}:messages"
        self._fallback_sessions.pop(redis_key, None)
        await self._ensure_redis()
        if self._redis is not None:
            try:
                await self._redis.delete(redis_key)
            except Exception as exc:
                logfire.warning("EpisodicMemory.clear_session Redis delete failed", error=str(exc))
        logfire.info("EpisodicMemory: session cleared", session_id=session_id[:8], user_id=user_id)

    # ------------------------------------------------------------------
    # summarize_session
    # ------------------------------------------------------------------

    async def summarize_session(self, session_id: str, user_id: Optional[str] = None) -> str:
        """P1.1: Summarise long sessions using LLM and cache the summary (user-scoped)."""
        # P1.1: Extract user_id from context; fallback to provided or default
        if user_id is None:
            try:
                user_id = get_user_id()
            except RuntimeError:
                user_id = settings.default_user_id
        
        history = await self.get_history(session_id, last_n=50, user_id=user_id)
        if not history:
            return ""
        text = "\n".join(f"{m['role']}: {m['content']}" for m in history)
        try:
            import litellm
            model = settings.get_litellm_model("fast_tasks")
            resp = await litellm.acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": "Summarise this conversation concisely."},
                    {"role": "user", "content": text[:6000]},
                ],
                max_tokens=300,
                temperature=0.1,
            )
            summary = resp.choices[0].message.content or ""
        except Exception as exc:
            logfire.warning("EpisodicMemory.summarize_session LLM failed", error=str(exc))
            summary = text[:500]

        await self._ensure_redis()
        summary_key = f"user:{user_id}:session:{session_id}:summary"
        if self._redis is not None:
            try:
                await self._redis.set(summary_key, summary, ex=self._SESSION_TTL)
            except Exception as exc:
                logfire.warning("EpisodicMemory.summarize_session Redis cache failed", error=str(exc))
        return summary

    # ------------------------------------------------------------------
    # Preference store (SEC-9: no hardcoded user_id)
    # ------------------------------------------------------------------

    async def set_preference(self, key: str, value: str, user_id: Optional[str] = None) -> None:
        uid = user_id or settings.default_user_id
        pref_key = f"prefs:{uid}:{key}"
        self._fallback_prefs[pref_key] = value
        await self._ensure_redis()
        if self._redis is not None:
            try:
                await self._redis.set(pref_key, value)
            except Exception as exc:
                logfire.warning("EpisodicMemory.set_preference Redis failed", error=str(exc))

    async def get_preference(self, key: str, default: str = "", user_id: Optional[str] = None) -> str:
        uid = user_id or settings.default_user_id
        pref_key = f"prefs:{uid}:{key}"
        await self._ensure_redis()
        if self._redis is not None:
            try:
                val = await self._redis.get(pref_key)
                if val is not None:
                    return val
            except Exception as exc:
                logfire.warning("EpisodicMemory.get_preference Redis failed", error=str(exc))
        return self._fallback_prefs.get(pref_key, default)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
        if self._pg_pool:
            await self._pg_pool.close()
