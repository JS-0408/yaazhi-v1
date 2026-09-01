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


class EpisodicMemory:
    """
    Conversation history store with Redis (primary) + PostgreSQL (durable).

    All methods are async. Falls back gracefully if PG is unavailable.
    SEC-9 FIX: user_id always read from settings.default_user_id or context.
    P1.1 FIX: Redis keys namespaced by user_id to ensure multi-user isolation.
    
    Redis key format (P1.1): user:{user_id}:session:{session_id}:messages
    """

    _SESSION_TTL = 86400          # 24 h
    _MAX_MESSAGES_IN_REDIS = 200

    def __init__(self) -> None:
        self._redis: Optional[aioredis.Redis] = None
        self._pg_pool: Optional[asyncpg.Pool] = None

    async def _ensure_redis(self) -> None:
        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True
            )

    async def _ensure_pg(self) -> None:
        if self._pg_pool is None and settings.postgres_url:
            try:
                self._pg_pool = await asyncpg.create_pool(
                    settings.postgres_url, min_size=1, max_size=5
                )
            except Exception as exc:
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
        pipe = self._redis.pipeline()
        pipe.rpush(redis_key, json.dumps(msg, ensure_ascii=False))
        pipe.ltrim(redis_key, -self._MAX_MESSAGES_IN_REDIS, -1)
        pipe.expire(redis_key, self._SESSION_TTL)
        await pipe.execute()

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
        Tries Redis first; falls back to PostgreSQL.
        """
        # P1.1: Extract user_id from context; fallback to provided or default
        if user_id is None:
            try:
                user_id = get_user_id()
            except RuntimeError:
                user_id = settings.default_user_id
        
        await self._ensure_redis()
        redis_key = f"user:{user_id}:session:{session_id}:messages"
        raw_msgs = await self._redis.lrange(redis_key, -last_n, -1)
        if raw_msgs:
            return [json.loads(m) for m in raw_msgs]

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
                    return [dict(r) for r in reversed(rows)]
            except Exception as exc:
                logfire.warning("EpisodicMemory.get_history PG failed", error=str(exc))
        return []

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
        
        await self._ensure_redis()
        await self._redis.delete(f"user:{user_id}:session:{session_id}:messages")
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
        await self._redis.set(summary_key, summary, ex=self._SESSION_TTL)
        return summary

    # ------------------------------------------------------------------
    # Preference store (SEC-9: no hardcoded user_id)
    # ------------------------------------------------------------------

    async def set_preference(self, key: str, value: str, user_id: Optional[str] = None) -> None:
        uid = user_id or settings.default_user_id
        await self._ensure_redis()
        pref_key = f"prefs:{uid}:{key}"
        await self._redis.set(pref_key, value)

    async def get_preference(self, key: str, default: str = "", user_id: Optional[str] = None) -> str:
        uid = user_id or settings.default_user_id
        await self._ensure_redis()
        pref_key = f"prefs:{uid}:{key}"
        val = await self._redis.get(pref_key)
        return val if val is not None else default

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
        if self._pg_pool:
            await self._pg_pool.close()
