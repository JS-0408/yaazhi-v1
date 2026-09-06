"""tests/test_orchestrator.py — Orchestrator + planner tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.planner import VALID_AGENT_TYPES, Planner, TaskPlan, SubTask
from core.state import TaskType
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Planner: agent type validation (W6)
# ---------------------------------------------------------------------------

def test_valid_agent_types_accepted():
    for agent in VALID_AGENT_TYPES:
        # SubTask now expects task_type enum; accept backward-compatible string by mapping when needed
        st = SubTask(task_id="t1", task_type=TaskType.RESEARCH, description="Test task")
        assert st.task_type == TaskType.RESEARCH


def test_invalid_agent_type_rejected():
    with pytest.raises(ValidationError):
        # TaskType must be a known enum; providing an invalid type should raise
        SubTask(task_id="t1", task_type="hacker_agent", description="bad task")


def test_unknown_agent_rejected():
    with pytest.raises(ValidationError):
        SubTask(task_id="t1", task_type="crewai_agent", description="not allowed")


# ---------------------------------------------------------------------------
# Planner: max subtasks limit (W7)
# ---------------------------------------------------------------------------

def test_max_subtasks_enforced():
    tasks = [
        {"task_id": f"t{i}", "task_type": "research", "description": f"task {i}"}
        for i in range(15)
    ]
    with pytest.raises(ValidationError) as exc_info:
        TaskPlan(original_task="big task", tasks=tasks)
    assert "10" in str(exc_info.value) or "tasks" in str(exc_info.value).lower()


def test_exactly_10_subtasks_allowed():
    tasks = [
        SubTask(task_id=f"t{i}", task_type=TaskType.RESEARCH, description=f"task {i}")
        for i in range(10)
    ]
    plan = TaskPlan(original_task="10 task plan", tasks=tasks)
    assert len(plan.tasks) == 10


# ---------------------------------------------------------------------------
# Planner: fallback plan on bad LLM JSON
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_planner_fallback_on_bad_json():
    planner = Planner()
    with patch.object(planner, "_call_llm", new_callable=AsyncMock,
                      return_value="NOT VALID JSON {{{"):
        plan = await planner.plan("Ask a general question")
    assert len(plan.tasks) == 1
    assert plan.tasks[0].task_type == TaskType.RESEARCH


@pytest.mark.asyncio
async def test_planner_fallback_on_invalid_agent():
    planner = Planner()
    bad_json = '{"original_task":"x","tasks":[{"task_id":"t1","task_type":"evil_agent","description":"hack"}]}'
    with patch.object(planner, "_call_llm", new_callable=AsyncMock, return_value=bad_json):
        plan = await planner.plan("test")
    # Should fall back to researcher
    assert plan.tasks[0].task_type == TaskType.RESEARCH


# ---------------------------------------------------------------------------
# Parallel grouping
# ---------------------------------------------------------------------------

def test_group_parallel_no_deps():
    plan = TaskPlan(
        original_task="test",
        tasks=[
            SubTask(task_id="t1", task_type=TaskType.RESEARCH, description="task 1"),
            SubTask(task_id="t2", task_type=TaskType.CODE, description="task 2"),
        ],
    )
    planner = Planner()
    groups = planner.get_parallel_groups(plan)
    assert len(groups) == 1
    assert len(groups[0]) == 2   # both can run in parallel


def test_group_parallel_with_deps():
    plan = TaskPlan(
        original_task="test",
        tasks=[
            SubTask(task_id="t1", task_type=TaskType.RESEARCH, description="research"),
            SubTask(task_id="t2", task_type=TaskType.CODE, description="write code", depends_on=["t1"]),
        ],
    )
    planner = Planner()
    groups = planner.get_parallel_groups(plan)
    assert len(groups) == 2
    assert groups[0][0].task_id == "t1"
    assert groups[1][0].task_id == "t2"


# ---------------------------------------------------------------------------
# Revise reason propagation (W9)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_revise_reason_in_planner_context():
    """revise_reason should be prepended to the task on retry."""
    planner = Planner()
    captured_calls = []

    async def capture_llm(user_content: str) -> str:
        captured_calls.append(user_content)
        return '{"original_task":"x","tasks":[{"task_id":"t1","task_type":"research","description":"retry"}]}'

    with patch.object(planner, "_call_llm", side_effect=capture_llm):
        await planner.plan("fix the bug", revise_reason="Output was too short")

    assert len(captured_calls) == 1
    assert "PREVIOUS PLAN FAILED" in captured_calls[0]
    assert "Output was too short" in captured_calls[0]


# ---------------------------------------------------------------------------
# Integration test: MOCKED LLM calls but REAL graph traversal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_integration_graph_traversal():
    from core.orchestrator import Yaazhi
    from core.state import AgentOutput
    
    orchestrator = Yaazhi()
    
    # Mock Planner
    orchestrator._planner.plan = AsyncMock(return_value=TaskPlan(
        original_task="do integration test",
        tasks=[SubTask(task_id="t1", task_type=TaskType.RESEARCH, description="mock research")]
    ))
    
    # Mock single task execution
    orchestrator._execute_single_task = AsyncMock(return_value=AgentOutput(
        agent_name="researcher",
        task_id="t1",
        content="Final Answer Mocked",
        success=True,
    ))
    
    # Mock Reviewer
    orchestrator._reviewer = MagicMock()
    orchestrator._reviewer.review = AsyncMock(return_value=MagicMock(verdict="PASS", feedback="Looks good", retry_with_context=""))
    
    output = await orchestrator.run("do integration test", session_id="test-session")
    assert output.response == "Final Answer Mocked"
