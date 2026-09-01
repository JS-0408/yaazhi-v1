"""tests/test_cost_tracker.py — CostTracker tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.cost_tracker import CostTracker, YaazhiCostCallback


# ---------------------------------------------------------------------------
# Cost accumulation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cost_accumulation(mock_redis):
    tracker = CostTracker()
    tracker._redis = mock_redis

    # Simulate two model calls stored
    mock_redis.get = AsyncMock(side_effect=["0.001234", "0.002500", "0.001234", "0.003734"])

    summary = await tracker.get_summary()
    assert "daily_usd" in summary
    assert "monthly_usd" in summary
    assert "by_model" in summary
    assert "budget_remaining_usd" in summary


@pytest.mark.asyncio
async def test_budget_alert_fires_when_exceeded(mock_redis):
    """When daily spend > threshold, the alert path should be triggered."""
    callback = YaazhiCostCallback()
    callback._redis = mock_redis

    # Simulate daily total exceeding budget
    mock_redis.get = AsyncMock(return_value="999.99")
    mock_redis.pipeline = MagicMock(return_value=AsyncMock(
        execute=AsyncMock(return_value=[True]),
        incrbyfloat=MagicMock(),
        expire=MagicMock(),
    ))

    alert_fired = []

    async def mock_alert(total):
        alert_fired.append(total)

    with patch.object(callback, "_fire_budget_alert", side_effect=mock_alert):
        await callback._store_cost_async(
            {"model": "groq/llama3", "metadata": {"session_id": "test"}},
            MagicMock(usage=MagicMock(prompt_tokens=10, completion_tokens=20)),
        )

    assert len(alert_fired) > 0


# ---------------------------------------------------------------------------
# Cheapest model
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_cheapest_model_returns_lowest_cost(mock_redis):
    tracker = CostTracker()
    tracker._redis = mock_redis

    # Simulate two models with different costs
    mock_redis.scan = AsyncMock(return_value=(0, [
        "yaazhi:costs:by_model:groq_llama3",
        "yaazhi:costs:by_model:gpt-4o",
    ]))
    cost_map = {
        "yaazhi:costs:by_model:groq_llama3": "0.05",
        "yaazhi:costs:by_model:gpt-4o": "2.50",
    }
    mock_redis.get = AsyncMock(side_effect=lambda k: cost_map.get(k, "0"))

    cheapest = await tracker.get_cheapest_model("research")
    assert "groq" in cheapest.lower()


# ---------------------------------------------------------------------------
# Callback installation
# ---------------------------------------------------------------------------

def test_callback_installed_in_litellm():
    import litellm
    tracker = CostTracker()
    original_callbacks = list(litellm.callbacks)

    tracker.install_callback()
    assert tracker.callback in litellm.callbacks

    # Cleanup
    litellm.callbacks = original_callbacks


@pytest.mark.asyncio
async def test_get_summary_structure(mock_redis):
    mock_redis.get = AsyncMock(return_value="0.0")
    mock_redis.scan = AsyncMock(return_value=(0, []))

    tracker = CostTracker()
    tracker._redis = mock_redis
    summary = await tracker.get_summary()

    required_keys = {"daily_usd", "monthly_usd", "daily_budget_usd",
                     "budget_remaining_usd", "by_model", "as_of"}
    assert required_keys.issubset(summary.keys())
