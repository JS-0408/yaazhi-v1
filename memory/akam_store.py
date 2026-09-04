"""
Yaazhi V4 — Akam Store (Private / Internal Cognitive Layers).

Implements the Akam domain described in the V4 framework:

  Layer  4 — Identity Graph       (user values, constraints, personality)
  Layer  7 — Affective/Stress Model (burnout_score, 0.0 = Fresh → 1.0 = Burnout)
  Layer 11 — Meta-Learning Epochs  (tracking what Yaazhi has learned over time)
  Layer 12 — Objective Mirror      (reflection of long-horizon objectives)

All data is persisted in the `akam_user_state` PostgreSQL table (V4 SQL schema)
with a Redis read-through cache for sub-millisecond access during planning.

Routing: only accessed when CognitiveRouter returns Domain.AKAM.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import logfire

from config.settings import settings


# ─── Akam State Model ────────────────────────────────────────────────────────

class AkamState:
    """
    In-memory snapshot of the Akam private store for a single request cycle.
    Populated by AkamStore.load() and passed into the planner context block.
    """

    def __init__(
        self,
        state_id: str,
        burnout_score: float,
        preferred_tone: str,
        active_constraints: dict[str, Any],
        identity_summary: str,
        meta_epochs: list[dict[str, Any]],
        objectives: list[str],
        updated_at: Optional[str] = None,
    ) -> None:
        self.state_id           = state_id
        self.burnout_score      = burnout_score      # Layer 7
        self.preferred_tone     = preferred_tone     # Layer 4 / user preference
        self.active_constraints = active_constraints # Layer 4 / hardware/env
        self.identity_summary   = identity_summary   # Layer 4 / personality snapshot
        self.meta_epochs        = meta_epochs        # Layer 11
        self.objectives         = objectives         # Layer 12
        self.updated_at         = updated_at

    def to_context_block(self, max_chars: int = 600) -> str:
        """
        Serialise to a compact Akam context block for injection into the
        2,048-token planning budget (Step 3 of the V4 workflow).
        """
        burnout_label = (
            "Critical" if self.burnout_score > 0.8 else
            "High"     if self.burnout_score > 0.6 else
            "Moderate" if self.burnout_score > 0.4 else
            "Low"
        )
        constraints_str = "; ".join(
            f"{k}: {v}" for k, v in list(self.active_constraints.items())[:4]
        )
        objectives_str = " | ".join(self.objectives[:3])

        block = (
            f"[AKAM] Burnout: {self.burnout_score:.2f} ({burnout_label}) | "
            f"Tone: {self.preferred_tone} | "
            f"Constraints: {constraints_str or 'none'} | "
            f"Objectives: {objectives_str or 'none'}"
        )
        return block[:max_chars]

    def __repr__(self) -> str:
        return (
            f"AkamState(burnout={self.burnout_score:.2f}, "
            f"tone={self.preferred_tone!r})"
        )


# ─── Akam Store ──────────────────────────────────────────────────────────────

class AkamStore:
    """
    Async gateway to the Akam private memory domain (Layers 4, 7, 11, 12).

    Persists to `akam_user_state` via PostgreSQL with Redis read-through cache.
    All writes are soft-insert (UPSERT) — the table always holds the latest
    snapshot rather than a history log (history lives in episodic_events).

    Configuration:
        Uses settings.postgres_url and settings.redis_url from config/.env.
    """

    _REDIS_KEY_TPL = "akam:user:{user_id}:state"
    _REDIS_TTL     = 3600  # 1 hour hot-cache

    def __init__(self) -> None:
        self._pg_pool:  Any = None
        self._redis:    Any = None

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    async def _ensure_pg(self) -> None:
        if self._pg_pool is not None:
            return
        if not settings.postgres_url:
            return
        try:
            import asyncpg  # type: ignore
            self._pg_pool = await asyncpg.create_pool(
                settings.postgres_url, min_size=1, max_size=3
            )
        except Exception as exc:
            logfire.warning("AkamStore: PostgreSQL pool failed", error=str(exc))

    async def _ensure_redis(self) -> None:
        if self._redis is not None:
            return
        try:
            import redis.asyncio as aioredis  # type: ignore
            self._redis = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True
            )
        except Exception as exc:
            logfire.warning("AkamStore: Redis connection failed", error=str(exc))

    # ------------------------------------------------------------------
    # Load — read Akam state (Redis → PG fallback)
    # ------------------------------------------------------------------

    async def load(self, user_id: Optional[str] = None) -> AkamState:
        """
        Load the current Akam state for the given user.

        Resolution order:
          1. Redis hot-cache (AkamState snapshot as JSON)
          2. PostgreSQL akam_user_state table
          3. Synthetic default (first-run)

        Args:
            user_id: User namespace. Defaults to settings.default_user_id.

        Returns:
            AkamState populated from the most recent persisted row.
        """
        uid = user_id or settings.default_user_id
        await self._ensure_redis()
        await self._ensure_pg()

        # 1. Hot cache
        if self._redis:
            try:
                raw = await self._redis.get(self._REDIS_KEY_TPL.format(user_id=uid))
                if raw:
                    data = json.loads(raw)
                    logfire.debug("AkamStore.load: cache hit", user_id=uid)
                    return self._hydrate(data)
            except Exception as exc:
                logfire.warning("AkamStore: Redis read failed", error=str(exc))

        # 2. PostgreSQL
        if self._pg_pool:
            try:
                async with self._pg_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT state_id, burnout_score, preferred_tone,
                               active_constraints, identity_summary,
                               meta_epochs, objectives, updated_at::text
                        FROM akam_user_state
                        WHERE user_id = $1
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """,
                        uid,
                    )
                    if row:
                        data = dict(row)
                        data["active_constraints"] = (
                            json.loads(data["active_constraints"])
                            if isinstance(data["active_constraints"], str)
                            else data["active_constraints"] or {}
                        )
                        data["meta_epochs"] = (
                            json.loads(data["meta_epochs"])
                            if isinstance(data["meta_epochs"], str)
                            else data["meta_epochs"] or []
                        )
                        data["objectives"] = (
                            json.loads(data["objectives"])
                            if isinstance(data["objectives"], str)
                            else data["objectives"] or []
                        )
                        state = self._hydrate(data)
                        await self._cache_state(uid, state)
                        return state
            except Exception as exc:
                logfire.warning("AkamStore.load: PG read failed", error=str(exc))

        # 3. First-run default
        logfire.info("AkamStore.load: no existing state, returning default", user_id=uid)
        return self._default_state()

    # ------------------------------------------------------------------
    # Save — persist Akam state
    # ------------------------------------------------------------------

    async def save(
        self,
        state: AkamState,
        user_id: Optional[str] = None,
    ) -> None:
        """
        Upsert the Akam state into `akam_user_state` and refresh the Redis cache.
        """
        uid = user_id or settings.default_user_id
        await self._ensure_pg()
        await self._ensure_redis()
        now = datetime.now(timezone.utc)

        if self._pg_pool:
            try:
                async with self._pg_pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO akam_user_state
                          (state_id, user_id, burnout_score, preferred_tone,
                           active_constraints, identity_summary,
                           meta_epochs, objectives, updated_at)
                        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8::jsonb, $9)
                        ON CONFLICT (user_id)
                        DO UPDATE SET
                          burnout_score      = EXCLUDED.burnout_score,
                          preferred_tone     = EXCLUDED.preferred_tone,
                          active_constraints = EXCLUDED.active_constraints,
                          identity_summary   = EXCLUDED.identity_summary,
                          meta_epochs        = EXCLUDED.meta_epochs,
                          objectives         = EXCLUDED.objectives,
                          updated_at         = EXCLUDED.updated_at
                        """,
                        state.state_id,
                        uid,
                        state.burnout_score,
                        state.preferred_tone,
                        json.dumps(state.active_constraints),
                        state.identity_summary,
                        json.dumps(state.meta_epochs),
                        json.dumps(state.objectives),
                        now,
                    )
                logfire.info("AkamStore.save: PG upsert success", user_id=uid)
            except Exception as exc:
                logfire.error("AkamStore.save: PG write failed", error=str(exc))

        await self._cache_state(uid, state)

    # ------------------------------------------------------------------
    # Affective update — Layer 7 (reflection pipeline Step 5)
    # ------------------------------------------------------------------

    async def update_burnout(
        self,
        command_count: int,
        error_count:   int,
        user_id: Optional[str] = None,
    ) -> float:
        """
        Recalculate and persist the burnout_score from telemetry.

        Formula:
            Δ_burnout = 0.05 * (error_count / max(command_count, 1))
            New score  = clamp(current + Δ, 0.0, 1.0)

        A successful session (error_rate < 0.1) reduces score by 0.02.

        Returns:
            The updated burnout_score.
        """
        state = await self.load(user_id)
        error_rate = error_count / max(command_count, 1)

        if error_rate > 0.1:
            delta = 0.05 * error_rate
        else:
            delta = -0.02  # recovery

        new_score = max(0.0, min(1.0, state.burnout_score + delta))
        state.burnout_score = new_score
        await self.save(state, user_id)
        logfire.info(
            "AkamStore.update_burnout",
            old=round(state.burnout_score - delta, 3),
            new=new_score,
            delta=round(delta, 4),
        )
        return new_score

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hydrate(data: dict) -> AkamState:
        return AkamState(
            state_id           = data.get("state_id",           str(uuid.uuid4())),
            burnout_score      = float(data.get("burnout_score", 0.0)),
            preferred_tone     = data.get("preferred_tone",      "Dry, Concise, Code-First"),
            active_constraints = data.get("active_constraints",  {}),
            identity_summary   = data.get("identity_summary",    ""),
            meta_epochs        = data.get("meta_epochs",         []),
            objectives         = data.get("objectives",          []),
            updated_at         = data.get("updated_at"),
        )

    @staticmethod
    def _default_state() -> AkamState:
        return AkamState(
            state_id           = str(uuid.uuid4()),
            burnout_score      = 0.0,
            preferred_tone     = "Dry, Concise, Code-First",
            active_constraints = {"hardware": "local", "llm_preference": "local_first"},
            identity_summary   = "Developer in University_Builder_Epoch.",
            meta_epochs        = [{"epoch": "University_Builder_Epoch", "started_at": datetime.now(timezone.utc).isoformat()}],
            objectives         = ["Complete B.Tech", "Ship BMS ML project", "Deploy Yaazhi V4"],
        )

    async def _cache_state(self, user_id: str, state: AkamState) -> None:
        if self._redis is None:
            return
        try:
            key = self._REDIS_KEY_TPL.format(user_id=user_id)
            payload = {
                "state_id":           state.state_id,
                "burnout_score":      state.burnout_score,
                "preferred_tone":     state.preferred_tone,
                "active_constraints": state.active_constraints,
                "identity_summary":   state.identity_summary,
                "meta_epochs":        state.meta_epochs,
                "objectives":         state.objectives,
                "updated_at":         state.updated_at,
            }
            await self._redis.set(key, json.dumps(payload), ex=self._REDIS_TTL)
        except Exception as exc:
            logfire.warning("AkamStore._cache_state: Redis write failed", error=str(exc))

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
        if self._pg_pool:
            await self._pg_pool.close()


# ─── Module-level singleton ───────────────────────────────────────────────────
akam_store = AkamStore()
