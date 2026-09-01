"""
Yaazhi Cost Tracker — LiteLLM callback-based spend monitoring.

Upgrade 3.3: Per-request token cost tracking, daily budget alerts,
cost-aware model routing. Exposes /costs FastAPI endpoint.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import litellm
import logfire
import redis.asyncio as aioredis

from config.settings import settings
from core.context import get_user_id


# ---------------------------------------------------------------------------
# LiteLLM custom logger
# ---------------------------------------------------------------------------

class YaazhiCostCallback(litellm.CustomLogger):  # type: ignore[misc]
    """
    LiteLLM callback that intercepts every successful completion and
    records cost to Redis.

    Keys:
      yaazhi:costs:daily:{YYYY-MM-DD}    — daily total (float string)
      yaazhi:costs:monthly:{YYYY-MM}     — monthly total
      yaazhi:costs:by_model:{model}      — per-model total
      yaazhi:costs:sessions:{session_id} — per-session total
      yaazhi:costs:users:{user_id}       — per-user total (P1.1: Multi-User Context)
    """

    def __init__(self) -> None:
        super().__init__()
        self._redis: Optional[aioredis.Redis] = None
        self._notifier_ref = None   # set after app startup to avoid circular import

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True
            )
        return self._redis

    async def async_log_success_event(self, kwargs: dict, response_obj, start_time, end_time) -> None:  # type: ignore
        """
        A-06 FIX: Use async callback instead of sync log_success_event.

        LiteLLM supports async callbacks natively via async_log_success_event.
        The old sync version used asyncio.get_event_loop() which is deprecated
        in Python 3.10+ and could silently drop cost records if the loop
        wasn't running. This async version is guaranteed to execute correctly.
        """
        await self._store_cost_async(kwargs, response_obj)

    async def _store_cost_async(self, kwargs: dict, response_obj) -> None:
        try:
            model = kwargs.get("model", "unknown")
            session_id = (kwargs.get("metadata") or {}).get("session_id", "global")
            
            # P1.1: Extract user_id from context for user-scoped cost tracking
            try:
                user_id = get_user_id()
            except RuntimeError:
                user_id = "default"
            
            cost = 0.0
            try:
                cost = litellm.completion_cost(completion_response=response_obj)
            except Exception:
                pass

            now = datetime.now(timezone.utc)
            day_key = f"yaazhi:costs:daily:{now.strftime('%Y-%m-%d')}"
            month_key = f"yaazhi:costs:monthly:{now.strftime('%Y-%m')}"
            model_key = f"yaazhi:costs:by_model:{model.replace('/', '_')}"
            session_key = f"yaazhi:costs:sessions:{session_id}"
            user_key = f"yaazhi:costs:users:{user_id}"  # P1.1: Per-user cost tracking

            r = await self._get_redis()
            pipe = r.pipeline()
            pipe.incrbyfloat(day_key, cost)
            pipe.expire(day_key, 86400 * 7)    # keep 7 days
            pipe.incrbyfloat(month_key, cost)
            pipe.expire(month_key, 86400 * 35)  # keep ~1 month+
            pipe.incrbyfloat(model_key, cost)
            pipe.incrbyfloat(session_key, cost)
            pipe.expire(session_key, 86400)
            pipe.incrbyfloat(user_key, cost)    # P1.1: Track per-user
            pipe.expire(user_key, 86400)
            await pipe.execute()

            logfire.debug(
                "CostTracker: recorded spend",
                model=model,
                cost_usd=round(cost, 6),
                session_id=session_id[:8] if session_id else "global",
                user_id=user_id,  # P1.1: Log user for traceability
            )

            # Budget alert
            daily_total = float(await r.get(day_key) or 0)
            if daily_total > settings.daily_budget_usd:
                await self._fire_budget_alert(daily_total)

        except Exception as exc:
            logfire.warning("CostTracker._store_cost_async failed", error=str(exc))

    async def _fire_budget_alert(self, daily_total: float) -> None:
        logfire.warning(
            "BUDGET ALERT: daily LLM spend exceeded threshold",
            daily_total_usd=round(daily_total, 4),
            threshold_usd=settings.daily_budget_usd,
        )
        if self._notifier_ref:
            try:
                await self._notifier_ref.broadcast(
                    f"⚠️ Yaazhi daily LLM spend: ${daily_total:.4f} "
                    f"(threshold: ${settings.daily_budget_usd:.2f})",
                    channels=["telegram"],
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CostTracker — query interface
# ---------------------------------------------------------------------------

class CostTracker:
    """Query interface for stored cost data."""

    def __init__(self) -> None:
        self._redis: Optional[aioredis.Redis] = None
        self.callback = YaazhiCostCallback()

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                settings.redis_url, encoding="utf-8", decode_responses=True
            )
        return self._redis

    async def get_summary(self) -> dict:
        """Return a JSON-serialisable summary of all cost data."""
        r = await self._get_redis()
        now = datetime.now(timezone.utc)

        daily_key = f"yaazhi:costs:daily:{now.strftime('%Y-%m-%d')}"
        monthly_key = f"yaazhi:costs:monthly:{now.strftime('%Y-%m')}"

        daily = float(await r.get(daily_key) or 0)
        monthly = float(await r.get(monthly_key) or 0)

        # Per-model breakdown
        model_keys = []
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor, match="yaazhi:costs:by_model:*", count=50)
            model_keys.extend(keys)
            if cursor == 0:
                break

        by_model: dict[str, float] = {}
        for k in model_keys:
            model_name = k.replace("yaazhi:costs:by_model:", "").replace("_", "/")
            val = float(await r.get(k) or 0)
            by_model[model_name] = round(val, 6)

        return {
            "daily_usd": round(daily, 6),
            "monthly_usd": round(monthly, 6),
            "daily_budget_usd": settings.daily_budget_usd,
            "budget_remaining_usd": round(
                max(0.0, settings.daily_budget_usd - daily), 6
            ),
            "by_model": by_model,
            "as_of": now.isoformat(),
        }

    async def get_cheapest_model(self, task_type: str) -> str:
        """
        Return the cheapest capable model for a task_type based on
        historical per-model costs stored in Redis.

        Falls back to settings.get_litellm_model(task_type) if no data.
        """
        r = await self._get_redis()
        cursor, keys = 0, []
        while True:
            cursor, batch = await r.scan(cursor, match="yaazhi:costs:by_model:*", count=50)
            keys.extend(batch)
            if cursor == 0:
                break

        if not keys:
            return settings.get_litellm_model(task_type)

        costs: list[tuple[str, float]] = []
        for k in keys:
            model_name = k.replace("yaazhi:costs:by_model:", "").replace("_", "/")
            val = float(await r.get(k) or 0)
            costs.append((model_name, val))

        costs.sort(key=lambda x: x[1])
        cheapest = costs[0][0] if costs else settings.get_litellm_model(task_type)
        logfire.debug("CostTracker.get_cheapest_model", task=task_type, model=cheapest)
        return cheapest

    def install_callback(self) -> None:
        """Register the cost callback with LiteLLM."""
        if self.callback not in litellm.callbacks:
            litellm.callbacks.append(self.callback)
            logfire.info("CostTracker: LiteLLM callback installed")
