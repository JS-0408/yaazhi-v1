"""
Yaazhi Core Package.

Keep package initialisation import-safe (no eager orchestrator import) to avoid
circular imports when modules import `core.agent_registry`.
"""

from core.guardrails import validate_user_input
from core.state import (
    AgentOutput,
    Language,
    MemoryResult,
    ReviewResult,
    ReviewVerdict,
    SubTask,
    TaskPlan,
    TaskType,
    YaazhiInput,
    YaazhiOutput,
    YaazhiState,
    make_initial_state,
)

__all__ = [
    "Yaazhi",
    "Planner",
    "Reviewer",
    "validate_user_input",
    "YaazhiState",
    "YaazhiInput",
    "YaazhiOutput",
    "TaskType",
    "ReviewVerdict",
    "Language",
    "SubTask",
    "TaskPlan",
    "AgentOutput",
    "ReviewResult",
    "MemoryResult",
    "make_initial_state",
]


def __getattr__(name: str):
    if name == "Yaazhi":
        from core.orchestrator import Yaazhi
        return Yaazhi
    if name == "Planner":
        from core.planner import Planner
        return Planner
    if name == "Reviewer":
        from core.reviewer import Reviewer
        return Reviewer
    raise AttributeError(name)
