"""
Yaazhi V4 — V4 Episodic Event Logger (Puram Domain — Layer 2).

Persists raw execution telemetry to the `episodic_events` PostgreSQL table
defined in the V4 SQL schema (init_v4.sql).  This is distinct from the
existing EpisodicMemory (conversation turns) — it logs structured events
with Tiṇai pre-filtering metadata for the reflection pipeline.

V4 Schema:
  episodic_events (
    event_id         UUID PRIMARY KEY,
    timestamp        TIMESTAMPTZ,
    tinai            tinai_category,
    kalam_epoch      VARCHAR(50),
    orchestrator_id  VARCHAR(50),
    event_type       VARCHAR(50),
    summary          TEXT,
    context_payload  JSONB,
    embedding        vector(1536)   -- populated asynchronously
  )
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import logfire

from config.settings import settings


class EpisodicEventLogger:
    """
    Async logger that writes structured execution events to `episodic_events`.

    Designed to be called fire-and-forget from the orchestrator's reflection
    pipeline (Step 5) without blocking the user-facing response.

    Usage (inside background task)::

        await event_logger.log(
            tinai="arch_linux_dev",
            event_type="code_execution",
            summary="Generated systemd suspend script",
            context_payload={"task_id": "t1", "duration_ms": 842},
            orchestrator_id="coder",
        )
    """

    def __init__(self) -> None:
        self._pg_pool: Any = None

    async def _ensure_pg(self) -> None:
        if self._pg_pool is not None:
            return
        if not settings.postgres_url:
            return
        try:
            import asyncpg  # type: ignore
            self._pg_pool = await asyncpg.create_pool(
                settings.postgres_url, min_size=1, max_size=3, statement_cache_size=0
            )
        except Exception as exc:
            logfire.warning("EpisodicEventLogger: PG pool failed", error=str(exc))

    async def log(
        self,
        tinai:            str = "general_technical",
        kalam_epoch:      str = "University_Builder_Epoch",
        orchestrator_id:  str = "orchestrator",
        event_type:       str = "task_execution",
        summary:          str = "",
        context_payload:  Optional[dict[str, Any]] = None,
        event_id:         Optional[str] = None,
    ) -> Optional[str]:
        """
        Insert a row into `episodic_events`.

        The `embedding` column is left NULL here and populated later by the
        asynchronous reflection pipeline (Layer 9 Fact Abstraction).

        Args:
            tinai:           Tiṇai category string.
            kalam_epoch:     Current temporal epoch.
            orchestrator_id: Which orchestrator/agent produced this event.
            event_type:      Coarse event classification.
            summary:         Human-readable summary of the event.
            context_payload: Arbitrary structured metadata.
            event_id:        Override the UUID (for idempotent replay).

        Returns:
            The event_id of the inserted row, or None on failure.
        """
        await self._ensure_pg()
        if self._pg_pool is None:
            logfire.debug("EpisodicEventLogger.log: no PG pool, skipping")
            return None

        eid = event_id or str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        payload_json = json.dumps(context_payload or {}, ensure_ascii=False)

        try:
            async with self._pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO episodic_events
                      (event_id, timestamp, tinai, kalam_epoch, orchestrator_id,
                       event_type, summary, context_payload)
                    VALUES ($1, $2, $3::tinai_category, $4, $5, $6, $7, $8::jsonb)
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    eid, now, tinai, kalam_epoch,
                    orchestrator_id, event_type, summary, payload_json,
                )
            logfire.debug(
                "EpisodicEventLogger.log: inserted",
                event_id=eid[:8],
                tinai=tinai,
                event_type=event_type,
            )
            return eid
        except Exception as exc:
            logfire.error("EpisodicEventLogger.log failed", error=str(exc))
            return None

    async def fetch_recent(
        self,
        tinai:  Optional[str] = None,
        limit:  int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Fetch recent episodic events, optionally filtered by Tiṇai.

        Used by the reflection pipeline to batch-process events for fact
        abstraction (Layer 9).

        Args:
            tinai:  If provided, restricts query to this Tiṇai category.
            limit:  Max rows (default 50 for reflection batch size).

        Returns:
            List of row dicts ordered by timestamp DESC.
        """
        await self._ensure_pg()
        if self._pg_pool is None:
            return []

        where = "WHERE tinai = $3::tinai_category" if tinai else ""
        params: list[Any] = [limit, offset]
        if tinai:
            params.append(tinai)

        query = f"""
        SELECT event_id::text, timestamp::text, tinai, kalam_epoch,
               orchestrator_id, event_type, summary, context_payload
        FROM episodic_events
        {where}
        ORDER BY timestamp DESC
        LIMIT $1 OFFSET $2
        """
        try:
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(query, *params)
                return [dict(r) for r in rows]
        except Exception as exc:
            logfire.error("EpisodicEventLogger.fetch_recent failed", error=str(exc))
            return []

    async def close(self) -> None:
        if self._pg_pool:
            await self._pg_pool.close()


# ─── Module-level singleton ───────────────────────────────────────────────────
event_logger = EpisodicEventLogger()
