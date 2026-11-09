"""
Memory resilience primitives shared by VectorStore and EpisodicMemory.

  RetryGate               — cooldown so a down dependency (PostgreSQL) is not
                            re-dialled on every request (asyncpg's default
                            connect timeout is 60 s, which stalls every call).
  EmbeddingUnavailableError — raised when no real embedding can be produced.
                            Previously a fake hash "embedding" was stored and
                            cached for 24 h, silently corrupting semantic search.
  PendingMemoryQueue      — durable Redis list holding writes that could not be
                            persisted yet; drained once the dependency recovers.
                            Shared across VPS nodes through the same Redis.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import logfire

PENDING_QUEUE_KEY = "yaazhi:memory:pending"
PENDING_QUEUE_MAX = 5000          # bound so a long outage cannot exhaust Redis RAM
PG_CONNECT_TIMEOUT_S = 10.0
PG_RETRY_COOLDOWN_S = 30.0


class EmbeddingUnavailableError(RuntimeError):
    """No real embedding could be generated (embedding service unreachable)."""


class RetryGate:
    """Allow an operation at most once per `cooldown_s` after a failure."""

    def __init__(self, cooldown_s: float = PG_RETRY_COOLDOWN_S) -> None:
        self._cooldown_s = cooldown_s
        self._next_attempt = 0.0

    def ready(self) -> bool:
        return time.monotonic() >= self._next_attempt

    def record_failure(self) -> None:
        self._next_attempt = time.monotonic() + self._cooldown_s

    def reset(self) -> None:
        self._next_attempt = 0.0


class PendingMemoryQueue:
    """Durable write-behind queue for memories awaiting an embedding / database."""

    def __init__(self, redis_getter: Any) -> None:
        # redis_getter: zero-arg callable returning the current redis client or None
        self._get_redis = redis_getter

    async def enqueue(self, record: dict[str, Any]) -> bool:
        """Push a record. Returns False if it could not be stored durably."""
        r = self._get_redis()
        if r is None:
            return False
        try:
            await r.rpush(PENDING_QUEUE_KEY, json.dumps(record, ensure_ascii=False))
            await r.ltrim(PENDING_QUEUE_KEY, -PENDING_QUEUE_MAX, -1)
            return True
        except Exception as exc:
            logfire.warn("PendingMemoryQueue.enqueue failed", error=str(exc))
            return False

    async def size(self) -> int:
        r = self._get_redis()
        if r is None:
            return 0
        try:
            return int(await r.llen(PENDING_QUEUE_KEY))
        except Exception:
            return 0

    async def pop(self) -> Optional[dict[str, Any]]:
        """Pop the oldest record, or None when empty / Redis unavailable."""
        r = self._get_redis()
        if r is None:
            return None
        try:
            raw = await r.lpop(PENDING_QUEUE_KEY)
        except Exception as exc:
            logfire.warn("PendingMemoryQueue.pop failed", error=str(exc))
            return None
        if raw is None:
            return None
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            return json.loads(text)
        except (ValueError, UnicodeDecodeError) as exc:
            logfire.warn("PendingMemoryQueue: dropping corrupt record", error=str(exc))
            return None

